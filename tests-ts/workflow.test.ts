import { describe, expect, test } from 'bun:test';
import { existsSync, mkdtempSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_MAP } from '../src/ts/excel/export-map';
import { IcegateError, NotReady, type IcegateClient } from '../src/ts/icegate/client';
import { cells_for, report_sheets, sync } from '../src/ts/workflow';
import { fill_bill, fill_containers, get_bill, list_bills, open_db, set_iec_name, shared_containers } from '../src/ts/store/db';
import { pdf } from './fixture';

const k = { sb_no: '7546404', sb_date: '2026-10-07', port_code: 'INMBD6' };
const IDENTITY: [string, number, number][] = [['SB No', 20, 760], ['7546404', 100, 760], ['SB Date', 200, 760], ['07/10/2026', 280, 760], ['Port Code', 400, 760], ['INMBD6', 480, 760]];
const TABLE: [string, number, number][] = [['11. Container', 20, 700], ['12. Seal', 200, 700], ['14. Size', 400, 700], ['ABCU1234567', 20, 680], ['SEAL1', 200, 680], ['40', 400, 680]];
const GATEPASS = pdf([...IDENTITY, ['3.Exporter', 20, 740], ['GATEPASS EXPORTS LTD', 100, 740], ['IEC/Br', 20, 720], ['0123456789', 100, 720], ...TABLE]);
const EMPTY = { sbDetailsModel: [], currentStatusModel: [], roslStatusModel: [], gatewayEgmStatusModel: [], egmStatusModel: [], igstInvoiceDetailsModel: [] };

function fake(over: Partial<IcegateClient> = {}): IcegateClient & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    jobStatus: async () => { calls.push('job'); return [{ 'Location Code': 'INMBD6', IEC: '0123456789', 'SB No': '7546404', 'SB date': '2026-10-07', Status: 'LEO', 'Job No': '', 'Job Date': '', Error: '', 'Control Number': '' }]; },
    myIec: async () => { calls.push('iec'); return []; },
    enquiry: async () => { calls.push('enquiry'); return {
      sbDetailsModel: [{ jobNo: '665', jobDate: '27 OCT 2026 12:09' }], currentStatusModel: [{ leoDate: '14 OCT 2026 00:09' }], roslStatusModel: [], gatewayEgmStatusModel: [],
      egmStatusModel: [], igstInvoiceDetailsModel: [{ invoiceNo: 'INV1', invoiceDate: '13-10-2026', igstValueCustom: 'N.A.' }] }; },
    plan: () => {},
    gatepassWindow: async () => { calls.push('window'); return []; },
    gatepassSearch: async () => { calls.push('search'); return [{ irn: 'IRN1', userId: 'U1' }]; },
    gatepassDownload: async (_r, out) => { calls.push('download'); await writeFile(out, GATEPASS); },
    ...over,
  };
}
const keep = mkdtempSync(join(tmpdir(), 'gatepass-'));
const opts = { workers: 2, excel_only: false, force: false, from: '2026-10-01', gatepass_dir: keep };
const run = (client: IcegateClient, extra: Partial<typeof opts> = {}, db = open_db(':memory:')) => sync(db, client, { ...opts, gatepass_dir: mkdtempSync(join(tmpdir(), 'gatepass-')), ...extra }).then(r => ({ db, ...r }));
const named = (db: ReturnType<typeof open_db>) => set_iec_name(db, '0123456789', 'ACME EXPORTS', 'my-iec');

