/** One run: Job Status, My IEC and the gatepass months create bills → enquiry → gatepass → report. The only module that ties the client and the database together. */
import { existsSync } from 'node:fs';
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { field_kind, type ExportMap } from './excel/export-map';
import { read_sheets } from './excel/read';
import { write_report, type Sheet } from './excel/report';
import { gatepass_identity, parse_gatepass, parse_sbcopy } from './gatepass/parse';
import { GmailError, type BillMail } from './gmail';
import { month_end, NotReady, type EcopyRecord, type IcegateClient } from './icegate/client';
import { BILL_FIELDS, is_typed, typed_field, type BillField } from './icegate/schemas';
import { agent_address, bill_identity, DERIVED_LCL, excel_date, fy_of, job_tokens, MAIL_FROM, own_job_of, parse_date, plausible_sb_date, sb_number, stuffing_of } from './rules';
import { apply_iec_names, clean, DataError, fill_bill, fill_containers, fill_invoices, fill_notes_from_mail, gaps, mail_checked, get_bill, list_bills, note_key, notes_of, set_notes, snapshot_notes, list_containers, lcl_key, list_invoices, set_iec_name, shared_containers, stamp, status_of, written_notes, type Bill, type BillKey, type BillRef, type Db } from './store/db';

export type RowStatus = 'Success' | 'Partial' | 'Cancelled' | 'Manual' | 'Not found' | 'Error';
export type GatepassResult = 'saved' | 'icegate' | 'gmail' | 'unavailable' | 'error';
export interface Outcome { status: RowStatus; detail: string; gatepass?: GatepassResult }
export interface SyncOptions { workers: number; excel_only: boolean; from: string; to?: string; force?: boolean; gatepass_dir: string; gmail?: (bill: BillKey) => AsyncGenerator<Uint8Array>; mail?: (bill: BillKey) => Promise<BillMail>; on_progress?: (done: number, total: number, bill: BillKey, outcome: Outcome) => void }
export interface SyncResult { results: [BillKey, Outcome][]; warnings: string[] }

/** Counts describe this run; previously saved files have unknown original provenance. */
export function gatepass_counts(outcomes: Outcome[]): Record<GatepassResult, number> {
  const counts: Record<GatepassResult, number> = {icegate:0, gmail:0, saved:0, unavailable:0, error:0};
  for (const outcome of outcomes) if (outcome.gatepass) counts[outcome.gatepass]++;
  return counts;
}

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

type GmailSource = (bill: BillKey) => AsyncGenerator<Uint8Array>;

/** Keeps the parsed gatepass as the source of the stored fields, so later runs parse it again instead of searching. */
async function keep_pdf(keep: string, k: BillKey, bytes: Uint8Array): Promise<void> {
  const file = join(keep, `${k.sb_date}_${k.sb_no}.pdf`);
  if (existsSync(file)) return;
  await mkdir(keep, { recursive: true, mode: 0o700 });
  await writeFile(file, bytes, { mode: 0o600 }); await chmod(file, 0o600);
}

/** A gatepass record listed with a placeholder SB date has no usable date and no IRN to download. Its PDF, found in Gmail by SB number and the job date's financial year, names the real bill: that date is the bill's. Returns null when no PDF passes. */
async function placeholder_bill(db: Db, r: Record<string, string | null | undefined>, gmail: GmailSource, keep: string): Promise<BillKey | null> {
  const listed = bill_identity(r.docNumber!, r.docDate!, r.location!);
  if (db.query('SELECT 1 FROM bills WHERE sb_no = ? AND port_code = ?').get(listed.sb_no, listed.port_code)) return null; // already created, by Job Status or an earlier run
  for await (const candidate of gmail({ ...listed, sb_date: parse_date(r.jobDate!) })) {
    try {
      const named = gatepass_identity(candidate), k = bill_identity(named.sb_no, named.sb_date, named.port_code);
      if (k.sb_no !== listed.sb_no || k.port_code !== listed.port_code || !plausible_sb_date(k.sb_date, r.jobDate!)) continue;
      parse_gatepass(candidate, k);
      fill_bill(db, k, {}); await keep_pdf(keep, k, candidate);
      return k;
    } catch { /* Try the next attachment. */ }
  }
  return null;
}

