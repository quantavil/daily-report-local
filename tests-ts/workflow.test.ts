import { describe, expect, test } from 'bun:test';
import { existsSync, mkdtempSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_MAP } from '../src/ts/excel/export-map';
import { GmailError } from '../src/ts/gmail';
import { IcegateError, NotReady, type IcegateClient } from '../src/ts/icegate/client';
import { read_sheets } from '../src/ts/excel/read';
import { write_report } from '../src/ts/excel/report';
import { cells_for, keep_notes, publish_report, report_sheets, sync, gatepass_counts, type SyncOptions } from '../src/ts/workflow';
import { fill_bill, fill_containers, get_bill, list_bills, notes_of, open_db, set_iec_name, set_notes, shared_containers } from '../src/ts/store/db';
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
    sbcopySearch: async () => { calls.push('sbsearch'); return []; },
    gatepassSearch: async () => { calls.push('search'); return [{ irn: 'IRN1', userId: 'U1' }]; },
    ecopyDownload: async (_r, out) => { calls.push('download'); await writeFile(out, GATEPASS); },
    ...over,
  };
}
const keep = mkdtempSync(join(tmpdir(), 'gatepass-'));
const opts: SyncOptions = { gmail: undefined, workers: 2, excel_only: false, force: false, from: '2026-10-01', gatepass_dir: keep };
const run = (client: IcegateClient, extra: Partial<SyncOptions> = {}, db = open_db(':memory:')) => sync(db, client, { ...opts, gatepass_dir: mkdtempSync(join(tmpdir(), 'gatepass-')), ...extra }).then(r => ({ db, ...r }));
const named = (db: ReturnType<typeof open_db>) => set_iec_name(db, '0123456789', 'ACME EXPORTS', 'my-iec');

describe('Gmail evidence', () => {
  const mail = (over: Partial<{ has_mail: boolean; filenames: string[]; sent_to: string[]; sent_cc: string[] }>) => async () => ({ has_mail: true, filenames: [], sent_to: [], sent_cc: [], ...over });
  test('one checklist token is own job Y, and a single outside address is the agent email', async () => {
    const { db } = await run(fake(), { mail: mail({ filenames: ['CheckList-EXP-2627-70.pdf'], sent_to: ['Agent@Cargo.com', 'info@vishallogistics.in'] }) });
    expect(notes_of(db, k)).toMatchObject({ own_job: 'Y', agent_email: 'agent@cargo.com' });
  });
  test('no token with a LEO date and some mail is N; several tokens are listed for the owner to settle', async () => {
    expect(notes_of((await run(fake(), { mail: mail({}) })).db, k)).toEqual({ own_job: 'N' });
    expect(notes_of((await run(fake(), { mail: mail({ filenames: ['a-EXP-2627-70.pdf', 'VIDE-EXP-2627-71.pdf', 'EXP-2627-70.pdf'] }) })).db, k).own_job).toBe('CHECK: EXP-2627-70, VIDE-EXP-2627-71');
  });
  test('typed notes win, a bill is read once, and a failure leaves bills unread with one warning', async () => {
    const db = open_db(':memory:'); let calls = 0;
    const counted = async () => { calls++; return { has_mail: true, filenames: ['EXP-2627-1.pdf'], sent_to: [], sent_cc: [] }; };
    await run(fake(), { mail: counted }, db);
    set_notes(db, k, { own_job: '' }); // the owner clears the cell
    await run(fake(), { mail: counted }, db);
    expect(calls).toBe(1);
    expect(notes_of(db, k).own_job).toBeUndefined();
    const typed = open_db(':memory:'); await run(fake(), { mail: async () => { throw new GmailError('no auth'); } }, typed);
    const again = await run(fake(), { mail: mail({ filenames: ['EXP-2627-9.pdf'] }) }, typed);
    expect(notes_of(typed, k).own_job).toBe('Y');
    expect(again.warnings).toEqual([]);
  });
  test('a failure is one warning', async () => {
    const r = await run(fake(), { mail: async () => { throw new GmailError('no auth'); } });
    expect(r.warnings.filter(w => w.includes('mail evidence'))).toHaveLength(1);
  });
});

