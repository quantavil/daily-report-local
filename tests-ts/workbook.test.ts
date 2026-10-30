import { describe, expect, test } from 'bun:test';
import { strToU8, unzipSync, zipSync } from 'fflate';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { openBook, readCells } from '../src/ts/ooxml';
import { WorkbookStore } from '../src/ts/workbook';
import type { RowUpdate } from '../src/ts/models';
import { SHEET, SHEET2, HEADERS, fixture } from './fixture';

const update = (row_idx: number, over: Partial<RowUpdate> = {}): RowUpdate => ({ row_idx, status: 'Success', values: {}, container_fields: new Set(), containers: [], detail: '', ...over });
const cells = async (path: string, sheet = SHEET) => readCells(await openBook(new Uint8Array(await readFile(path))), sheet);

describe('workbook safety', () => {
  test('reports invalid bill numbers within scope while cancellation takes precedence', async () => {
    const path = await fixture([['123', 'bad date', 'bad'], ['bad', 'bad date', 'bad', null, null, null, null, null, null, 'CANCELLED'], ['7546404', '07/10/2026', 'INMBD6']]);
    const store = await WorkbookStore.open(path);
    const rows = store.rows(SHEET);
    expect(rows).toHaveLength(3);
    expect(rows[0]?.error).toContain('SB number');
    expect(rows[1]).toMatchObject({cancelled:true});
    expect(store.rows(SHEET, undefined, '7546404').map(row => row.row_idx)).toEqual([4]);
  });
  test('reads scoped rows, ISO dates from serials and cancelled rows', async () => {
    const path = await fixture([
      ['7546404', '<c r="B2"><v>46302</v></c>', 'INMBD6', null, null, null, null, null, null, null, 'Partial'],
      ['7546405', '07/10/2026', 'INMBD6', null, null, null, null, null, null, 'Cancelled', null],
    ]);
    const store = await WorkbookStore.open(path);
    const [first, second] = store.rows(SHEET);
    expect(first).toMatchObject({ row_idx: 2, status: 'Partial', cancelled: false, identity: { sb_no: '7546404', sb_date: '2026-10-07' } });
    expect(second?.cancelled).toBe(true);
    expect(store.rows(SHEET, new Set([3]))).toHaveLength(1);
  });

  test('writes only the target cells and leaves every other part byte-identical', async () => {
    const path = await fixture([['7546404', '07/10/2026', 'INMBD6', null, null, null, null, null, null, null, null]]);
    const before = unzipSync(new Uint8Array(await readFile(path)));
    const store = await WorkbookStore.open(path);
    store.apply(SHEET, [update(2, { values: { inv_no: 'INV1', leo_date: '2026-10-14' } })]);
    await store.save();
    const after = unzipSync(new Uint8Array(await readFile(path)));
    const changed = Object.keys(before).filter(k => Buffer.compare(before[k]!, after[k]!) !== 0).sort();
    expect(changed).toEqual(['xl/styles.xml', 'xl/worksheets/sheet1.xml']);
    const c = await cells(path);
    expect(c.get('D2')?.v).toBe('INV1');
    expect(c.get('E2')?.v).toBe('46309');
    expect(c.get('K2')?.v).toBe('Success');
    expect(c.get('B2')?.v).toBe('07/10/2026');
  });

  test('keeps formula cells even with force and downgrades the row', async () => {
    const path = await fixture([['7546404', '07/10/2026', 'INMBD6', '<c r="D2"><f>1+1</f><v>2</v></c>', null, null, null, null, null, null, null]]);
    const store = await WorkbookStore.open(path);
    const updates = [update(2, { values: { inv_no: 'INV1' } })];
    store.apply(SHEET, updates, true);
    expect(updates[0]?.status).toBe('Partial');
    await store.save();
    const c = await cells(path);
    expect(c.get('D2')).toMatchObject({ f: '1+1', v: '2' });
  });

  test('treats shared-formula children as formulas and never overwrites them', async () => {
    const shared = '<c r="D2"><f t="shared" ref="D2:D3" si="0">1+1</f><v>2</v></c>', child = '<c r="D3"><f t="shared" si="0"/><v>2</v></c>';
    const path = await fixture([['7546404', '07/10/2026', 'INMBD6', shared], ['7546405', '07/10/2026', 'INMBD6', child]]);
    const store = await WorkbookStore.open(path);
    expect(store.rows(SHEET)[1]?.values.inv_no).toBe('=');
    const updates = [update(3, { values: { inv_no: 'INV2' } })];
    store.apply(SHEET, updates, true);
    expect(updates[0]?.status).toBe('Partial');
    expect(updates[0]?.detail).toContain('Protected formula D3');
    expect(await store.save()).toBe(true);
    expect((await cells(path)).get('D3')).toMatchObject({ f: '', v: '2' });
  });

  test('several sheets share one save: one backup, both sheets written, newest five backups kept', async () => {
    const path = await fixture([['7546404', '07/10/2026', 'INMBD6']], '', true);
    const backups = async () => (await readdir(dirname(path))).filter(n => n.includes('.bak-'));
    for (let i = 0; i < 7; i++) {
      const store = await WorkbookStore.open(path);
      for (const sheet of [SHEET, SHEET2]) store.apply(sheet, [update(2, { values: { inv_no: `INV${i}` } })], true);
      expect(await store.save()).toBe(true);
      if (i === 0) expect(await backups()).toHaveLength(1);
    }
    expect((await cells(path)).get('D2')?.v).toBe('INV6');
    expect((await cells(path, SHEET2)).get('D2')?.v).toBe('INV6');
    expect(await backups()).toHaveLength(5);
    expect(await (await WorkbookStore.open(path)).save()).toBe(false);
  });

  test('does not overwrite filled cells without force, and does with force', async () => {
    const path = await fixture([['7546404', '07/10/2026', 'INMBD6', 'OLD', null, null, null, null, null, null, null]]);
    const store = await WorkbookStore.open(path);
    store.apply(SHEET, [update(2, { values: { inv_no: 'NEW' } })]);
    expect((await store.save(), (await cells(path)).get('D2')?.v)).toBe('OLD');
    const forced = await WorkbookStore.open(path);
    forced.apply(SHEET, [update(2, { values: { inv_no: 'NEW' } })], true);
    await forced.save();
    expect((await cells(path)).get('D2')?.v).toBe('NEW');
  });

  test('shared merged cells are blocked unless every merged row is selected', async () => {
    const merge = '<mergeCells count="1"><mergeCell ref="D2:D3"/></mergeCells>';
    const rows = [['7546404', '07/10/2026', 'INMBD6'], ['7546405', '07/10/2026', 'INMBD6']];
    const path = await fixture(rows, merge);
    const store = await WorkbookStore.open(path);
    const partial = [update(2, { values: { inv_no: 'INV1' } })];
    store.apply(SHEET, partial);
    expect(partial[0]?.status).toBe('Partial');
    expect(partial[0]?.detail).toContain('Blocked shared cell D2');
    const both = [update(2, { values: { inv_no: 'INV1' } }), update(3, { values: { inv_no: 'INV1' } })];
    store.apply(SHEET, both);
    await store.save();
    expect((await cells(path)).get('D2')?.v).toBe('INV1');
  });

  test('rejects saving if another process changed the workbook since open', async () => {
    const path = await fixture([['7546404', '07/10/2026', 'INMBD6']]);
    const store = await WorkbookStore.open(path);
    store.apply(SHEET, [update(2, { values: { inv_no: 'A' } })]);
    await writeFile(path, await readFile(path).then(b => Buffer.concat([b, Buffer.from('x')])));
    const before = await readFile(path);
    await expect(store.save()).rejects.toThrow('changed since opening');
    expect(await readFile(path)).toEqual(before);
  });
});