describe('sync', () => {
  test('creates the bill from Job Status, fills it from the portal and names the exporter from the IEC', async () => {
    const db = open_db(':memory:'); named(db);
    const { results } = await run(fake(), {}, db);
    expect(results[0]![1]).toMatchObject({ status: 'Success' });
    expect(get_bill(db, k)).toMatchObject({ job_no: '665', job_date: '2026-10-27', leo_date: '2026-10-14', exporter: 'ACME EXPORTS', iec: '0123456789' });
  });
  test('a parsed gatepass PDF is kept in the gatepass folder', async () => {
    await run(fake(), { gatepass_dir: keep });
    expect(existsSync(join(keep, '2026-10-07_7546404.pdf'))).toBe(true);
  });
  test('the gatepass exporter names a bill whose IEC has no name, but never replaces an IEC name', async () => {
    const unnamed = await run(fake());
    expect(get_bill(unnamed.db, k)!.exporter).toBe('GATEPASS EXPORTS LTD');
    expect(unnamed.results[0]![1].status).toBe('Success');
    const db = open_db(':memory:'); named(db);
    await run(fake(), {}, db);
    expect(get_bill(db, k)!.exporter).toBe('ACME EXPORTS');
  });
  test('the gatepass IEC fills a bill whose Job Status row has none, and never replaces the Job Status IEC', async () => {
    const noIec = fake({ jobStatus: async () => [{ 'Location Code': 'INMBD6', 'SB No': '7546404', 'SB date': '2026-10-07' }] });
    const { db, results } = await run(noIec);
    expect(get_bill(db, k)).toMatchObject({ iec: '0123456789', exporter: 'GATEPASS EXPORTS LTD' });
    expect(results[0]![1].status).toBe('Success');
    db.query("UPDATE bills SET iec = '1111111111'").run(); db.query("UPDATE bills SET exporter = '', fetched_gatepass = ''").run();
    await run(fake(), {}, db);
    expect(get_bill(db, k)!.iec).toBe('1111111111');
  });
  test('gatepass months create bills that Job Status never lists', async () => {
    const records = [{ docNumber: '8108428', docDate: '27-10-2026', location: 'INMBD6' }, { docNumber: 'bad', docDate: '27-10-2026', location: 'INMBD6' }, { docNumber: '8108429', docDate: '28-10-2026', location: 'INMBD6' }];
    const asked: string[] = [];
    const { db, warnings } = await run(fake({ jobStatus: async () => [], gatepassWindow: async (from, to) => { asked.push(`${from}..${to}`); return from.startsWith('2026-10') ? records : []; } }), { from: '2026-09-15' });
    expect(asked.sort()).toEqual(['2026-09-01..2026-09-30', '2026-10-01..2026-10-31']);
    expect(list_bills(db).map(b => b.sb_no).sort()).toEqual(['8108428', '8108429']);
    expect(warnings).toEqual([]);
  });
  test('a failed month is a warning and the other months still run', async () => {
    const { db, warnings } = await run(fake({ gatepassWindow: async from => { if (from.startsWith('2026-09')) throw new IcegateError('slow'); return []; } }), { from: '2026-09-01' });
    expect(warnings).toEqual(['2026-09: slow']);
    expect(list_bills(db)).toHaveLength(1);
  });
  test('complete bills are skipped before any portal call', async () => {
    const db = open_db(':memory:'); named(db);
    await run(fake(), {}, db);
    const client = fake();
    const { results } = await run(client, {}, db);
    expect(results).toEqual([]);
    expect(client.calls).not.toContain('enquiry');
    expect(client.calls).not.toContain('search');
  });
  test('a kept gatepass is parsed again without the portal', async () => {
    const db = open_db(':memory:');
    await run(fake(), { gatepass_dir: keep }, db); // keeps the PDF; no IEC name, so the exporter names the exporter
    db.query("UPDATE bills SET exporter = '', fetched_gatepass = ''").run(); db.query('DELETE FROM containers').run();
    const client = fake();
    await run(client, { gatepass_dir: keep }, db);
    expect(client.calls).not.toContain('search');
    expect(client.calls).not.toContain('download');
    expect(get_bill(db, k)!.exporter).toBe('GATEPASS EXPORTS LTD');
  });
  test('--force queries a complete bill again', async () => {
    const db = open_db(':memory:'); named(db);
    await run(fake(), {}, db);
    const client = fake();
    const { results } = await run(client, { force: true }, db);
    expect(results[0]![1].status).toBe('Success');
    expect(client.calls).toContain('enquiry');
    expect(client.calls).toContain('search');
  });
  test('a gatepass that is not ready leaves blanks and the bill Partial', async () => {
    const { results, db } = await run(fake({ gatepassDownload: async () => { throw new NotReady('retry in about 60 minutes'); } }));
    expect(results[0]![1]).toMatchObject({ status: 'Partial' });
    expect(results[0]![1].detail).toContain('gatepass not ready');
    expect(get_bill(db, k)!.exporter).toBe('');
  });
  test('an enquiry failure is an Error on that bill only', async () => {
    const ok = fake().enquiry;
    const second = { sb_no: '7546405', sb_date: '2026-10-07', port_code: 'INMBD6' };
    const { results } = await run(fake({ jobStatus: async () => [{ 'Location Code': 'INMBD6', 'SB No': '7546404', 'SB date': '2026-10-07' }, { 'Location Code': 'INMBD6', 'SB No': '7546405', 'SB date': '2026-10-07' }],
      enquiry: async b => { if (b.sb_no === second.sb_no) throw new IcegateError('boom'); return ok(b); } }));
    const by = Object.fromEntries(results.map(([key, o]) => [key.sb_no, o]));
    expect(by['7546404']!.status).toBe('Success');
    expect(by['7546405']).toMatchObject({ status: 'Error' });
    expect(by['7546405']!.detail).toContain('enquiry: boom');
  });
  test('a bill the portal does not know is Not found, run after run', async () => {
    const db = open_db(':memory:'), client = fake({ jobStatus: async () => [], enquiry: async () => EMPTY, gatepassWindow: async () => [{ docNumber: '7546404', docDate: '07-10-2026', location: 'INMBD6' }] });
    for (let i = 0; i < 2; i++) expect((await run(client, {}, db)).results[0]![1].status).toBe('Not found');
  });
  test('a bill cancelled at the portal is never worked on', async () => {
    const client = fake({ enquiry: async () => ({ ...EMPTY, sbDetailsModel: [{ jobNo: '1' }], currentStatusModel: [{ currStatus: 'SB is Cancelled' }] }) });
    const { results } = await run(client);
    expect(results[0]![1].status).toBe('Cancelled');
    expect(client.calls).not.toContain('search');
  });
  test('--excel never calls the portal and works from the database', async () => {
    const client = fake();
    const db = open_db(':memory:'); fill_bill(db, k, {});
    const { results } = await run(client, { excel_only: true }, db);
    expect(client.calls).toEqual([]);
    expect(results[0]![1].status).toBe('Not found');
  });
  test('a bill with an invalid portal date is Error, never Success', async () => {
    const ok = fake().enquiry;
    const { results, db } = await run(fake({ enquiry: async b => { const e = await ok(b); e.currentStatusModel = [{ leoDate: 'soon' }]; return e; } }));
    expect(results[0]![1]).toMatchObject({ status: 'Error' });
    expect(results[0]![1].detail).toContain('Invalid date');
    expect(get_bill(db, k)!.leo_date).toBe('');
  });
  test('a database failure during discovery stops the run instead of being skipped', async () => {
    const db = open_db(':memory:'); db.run('DROP TABLE bills');
    await expect(run(fake(), {}, db)).rejects.toThrow();
  });
  test('a failed Job Status login stops the run before any change', async () => {
    await expect(run(fake({ jobStatus: async () => { throw new IcegateError('login failed'); } }))).rejects.toThrow('login failed');
  });
});