/** Job Status and My IEC (a failure stops the run before anything is written), then every gatepass month since `from`, which is what creates bills. A failed month is a warning. */
export async function discover(db: Db, client: IcegateClient, from: string, to?: string, gmail?: GmailSource, keep = ''): Promise<string[]> {
  const rows = await client.jobStatus(), ieces = await client.myIec(), warnings: string[] = []; // one login at a time: the portal answers 400 to simultaneous ones
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
        try {
          k = bill_identity(r.docNumber!, r.docDate!, r.location!);
          if (!plausible_sb_date(k.sb_date, r.jobDate!)) { if (gmail) await placeholder_bill(db, r, gmail, keep); continue; }
        } catch (error) { if (error instanceof GmailError) warnings.push(`SB ${r.docNumber}: ${error.message}`); continue; }
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

/** The signed PDF of an e-copy record, read into memory; the temporary file never outlives the call. */
async function download(client: IcegateClient, record: EcopyRecord): Promise<Uint8Array> {
  const dir = await mkdtemp(join(tmpdir(), 'daily-report-'));
  try { const file = join(dir, 'ecopy.pdf'); await client.ecopyDownload(record, file); return new Uint8Array(await readFile(file)); }
  finally { await rm(dir, { recursive: true, force: true }); }
}

/** The SB e-copy listing is available from filing day. Its `jobNumber` (a daily counter, 953 for the 953rd job of the day) is stored for every bill; the PDF is read only when IEC or exporter is still blank and then only those two values are kept. False when the portal lists no copy. */
async function sbcopy(db: Db, client: IcegateClient, k: BillKey, names: boolean): Promise<boolean> {
  const record = (await client.sbcopySearch({ location: k.port_code, sb_no: k.sb_no, sb_date: k.sb_date })).at(-1);
  if (!record) return false;
  fill_bill(db, k, { sb_job_no: clean(record.jobNumber ?? '') });
  if (names && record.irn && wants(gaps(db, k), 'iec', 'exporter')) {
    const copy = parse_sbcopy(await download(client, record), k);
    fill_bill(db, k, { iec: copy.iec, exporter: copy.exporter });
  }
  return true;
}

/** Parse the SB35 gatepass: containers, the IEC, the exporter and the facilitation category. A kept PDF is parsed again without the portal; otherwise search, download and keep it. Returns where the PDF came from, or `unavailable`. */
async function gatepass(db: Db, client: IcegateClient, k: BillKey, keep: string, gmail?: (bill: BillKey) => AsyncGenerator<Uint8Array>, portal = true): Promise<GatepassResult> {
  const kept = join(keep, `${k.sb_date}_${k.sb_no}.pdf`);
  let bytes: Uint8Array | undefined;
  let pass: ReturnType<typeof parse_gatepass> | undefined;
  let portalError: unknown;
  let source: GatepassResult = 'saved';
  if (existsSync(kept)) {
    bytes = new Uint8Array(await readFile(kept));
    pass = parse_gatepass(bytes, k);
  } else {
    try {
      const record = portal ? (await client.gatepassSearch({ location: k.port_code, sb_no: k.sb_no, sb_date: k.sb_date })).filter(r => r.irn).at(-1) : undefined;
      if (record) { const candidate = await download(client, record); pass = parse_gatepass(candidate, k); bytes = candidate; source = 'icegate'; }
    } catch (error) { portalError = error; }
    if (!pass && gmail) {
      for await (const candidate of gmail(k)) {
        try { pass = parse_gatepass(candidate, k); bytes = candidate; source = 'gmail'; break; } catch { /* Try the next attachment. */ }
      }
    }
    if (!pass || !bytes) {
      if (portalError) throw portalError;
      return 'unavailable';
    }
  }
  await keep_pdf(keep, k, bytes);
  fill_bill(db, k, { iec: pass.iec, exporter: pass.exporter, facilitation: pass.facilitation }); // first non-empty wins: the Job Status IEC and an IEC-derived name are kept
  fill_containers(db, k, pass.containers);
  stamp(db, k, 'gatepass');
  return source;
}

/** Fetch what one bill is still missing and judge it. Failures are reported on the bill, never thrown. */
async function work(db: Db, client: IcegateClient | null, k: BillKey, keep: string, force = false, gmail?: (bill: BillKey) => AsyncGenerator<Uint8Array>): Promise<Outcome> {
  const notes: string[] = [], errors: string[] = [];
  let gatepassResult: GatepassResult | undefined;
  const before = get_bill(db, k);
  let known = !!(before?.fetched_job || before?.fetched_enquiry);
  if (client) {
    if (force || wants(gaps(db, k), 'leo_date', 'invoice', 'container')) {
      try { known = (await enrich(db, client, k)) || known; } catch (error) { errors.push(`enquiry: ${message(error)}`); }
    }
    if (status_of(db, k) !== 'Cancelled' && known && (get_bill(db, k)?.leo_date || gmail) && (force || wants(gaps(db, k), 'container', 'size', 'seal', 'exporter', 'iec'))) {
      try { gatepassResult = await gatepass(db, client, k, keep, gmail, !!get_bill(db, k)?.leo_date); if (gatepassResult === 'unavailable') notes.push('no matching gatepass'); }
      catch (error) { gatepassResult = error instanceof NotReady ? 'unavailable' : 'error'; if (error instanceof NotReady) notes.push('gatepass not ready'); else if (error instanceof GmailError) notes.push(error.message); else errors.push(`gatepass: ${message(error)}`); }
    }
    const names = status_of(db, k) !== 'Cancelled' && wants(gaps(db, k), 'iec', 'exporter');
    if (known && status_of(db, k) !== 'Manual' && (names || !get_bill(db, k)?.sb_job_no)) { // the gatepass is preferred for IEC and exporter; the SB e-copy listing supplies the job counter
      try { if (!(await sbcopy(db, client, k, names))) notes.push('no SB e-copy'); }
      catch (error) { if (error instanceof NotReady) notes.push('SB e-copy not ready'); else errors.push(`SB e-copy: ${message(error)}`); }
    }
  }
  const status = status_of(db, k);
  if (status === 'Cancelled') return { status, detail: 'Classified as cancelled from portal status', gatepass: gatepassResult };
  const missing = gaps(db, k);
  if (status === 'Manual') return { status, detail: `closed by hand; missing: ${missing.join(', ')}`, gatepass: gatepassResult };
  if (!missing.length) return { status: 'Success', detail: 'All fields filled', gatepass: gatepassResult };
  if (errors.length) return { status: 'Error', detail: errors.join('; '), gatepass: gatepassResult };
  if (status === 'Not found') return { status, detail: 'Not known to the portal' };
  return { status: 'Partial', detail: [`missing: ${missing.join(', ')}`, ...notes].join('; '), gatepass: gatepassResult };
}

/** Bills that can still gain something: not complete, cancelled or closed by hand, unless the run is forced. A bill without its SB job counter is also revisited, except one closed by hand. */
const pending = (db: Db, b: Bill, force = false) => { const s = status_of(db, b); return force || (s !== 'Success' && s !== 'Cancelled' && s !== 'Manual') || (s !== 'Manual' && !b.sb_job_no); };

export async function sync(db: Db, client: IcegateClient, o: SyncOptions): Promise<SyncResult> {
  const warnings = o.excel_only ? [] : await discover(db, client, o.from, o.to, o.gmail, o.gatepass_dir);
  apply_iec_names(db); // before any gatepass, so an IEC-derived name outranks the gatepass exporter
  const todo = list_bills(db).filter(b => (!o.to || (b.sb_date >= o.from && b.sb_date <= o.to)) && pending(db, b, o.force)).map(key_of);
  if (!o.excel_only) client.plan(todo.filter(k => o.force || wants(gaps(db, k), 'container', 'size', 'seal', 'exporter', 'iec')).map(k => k.sb_date));
  const results: [BillKey, Outcome][] = [];
  await pool(todo, o.workers, async k => {
    const outcome = await work(db, o.excel_only ? null : client, k, o.gatepass_dir, o.force, o.gmail);
    results.push([k, outcome]);
    o.on_progress?.(results.length, todo.length, k, outcome);
  });
  if (!o.excel_only && o.mail) warnings.push(...await read_mail(db, o.mail, o.workers));
  return { results: results.sort((a, b) => a[0].sb_date.localeCompare(b[0].sb_date) || a[0].sb_no.localeCompare(b[0].sb_no)), warnings };
}

/** From FY 2026-27, once per bill with a LEO date that is not cancelled: own job and agent address from Gmail, written only where the owner has typed nothing. A Gmail failure stops this step with one warning; the bills stay unread for the next run. */
async function read_mail(db: Db, mail: (bill: BillKey) => Promise<BillMail>, workers: number): Promise<string[]> {
  const done = mail_checked(db), todo = list_bills(db).filter(b => b.sb_date >= MAIL_FROM && b.leo_date && status_of(db, b) !== 'Cancelled' && !done.has(`${fy_of(b.sb_date)}|${b.sb_no}`)), warnings: string[] = [];
  let stopped = false;
  await pool(todo, Math.min(workers, 4), async b => {
    if (stopped) return;
    try {
      const found = await mail(key_of(b));
      fill_notes_from_mail(db, b, { own_job: own_job_of(job_tokens(found.filenames), found.has_mail, true), agent_email: agent_address(found.sent_to, found.sent_cc) });
    } catch (error) { if (!stopped) warnings.push(`Gmail mail evidence stopped: ${message(error)}`); stopped = true; }
  });
  return warnings;
}

/** The typed-in columns (remarks, and the derived columns) exist only in the workbook, so before it is rewritten they are read back into the database. Only a cell that differs from what the workbook showed when this program last wrote it counts as typed: a cleared cell clears the note (a derived column then shows its computed value again), while a workbook that is merely out of date changes nothing. A derived cell the program never wrote down before cannot be told from a typed one and is left alone once. A bill is found by its SB number and date; a sheet without a column leaves that note alone. A workbook that cannot be read throws, so it is never overwritten. Returns how many notes changed. */
export async function keep_notes(db: Db, map: ExportMap, path: string): Promise<number> {
  const fields = Object.entries(map).filter(([, id]) => is_typed(id));
  const header_of = (id: string) => Object.keys(map).find(h => map[h] === id);
  const sheets = fields.length ? await read_sheets(path) : null;
  if (!sheets) return 0;
  const no = header_of('bills.sb_no'), date = header_of('bills.sb_date'), written = written_notes(db);
  if (!no || !date) throw new Error('export-map.json needs the Shipping Bill No and Shipping Bill Date columns so typed values can be matched to their bills');
  let changed = 0;
  for (const sheet of sheets) {
    if (!sheet.headers.includes(no) || !sheet.headers.includes(date)) continue;
    const present = fields.filter(([header]) => sheet.headers.includes(header));
    for (const row of sheet.rows) {
      let k: BillRef;
      try { k = { sb_no: sb_number(row[no]!), sb_date: excel_date(row[date]!) }; } catch { continue; }
      const typed = present.filter(([header, id]) => {
        const was = written.get(note_key(k, typed_field(id)));
        return (was !== undefined || id.startsWith('notes.')) && (row[header] ?? '').trim() !== (was ?? '');
      });
      if (typed.length) changed += set_notes(db, k, Object.fromEntries(typed.map(([header, id]) => [typed_field(id), row[header] ?? ''])));
    }
  }
  return changed;
}

/** Writes the workbook and remembers what its typed-in columns show (see `keep_notes`). */
export async function publish_report(db: Db, path: string, sheets: Sheet[], map: ExportMap): Promise<void> {
  await write_report(path, sheets);
  const header_of = (id: string) => Object.keys(map).find(h => map[h] === id), no = header_of('bills.sb_no'), date = header_of('bills.sb_date');
  const fields = Object.entries(map).filter(([, id]) => is_typed(id));
  snapshot_notes(db, no && date ? sheets.flatMap(sheet => sheet.rows.flatMap(row => fields.map(([header, id]) => [{ sb_no: row[no]?.value ?? '', sb_date: row[date]?.value ?? '' }, typed_field(id), (row[header]?.value ?? '').trim()] as [BillRef, string, string]))) : []);
}

/** The export values for one bill, keyed by report header. Multi-row fields are newline-joined, blanks kept in place. */
export function cells_for(db: Db, k: BillKey, map: ExportMap, lcl: Set<string>): Sheet['rows'][number] {
  const bill = get_bill(db, k), cells: Sheet['rows'][number] = {};
  const lists: Record<string, Record<string, string>[]> = { invoices: list_invoices(db, k), containers: list_containers(db, k) }, notes = notes_of(db, k);
  for (const [header, id] of Object.entries(map)) {
    const [table, col] = id.split('.') as [string, string];
    const value = is_typed(id) && notes[col] !== undefined ? notes[col]! // typed in the workbook: beats what would be computed
      : id === 'derived.status' ? status_of(db, k)
      : id === 'derived.stuffing' ? stuffing_of(lists.containers!.map(c => c.seal ?? ''))
      : id === 'derived.type_shipment' ? (lists.containers!.some(c => lcl.has(lcl_key(c))) ? DERIVED_LCL : '')
      : table === 'notes' ? ''
      : table === 'bills' ? bill?.[col] ?? '' : (lists[table] ?? []).map(r => r[col] ?? '').join('\n');
    if (value.trim()) cells[header] = { value, date: field_kind(id) === 'date' };
  }
  return cells;
}

/** One sheet per financial year (`FY yyyy-yy`), oldest year first, each sorted by shipping bill date. */
export function report_sheets(db: Db, map: ExportMap): Sheet[] {
  const lcl = shared_containers(db), years = new Map<string, Bill[]>();
  for (const b of list_bills(db).sort((a, b) => a.sb_date.localeCompare(b.sb_date) || a.sb_no.localeCompare(b.sb_no))) years.set(fy_of(b.sb_date), [...(years.get(fy_of(b.sb_date)) ?? []), b]);
  const header_of = (id: string) => Object.keys(map).find(h => map[h] === id), flag = header_of('notes.own_job'), target = header_of('bills.sb_job_no');
  return [...years].sort(([a], [b]) => a.localeCompare(b)).map(([fy, bills]) => ({ name: `FY ${fy}`, headers: Object.keys(map), derived: Object.keys(map).filter(h => is_typed(map[h]!)), ...(flag && target ? { highlight: { flag, target } } : {}), rows: bills.map(b => cells_for(db, b, map, lcl)) }));
}
