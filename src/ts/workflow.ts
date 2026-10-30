/** One run: Job Status, My IEC and the gatepass months create bills → enquiry → gatepass → report. The only module that ties the client and the database together. */
import { existsSync } from 'node:fs';
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { field_kind, type ExportMap } from './excel/export-map';
import type { Sheet } from './excel/report';
import { parse_gatepass } from './gatepass/parse';
import { month_end, NotReady, type IcegateClient } from './icegate/client';
import { BILL_FIELDS, type BillField } from './icegate/schemas';
import { bill_identity, DERIVED_LCL, fy_of } from './rules';
import { apply_iec_names, clean, DataError, fill_bill, fill_containers, fill_invoices, gaps, get_bill, list_bills, list_containers, list_invoices, set_iec_name, shared_containers, stamp, status_of, type Bill, type BillKey, type Db } from './store/db';

export type RowStatus = 'Success' | 'Partial' | 'Cancelled' | 'Not found' | 'Error';
export interface Outcome { status: RowStatus; detail: string }
export interface SyncOptions { workers: number; excel_only: boolean; from: string; to?: string; force?: boolean; gatepass_dir: string; on_progress?: (done: number, total: number, bill: BillKey, outcome: Outcome) => void }
export interface SyncResult { results: [BillKey, Outcome][]; warnings: string[] }

const ENQUIRY_MODELS = ['sbDetailsModel', 'currentStatusModel', 'roslStatusModel', 'gatewayEgmStatusModel'] as const;
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const key_of = (b: BillKey): BillKey => ({ sb_no: b.sb_no, sb_date: b.sb_date, port_code: b.port_code });
const wants = (g: string[], ...names: string[]) => g.some(x => names.includes(x));

/** Portal columns of one source row, renamed to database columns. */
function pick(from: BillField['from'], row: Record<string, string> | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (row) for (const f of BILL_FIELDS) if (f.from === from && row[f.key] !== undefined) out[f.col] = row[f.key]!;
  return out;
}

/** `yyyy-mm` months from the month of `from` up to the month of `to` (default: today in India). */
function months(from: string, to?: string): string[] {
  const last = (to ?? new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })).slice(0, 7), out: string[] = [];
  for (let [y, m] = [+from.slice(0, 4), +from.slice(5, 7)]; `${y}-${String(m).padStart(2, '0')}` <= last; m === 12 ? (y++, m = 1) : m++) out.push(`${y}-${String(m).padStart(2, '0')}`);
  return out;
}

async function pool<T>(items: T[], size: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, async () => { while (next < items.length) await fn(items[next++]!); }));
}

/** Job Status and My IEC (a failure stops the run before anything is written), then every gatepass month since `from`, which is what creates bills. A failed month is a warning. */
export async function discover(db: Db, client: IcegateClient, from: string, to?: string): Promise<string[]> {
  const [rows, ieces] = await Promise.all([client.jobStatus(), client.myIec()]), warnings: string[] = [];
  for (const row of rows) {
    let k: BillKey;
    try { k = bill_identity(row['SB No']!, row['SB date']!, row['Location Code']!); } catch { continue; }
    try { fill_bill(db, k, pick('job', row)); stamp(db, k, 'job'); } catch (error) { if (!(error instanceof DataError)) throw error; warnings.push(`SB ${k.sb_no}: ${error.message}`); }
  }
  for (const m of ieces) if (clean(m.iecCode) && clean(m.iecName)) set_iec_name(db, m.iecCode.trim(), m.iecName.trim(), 'my-iec');
  await pool(months(from, to), 3, async month => {
    try {
      for (const r of await client.gatepassWindow(`${month}-01`, month_end(month))) {
        let k: BillKey;
        try { k = bill_identity(r.docNumber!, r.docDate!, r.location!); } catch { continue; }
        try { fill_bill(db, k, {}); } catch (error) { if (!(error instanceof DataError)) throw error; warnings.push(`SB ${k.sb_no}: ${error.message}`); }
      }
    } catch (error) { warnings.push(`${month}: ${message(error)}`); }
  });
  return warnings;
}

/** Public enquiry for one bill. Returns false when the portal knows nothing about it. */
async function enrich(db: Db, client: IcegateClient, k: BillKey): Promise<boolean> {
  const e = await client.enquiry({ location: k.port_code, sb_no: k.sb_no, sb_date: k.sb_date });
  fill_bill(db, k, Object.assign({}, ...ENQUIRY_MODELS.map(m => pick(m, e[m][0]))));
  fill_invoices(db, k, e.igstInvoiceDetailsModel.map(r => ({ invoice_no: r.invoiceNo ?? '', invoice_date: r.invoiceDate, igst_value: r.igstValueCustom })));
  fill_containers(db, k, e.egmStatusModel.map(r => ({ container_no: r.containerNo ?? '', seal: r.sealNo, egm_no: r.egmNo, egm_date: r.egmDate })));
  const known = e.sbDetailsModel.length > 0 || e.currentStatusModel.length > 0;
  if (known) stamp(db, k, 'enquiry'); // an empty answer is not remembered, so the bill stays Not found
  return known;
}