describe('report', () => {
  test('cells_for joins list fields with newlines and keeps blank positions', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, {});
    fill_containers(db, k, [{ container_no: 'A', size: '40' }, { container_no: 'B', seal: 'S2' }]);
    const c = cells_for(db, k, DEFAULT_MAP, new Set());
    expect(c['Container No.']!.value).toBe('A\nB');
    expect(c['Container Size']!.value).toBe('40\n');
    expect(c['Customs Seal Number']!.value).toBe('\nS2');
    expect(c['LEO Date']).toBeUndefined();
    expect(c['Shipping Bill Date']).toEqual({ value: '2026-10-07', date: true });
    expect(c['Status']!.value).toBe('Not found');
  });
  test('a container shared by bills of different exporters is derived LCL; nothing else is', () => {
    const db = open_db(':memory:'), other = { sb_no: '7546405', sb_date: '2026-10-08', port_code: 'INMBD6' }, alone = { sb_no: '7546406', sb_date: '2026-10-09', port_code: 'INMBD6' };
    fill_bill(db, k, { exporter: 'A' }); fill_bill(db, other, { exporter: 'B' }); fill_bill(db, alone, { exporter: 'A' });
    fill_containers(db, k, [{ container_no: 'SHARED1' }]); fill_containers(db, other, [{ container_no: 'SHARED1' }]); fill_containers(db, alone, [{ container_no: 'OWN1' }]);
    const lcl = shared_containers(db);
    expect([...lcl]).toEqual(['SHARED1']);
    expect(cells_for(db, k, DEFAULT_MAP, lcl)['Type Shipment']!.value).toBe('LCL (derived)');
    expect(cells_for(db, alone, DEFAULT_MAP, lcl)['Type Shipment']).toBeUndefined();
  });
  test('one sheet per financial year, oldest first, bills by date', () => {
    const db = open_db(':memory:');
    for (const [no, date] of [['3', '2026-10-01'], ['2', '2026-03-31'], ['1', '2026-04-01'], ['4', '2025-05-05']]) fill_bill(db, { sb_no: no!.padStart(7, '0'), sb_date: date!, port_code: 'INMBD6' }, {});
    const sheets = report_sheets(db, DEFAULT_MAP);
    expect(sheets.map(s => [s.name, s.rows.map(r => r['Shipping Bill No']!.value)])).toEqual([['FY 2025-26', ['0000004', '0000002']], ['FY 2026-27', ['0000001', '0000003']]]);
  });
});