describe('sync', () => {
  test('stores the SB e-copy job counter without downloading when IEC and exporter are known', async () => {
    const db = open_db(':memory:'); named(db);
    const client = fake({ sbcopySearch: async () => [{ irn: 'IRN9', userId: 'U', jobNumber: '953', docNumber: '7546404', location: 'INMBD6' }] });
    await run(client, {}, db);
    expect(get_bill(db, k)!.sb_job_no).toBe('953');
  });
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
    const records = [{ docNumber: '8108428', docDate: '27-10-2026', jobDate: '27-10-2026', location: 'INMBD6' }, { docNumber: 'bad', docDate: '27-10-2026', jobDate: '27-10-2026', location: 'INMBD6' }, { docNumber: '8108429', docDate: '28-10-2026', jobDate: '28-10-2026', location: 'INMBD6' }, { docNumber: '8108430', docDate: '20-12-2002', jobDate: '28-10-2026', location: 'INMBD6' }];
    const asked: string[] = [];
    const { db, warnings } = await run(fake({ jobStatus: async () => [], gatepassWindow: async (from, to) => { asked.push(`${from}..${to}`); return from.startsWith('2026-10') ? records : []; } }), { from: '2026-09-15', to: '2026-10-31' }); // `to` keeps the month list independent of today
    expect(asked.sort()).toEqual(['2026-09-01..2026-09-30', '2026-10-01..2026-10-31']);
    expect(list_bills(db).map(b => b.sb_no).sort()).toEqual(['8108428', '8108429']);
    expect(warnings).toEqual([]);
  });
  describe('a gatepass record listed with a placeholder SB date', () => {
    const placeholder = { docNumber: '7546404', docDate: '20-12-2002', jobDate: '08-10-2026', location: 'INMBD6' };
    const listing = (over: Partial<IcegateClient> = {}) => fake({ jobStatus: async () => [], gatepassWindow: async from => from.startsWith('2026-10') ? [placeholder] : [], ...over });
    const mail = (...files: Uint8Array[]) => { const asked: string[] = []; return { asked, gmail: async function* (bill: typeof k) { asked.push(`${bill.sb_no} ${bill.sb_date}`); yield* files; } }; };
    test('takes its SB date from the Gmail PDF, which is kept for the bill', async () => {
      const { asked, gmail } = mail(GATEPASS), dir = mkdtempSync(join(tmpdir(), 'gatepass-'));
      const { db, results } = await run(listing(), { gmail, gatepass_dir: dir });
      expect(list_bills(db).map(b => `${b.sb_date} ${b.sb_no}`)).toEqual(['2026-10-07 7546404']);
      expect(existsSync(join(dir, '2026-10-07_7546404.pdf'))).toBe(true);
      expect(asked[0]).toBe('7546404 2026-10-08'); // searched by the job date's financial year
      expect(results[0]![1]).toMatchObject({ status: 'Success', gatepass: 'saved' });
    });
    test('creates nothing without a PDF that names this bill with a plausible date', async () => {
      const other = pdf([['SB No', 20, 760], ['9999999', 100, 760], ['SB Date', 200, 760], ['07/10/2026', 280, 760], ['Port Code', 400, 760], ['INMBD6', 480, 760]]);
      const stale = pdf([['SB No', 20, 760], ['7546404', 100, 760], ['SB Date', 200, 760], ['07/09/2026', 280, 760], ['Port Code', 400, 760], ['INMBD6', 480, 760]]);
      for (const files of [[], [other], [stale], [new Uint8Array([1, 2, 3])]]) {
        const { db, warnings } = await run(listing(), { gmail: mail(...files).gmail });
        expect(list_bills(db)).toEqual([]);
        expect(warnings).toEqual([]);
      }
    });
    test('is not searched again once the bill exists, and a Gmail failure is only a warning', async () => {
      const db = open_db(':memory:'), { asked, gmail } = mail(GATEPASS);
      await run(listing(), { gmail }, db); await run(listing(), { gmail }, db);
      expect(asked).toEqual(['7546404 2026-10-08']);
      const failing = async function* (): AsyncGenerator<Uint8Array> { throw new GmailError('Gmail lookup failed'); };
      const { db: empty, warnings } = await run(listing(), { gmail: failing });
      expect(list_bills(empty)).toEqual([]);
      expect(warnings).toEqual(['SB 7546404: Gmail lookup failed']);
    });
  });
  test('a failed month is a warning and the other months still run', async () => {
    const { db, warnings } = await run(fake({ gatepassWindow: async from => { if (from.startsWith('2026-09')) throw new IcegateError('slow'); return []; } }), { from: '2026-09-01' });
    expect(warnings).toEqual(['2026-09: slow']);
    expect(list_bills(db)).toHaveLength(1);
  });
  test('complete bills are skipped before any portal call', async () => {
    const db = open_db(':memory:'); named(db);
    const listed = fake({ sbcopySearch: async () => [{ irn: 'IRN9', jobNumber: '953', docNumber: '7546404', location: 'INMBD6' }] });
    await run(listed, {}, db);
    const client = fake();
    const { results } = await run(client, {}, db);
    expect(results).toEqual([]);
    expect(client.calls).not.toContain('enquiry');
    expect(client.calls).not.toContain('search');
  });
  test('a complete bill without its job counter is revisited for the SB e-copy listing only', async () => {
    const db = open_db(':memory:'); named(db);
    await run(fake(), {}, db);
    const client = fake({ sbcopySearch: async () => [{ irn: 'IRN9', jobNumber: '953', docNumber: '7546404', location: 'INMBD6' }] });
    await run(client, {}, db);
    expect(get_bill(db, k)!.sb_job_no).toBe('953');
    expect(client.calls).not.toContain('enquiry');
    expect(client.calls).not.toContain('download');
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
    const { results, db } = await run(fake({ ecopyDownload: async () => { throw new NotReady('retry in about 60 minutes'); } }));
    expect(results[0]![1]).toMatchObject({ status: 'Partial' });
    expect(results[0]![1].detail).toContain('gatepass not ready');
    expect(get_bill(db, k)!.exporter).toBe('');
  });
  test('gatepass counts distinguish new sources, saved copies, misses and errors', () => {
    expect(gatepass_counts([{status:'Success',detail:'',gatepass:'gmail'}, {status:'Partial',detail:'',gatepass:'unavailable'}, {status:'Success',detail:'',gatepass:'saved'}, {status:'Error',detail:'',gatepass:'error'}])).toEqual({icegate:0,gmail:1,saved:1,unavailable:1,error:1});
  });
  test('reports the gatepass source and unavailable result per bill', async () => {
    const portal = await run(fake());
    expect(portal.results[0]![1].gatepass).toBe('icegate');
    const gmail = async function* () { yield GATEPASS; };
    const mail = await run(fake({gatepassSearch: async () => []}), {gmail});
    expect(mail.results[0]![1].gatepass).toBe('gmail');
    const missing = await run(fake({gatepassSearch: async () => []}));
    expect(missing.results[0]![1].gatepass).toBe('unavailable');
  });
  test('Gmail fallback skips mismatched PDFs and uses a matching gatepass when ICEGATE has no IRN', async () => {
    const wrong = pdf([['SB No',20,760],['9999999',100,760],['SB Date',200,760],['07/10/2026',280,760],['Port Code',400,760],['INMBD6',480,760], ...TABLE]);
    const gmail = async function* () { yield wrong; yield GATEPASS; };
    const {db, results} = await run(fake({gatepassSearch: async () => []}), {gmail});
    expect(results[0]![1].status).toBe('Success');
    expect(get_bill(db,k)!.exporter).toBe('GATEPASS EXPORTS LTD');
  });
  test('Gmail can supply a gatepass when the enquiry has no LEO date', async () => {
    const gmail = async function* () { yield GATEPASS; };
    const client = fake({enquiry: async () => ({...EMPTY, sbDetailsModel:[{}]})});
    const {db} = await run(client, {gmail});
    expect(get_bill(db,k)!.exporter).toBe('GATEPASS EXPORTS LTD');
    expect(client.calls).not.toContain('search');
  });
  test('Gmail fallback recovers when the portal download fails', async () => {
    const gmail = async function* () { yield GATEPASS; };
    const {results} = await run(fake({ecopyDownload: async () => { throw new NotReady('retry later'); }}), {gmail});
    expect(results[0]![1].status).toBe('Success');
  });
  test('Gmail candidates that do not match the bill are not kept', async () => {
    const gmail = async function* () { yield pdf([['SB No',20,760],['9999999',100,760]]); };
    const dir = mkdtempSync(join(tmpdir(), 'gatepass-rejected-'));
    const {results} = await run(fake({gatepassSearch: async () => []}), {gmail, gatepass_dir:dir});
    expect(results[0]![1].status).toBe('Partial');
    expect(existsSync(join(dir, '2026-10-07_7546404.pdf'))).toBe(false);
  });
  test('Gmail failure is reported without exposing message content', async () => {
    const gmail = async function* (): AsyncGenerator<Uint8Array> { throw new GmailError('Gmail lookup failed'); };
    const {results} = await run(fake({gatepassSearch: async () => []}), {gmail});
    expect(results[0]![1].detail).toContain('Gmail lookup failed');
    expect(results[0]![1].status).toBe('Partial');
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
    const db = open_db(':memory:'), client = fake({ jobStatus: async () => [], enquiry: async () => EMPTY, gatepassWindow: async () => [{ docNumber: '7546404', docDate: '07-10-2026', jobDate: '07-10-2026', location: 'INMBD6' }] });
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
    expect([...lcl].map(x => x.split('|')[2]).sort()).toEqual(['SHARED1', 'SHARED1']);
    expect(cells_for(db, k, DEFAULT_MAP, lcl)['Type Shipment (derived)']!.value).toBe('LCL');
    expect(cells_for(db, alone, DEFAULT_MAP, lcl)['Type Shipment (derived)']).toBeUndefined();
  });
  test('the own-job column is blank until typed and marks the sheet so Excel colours the SB Job No where it says Y', () => {
    const db = open_db(':memory:'); fill_bill(db, k, {});
    expect(cells_for(db, k, DEFAULT_MAP, new Set())['Remark(Own job Y/N)']).toBeUndefined();
    expect(report_sheets(db, DEFAULT_MAP)[0]!.highlight).toEqual({ flag: 'Remark(Own job Y/N)', target: 'SB Job No' });
    expect(report_sheets(db, DEFAULT_MAP)[0]!.derived).toContain('Remark(Bill no.)');
  });
  test('remarks typed into the workbook survive the rewrite, a cleared cell clears the note, and the previous file stays as .bak', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx'), db = open_db(':memory:'), other = { sb_no: '7546499', sb_date: '2026-10-08', port_code: 'INMBD6' };
    fill_bill(db, k, { exporter: 'A' }); fill_bill(db, other, {});
    await publish_report(db, path, report_sheets(db, DEFAULT_MAP), DEFAULT_MAP);
    // the user opens the file, types remarks and saves it
    const typed = report_sheets(db, DEFAULT_MAP).map(s => ({ ...s, rows: s.rows.map((r, i) => ({ ...r, 'Remark(Bill no.)': { value: i === 0 ? 'AG-1 & co' : 'AG-2' }, ...(i === 0 ? { 'Remark(Own job Y/N)': { value: 'Y' } } : {}) })) }));
    await write_report(path, typed);
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(3);
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(0); // reading the same file again changes nothing
    await publish_report(db, path, report_sheets(db, DEFAULT_MAP), DEFAULT_MAP);
    const text = (await read_sheets(path))![0]!;
    expect(text.rows.map(r => r['Remark(Bill no.)'])).toEqual(['AG-1 & co', 'AG-2']);
    expect(text.rows.map(r => r['Remark(Own job Y/N)'])).toEqual(['Y', '']);
    expect(existsSync(`${path}.bak`)).toBe(true);
    // clearing a cell in the workbook clears the note
    await write_report(path, report_sheets(db, DEFAULT_MAP).map(s => ({ ...s, rows: s.rows.map(r => ({ ...r, 'Remark(Bill no.)': { value: '' } })) })));
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(2);
    expect(notes_of(db, k)).toEqual({ own_job: 'Y' });
  });
  test('a workbook that is out of date never wipes notes the database already holds', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx'), db = open_db(':memory:');
    fill_bill(db, k, {});
    await publish_report(db, path, report_sheets(db, DEFAULT_MAP), DEFAULT_MAP); // the workbook shows no notes
    set_notes(db, k, { bill_no: 'restored' }); // notes arrive another way (an older workbook, another machine)
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(0);
    expect(notes_of(db, k)).toEqual({ bill_no: 'restored' });
  });
  test('keep_notes ignores a missing workbook, leaves notes of columns the sheet lacks, and stops on an unreadable file', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'daily-report-')), db = open_db(':memory:');
    fill_bill(db, k, {}); set_notes(db, k, { bill_no: 'keep me' });
    expect(await keep_notes(db, DEFAULT_MAP, join(dir, 'none.xlsx'))).toBe(0);
    const narrow = { 'Shipping Bill No': 'bills.sb_no', 'Shipping Bill Date': 'bills.sb_date', 'Remark(Own job Y/N)': 'notes.own_job' };
    await write_report(join(dir, 'a.xlsx'), report_sheets(db, narrow));
    expect(await keep_notes(db, DEFAULT_MAP, join(dir, 'a.xlsx'))).toBe(0);
    expect(notes_of(db, k)).toEqual({ bill_no: 'keep me' });
    await Bun.write(join(dir, 'bad.xlsx'), 'not a zip');
    await expect(keep_notes(db, DEFAULT_MAP, join(dir, 'bad.xlsx'))).rejects.toThrow('left untouched');
    await expect(keep_notes(db, { Remark: 'notes.bill_no', Exporter: 'bills.exporter' }, join(dir, 'a.xlsx'))).rejects.toThrow('Shipping Bill No');
  });
  test('a stored stuffing override beats the seal rule', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, {}); fill_containers(db, k, [{ container_no: 'A1', seal: '0091802' }]);
    set_notes(db, k, { stuffing: 'Factory stuffing' });
    expect(cells_for(db, k, DEFAULT_MAP, new Set())['Type of Stuffing (derived)']!.value).toBe('Factory stuffing');
  });
  test('derived columns typed over in the workbook survive the rewrite; clearing the cell brings the computed value back', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx'), db = open_db(':memory:');
    fill_bill(db, k, {}); fill_containers(db, k, [{ container_no: 'A1', seal: 'BOLT52524390' }]);
    await publish_report(db, path, report_sheets(db, DEFAULT_MAP), DEFAULT_MAP);
    const edit = (cells: Record<string, string>) => write_report(path, report_sheets(db, DEFAULT_MAP).map(s => ({ ...s, rows: s.rows.map(r => ({ ...r, ...Object.fromEntries(Object.entries(cells).map(([h, v]) => [h, { value: v }])) })) })));
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(0); // the computed values themselves are not typed
    await edit({ 'Type of Stuffing (derived)': 'ICD stuffing', Status: 'Done', 'Type Shipment (derived)': 'LCL' });
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(3);
    await publish_report(db, path, report_sheets(db, DEFAULT_MAP), DEFAULT_MAP);
    const row = (await read_sheets(path))![0]!.rows[0]!;
    expect([row['Type of Stuffing (derived)'], row.Status, row['Type Shipment (derived)']]).toEqual(['ICD stuffing', 'Done', 'LCL']);
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(0);
    await edit({ 'Type of Stuffing (derived)': '', Status: '', 'Type Shipment (derived)': '' });
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(3);
    expect(notes_of(db, k)).toEqual({});
    expect(cells_for(db, k, DEFAULT_MAP, new Set())['Type of Stuffing (derived)']!.value).toBe('Factory stuffing');
  });
  test('a stale derived cell is not mistaken for a typed one, and one never written down before is left alone', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx'), db = open_db(':memory:');
    fill_bill(db, k, {}); fill_containers(db, k, [{ container_no: 'A1', seal: 'BOLT52524390' }]);
    await write_report(path, report_sheets(db, DEFAULT_MAP)); // written by an older program: nothing was snapshotted
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(0);
    await publish_report(db, path, report_sheets(db, DEFAULT_MAP), DEFAULT_MAP);
    fill_containers(db, k, [{ container_no: 'A1', seal: '0091802' }]); // new data makes the computed stuffing blank, the workbook is out of date
    expect(await keep_notes(db, DEFAULT_MAP, path)).toBe(0);
    expect(notes_of(db, k)).toEqual({});
  });
  test('the old stuffing_overrides table is moved into the notes when the database opens', () => {
    const dir = mkdtempSync(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.db');
    const first = open_db(path); fill_bill(first, k, {});
    first.run('CREATE TABLE stuffing_overrides (fy TEXT NOT NULL, sb_no TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (fy, sb_no))');
    first.run("INSERT INTO stuffing_overrides VALUES ('2026-27', ?, 'ICD stuffing')", [k.sb_no]); first.close();
    const db = open_db(path);
    expect(notes_of(db, k)).toEqual({ stuffing: 'ICD stuffing' });
    expect(db.query("SELECT 1 FROM sqlite_master WHERE name = 'stuffing_overrides'").get()).toBeNull();
  });
  test('type of stuffing is read from the seals: letters mean factory, plain numbers stay blank', () => {
    const db = open_db(':memory:'), icd = { sb_no: '7546405', sb_date: '2026-10-08', port_code: 'INMBD6' };
    fill_bill(db, k, {}); fill_bill(db, icd, {});
    fill_containers(db, k, [{ container_no: 'A1', seal: 'BOLT52524390' }]); fill_containers(db, icd, [{ container_no: 'B1', seal: '0227948' }]);
    expect(cells_for(db, k, DEFAULT_MAP, new Set())['Type of Stuffing (derived)']!.value).toBe('Factory stuffing');
    expect(cells_for(db, icd, DEFAULT_MAP, new Set())['Type of Stuffing (derived)']).toBeUndefined();
  });
  test('a container reused months later by another exporter is not LCL', () => {
    const db = open_db(':memory:'), later = { sb_no: '7546407', sb_date: '2026-12-20', port_code: 'INMBD6' };
    fill_bill(db, k, { exporter: 'A' }); fill_bill(db, later, { exporter: 'B' });
    fill_containers(db, k, [{ container_no: 'REUSED1' }]); fill_containers(db, later, [{ container_no: 'REUSED1' }]);
    expect(shared_containers(db).size).toBe(0);
  });
  test('one sheet per financial year, oldest first, bills by date', () => {
    const db = open_db(':memory:');
    for (const [no, date] of [['3', '2026-10-01'], ['2', '2026-03-31'], ['1', '2026-04-01'], ['4', '2025-05-05']]) fill_bill(db, { sb_no: no!.padStart(7, '0'), sb_date: date!, port_code: 'INMBD6' }, {});
    const sheets = report_sheets(db, DEFAULT_MAP);
    expect(sheets.map(s => [s.name, s.rows.map(r => r['Shipping Bill No']!.value)])).toEqual([['FY 2025-26', ['0000004', '0000002']], ['FY 2026-27', ['0000001', '0000003']]]);
  });
});