/** Parse the SB35 gatepass: containers, the IEC and, for a bill without an IEC name, the exporter. A kept PDF is parsed again without the portal; otherwise search, download and keep it. Returns a note when there is nothing to parse yet. */
async function gatepass(db: Db, client: IcegateClient, k: BillKey, keep: string): Promise<string> {
  const kept = join(keep, `${k.sb_date}_${k.sb_no}.pdf`);
  let bytes: Uint8Array;
  if (existsSync(kept)) bytes = new Uint8Array(await readFile(kept));
  else {
    const record = (await client.gatepassSearch({ location: k.port_code, sb_no: k.sb_no, sb_date: k.sb_date })).filter(r => r.irn).at(-1);
    if (!record) return 'no gatepass yet';
    const dir = await mkdtemp(join(tmpdir(), 'daily-report-'));
    try {
      const file = join(dir, 'gatepass.pdf');
      await client.gatepassDownload(record, file);
      bytes = new Uint8Array(await readFile(file));
    } finally { await rm(dir, { recursive: true, force: true }); }
  }
  const pass = parse_gatepass(bytes, k);
  if (!existsSync(kept)) { // the parsed PDF is kept as the source of the stored fields
    await mkdir(keep, { recursive: true, mode: 0o700 });
    await writeFile(kept, bytes, { mode: 0o600 }); await chmod(kept, 0o600);
  }
  fill_bill(db, k, { iec: pass.iec, exporter: pass.exporter }); // first non-empty wins: the Job Status IEC and an IEC-derived name are kept
  fill_containers(db, k, pass.containers);
  stamp(db, k, 'gatepass');
  return '';
}

/** Fetch what one bill is still missing and judge it. Failures are reported on the bill, never thrown. */
async function work(db: Db, client: IcegateClient | null, k: BillKey, keep: string, force = false): Promise<Outcome> {
  const notes: string[] = [], errors: string[] = [];
  const before = get_bill(db, k);
  let known = !!(before?.fetched_job || before?.fetched_enquiry);
  if (client) {
    if (force || wants(gaps(db, k), 'leo_date', 'invoice', 'container')) {
      try { known = (await enrich(db, client, k)) || known; } catch (error) { errors.push(`enquiry: ${message(error)}`); }
    }
    if (status_of(db, k) !== 'Cancelled' && known && get_bill(db, k)?.leo_date && (force || wants(gaps(db, k), 'container', 'size', 'seal', 'exporter', 'iec'))) { // no LEO, no SB35 gatepass
      try { const note = await gatepass(db, client, k, keep); if (note) notes.push(note); }
      catch (error) { if (error instanceof NotReady) notes.push('gatepass not ready'); else errors.push(`gatepass: ${message(error)}`); }
    }
  }
  const status = status_of(db, k);
  if (status === 'Cancelled') return { status, detail: 'Cancelled at the portal' };
  const missing = gaps(db, k);
  if (!missing.length) return { status: 'Success', detail: 'All fields filled' };
  if (errors.length) return { status: 'Error', detail: errors.join('; ') };
  if (status === 'Not found') return { status, detail: 'Not known to the portal' };
  return { status: 'Partial', detail: [`missing: ${missing.join(', ')}`, ...notes].join('; ') };
}

/** Bills that can still gain something: not complete and not cancelled, unless the run is forced. */
const pending = (db: Db, b: Bill, force = false) => { const s = status_of(db, b); return force || (s !== 'Success' && s !== 'Cancelled'); };

export async function sync(db: Db, client: IcegateClient, o: SyncOptions): Promise<SyncResult> {
  const warnings = o.excel_only ? [] : await discover(db, client, o.from, o.to);
  apply_iec_names(db); // before any gatepass, so an IEC-derived name outranks the gatepass exporter
  const todo = list_bills(db).filter(b => (!o.to || (b.sb_date >= o.from && b.sb_date <= o.to)) && pending(db, b, o.force)).map(key_of);
  if (!o.excel_only) client.plan(todo.filter(k => o.force || wants(gaps(db, k), 'container', 'size', 'seal', 'exporter', 'iec')).map(k => k.sb_date));
  const results: [BillKey, Outcome][] = [];
  await pool(todo, o.workers, async k => {
    const outcome = await work(db, o.excel_only ? null : client, k, o.gatepass_dir, o.force);
    results.push([k, outcome]);
    o.on_progress?.(results.length, todo.length, k, outcome);
  });
  return { results: results.sort((a, b) => a[0].sb_date.localeCompare(b[0].sb_date) || a[0].sb_no.localeCompare(b[0].sb_no)), warnings };
}

/** The export values for one bill, keyed by report header. Multi-row fields are newline-joined, blanks kept in place. */
export function cells_for(db: Db, k: BillKey, map: ExportMap, lcl: Set<string>): Sheet['rows'][number] {
  const bill = get_bill(db, k), cells: Sheet['rows'][number] = {};
  const lists: Record<string, Record<string, string>[]> = { invoices: list_invoices(db, k), containers: list_containers(db, k) };
  for (const [header, id] of Object.entries(map)) {
    const [table, col] = id.split('.') as [string, string];
    const value = id === 'derived.status' ? status_of(db, k)
      : id === 'derived.type_shipment' ? (lists.containers!.some(c => lcl.has(c.container_no!)) ? DERIVED_LCL : '')
      : table === 'bills' ? bill?.[col] ?? '' : (lists[table] ?? []).map(r => r[col] ?? '').join('\n');
    if (value.trim()) cells[header] = { value, date: field_kind(id) === 'date' };
  }
  return cells;
}

/** One sheet per financial year (`FY yyyy-yy`), oldest year first, each sorted by shipping bill date. */
export function report_sheets(db: Db, map: ExportMap): Sheet[] {
  const lcl = shared_containers(db), years = new Map<string, Bill[]>();
  for (const b of list_bills(db).sort((a, b) => a.sb_date.localeCompare(b.sb_date) || a.sb_no.localeCompare(b.sb_no))) years.set(fy_of(b.sb_date), [...(years.get(fy_of(b.sb_date)) ?? []), b]);
  return [...years].sort(([a], [b]) => a.localeCompare(b)).map(([fy, bills]) => ({ name: `FY ${fy}`, headers: Object.keys(map), rows: bills.map(b => cells_for(db, b, map, lcl)) }));
}
