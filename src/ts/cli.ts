/** Command-line entry point: tagged output over the ICEGATE sync into SQLite and the Excel export. */
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { parseArgs } from 'node:util';
import pkg from '../../package.json';
import { load_map } from './excel/export-map';
import { write_report } from './excel/report';
import { command_client } from './icegate/client';
import { gmail_gatepasses } from './gmail';
import { CATALOG } from './icegate/schemas';
import { parse_date } from './rules';
import { gaps, list_bills, mark_manual, missing_iecs, open_db, set_iec_name, status_of, type BillKey, type Db } from './store/db';
import { gatepass_counts, report_sheets, sync, type RowStatus } from './workflow';

export const ROOT = join(import.meta.dir, '../..');

const USAGE = `daily-report ${pkg.version} - fetch ICEGATE data into SQLite, then write report.xlsx

Usage: bun run sync [options]
       bun run sync iec-missing          IECs that still need an exporter name
       bun run sync iec-set IEC "NAME"   Approve the exporter name for an IEC
       bun run sync mark-manual SBNO DATE Never query this bill again (closed by hand)
       bun run sync fields               Fields that can be exported

Plain "bun run sync" creates bills from ICEGATE (Job Status plus the gatepass months), fetches
what unfinished bills lack into report.db, then rewrites report.xlsx from the database.

  -c, --check            List bills with missing fields (read-only, no network)
  -n, --dry-run          Do everything except write report.xlsx
      --excel            Only rewrite report.xlsx from what report.db already holds (no ICEGATE calls)
      --to YYYY-MM-DD    Stop discovery at this month and fetch only bills dated from --from to this day
      --force            Also query Success, Cancelled and Manual bills (normally never queried again)
      --from YYYY-MM-DD  Discover bills from this month on (default: the month before the newest stored bill, else 2023-03-01)
  -f, --file PATH        Report (default: $EXCEL_PATH or "report.xlsx")
  -d, --data-dir PATH    Folder of report.db (default: $DAILY_REPORT_DATA_DIR or ./data)
  -j, --workers N        Concurrent bills, 1-8 (default 6)
  -v, --verbose          Show full details
  -q, --quiet            Only errors and the final summary
      --no-color         Disable colors (NO_COLOR is honored)
  -V, --version          Print the version
  -h, --help             Show this help

Exit status is 1 if any bill errored.`;

const paint = (code: number, text: string) => process.stdout.isTTY && !process.env.NO_COLOR ? `\x1b[${code}m${text}\x1b[0m` : text;
const STATUS_COLOR: Record<RowStatus, number> = { Success: 32, Partial: 33, 'Not found': 36, Cancelled: 90, Manual: 35, Error: 31 };
const tag = (name: string) => paint(36, `[${name}]`);

const clip = (text: string, verbose: boolean) => verbose || text.length <= 110 ? text : text.slice(0, 107) + '...';
const line = (k: BillKey, status: string, detail: string) => `${tag(`SB ${k.sb_no}`)} ${k.sb_date}  ${status}${detail ? '  ' + detail : ''}`;
const FIRST_MONTH = '2023-03-01';

/** Read-only: what the database still lacks per unfinished bill. */
function showCheck(db: Db): void {
  let incomplete = 0, cancelled = 0, manual = 0;
  const bills = list_bills(db).sort((a, b) => a.sb_date.localeCompare(b.sb_date));
  for (const b of bills) {
    const status = status_of(db, b);
    if (status === 'Cancelled') cancelled++;
    if (status === 'Manual') manual++;
    if (status === 'Success' || status === 'Cancelled' || status === 'Manual') continue;
    incomplete++;
    console.log(line(b, paint(STATUS_COLOR[status], status.padEnd(9)), `missing: ${gaps(db, b).join(', ')}`));
  }
  const counts = new Map<string, number>();
  for (const b of bills) if (!['Success', 'Cancelled', 'Manual'].includes(status_of(db, b))) for (const g of gaps(db, b)) counts.set(g, (counts.get(g) ?? 0) + 1);
  if (counts.size) console.log(`${tag('gaps')} ${[...counts].sort((a, b) => b[1] - a[1]).map(([g, n]) => `${g} ${n}`).join(' · ')}`);
  console.log(`${tag('check')} ${incomplete} incomplete · ${cancelled} cancelled · ${manual} manual · ${bills.length} bills`);
}

/** The month before the newest stored bill (late gatepasses and LEOs trail the bill date), or the portal's first month. */
function default_from(db: Db): string {
  const newest = list_bills(db)[0]?.sb_date;
  if (!newest) return FIRST_MONTH;
  const d = new Date(Date.UTC(+newest.slice(0, 4), +newest.slice(5, 7) - 2, 1));
  return d.toISOString().slice(0, 10) < FIRST_MONTH ? FIRST_MONTH : d.toISOString().slice(0, 10);
}

export async function main(argv: string[] = Bun.argv.slice(2)): Promise<number> {
  const started = performance.now();
  try {
    const { values: o, positionals } = parseArgs({ args: argv, strict: true, allowPositionals: true, options: {
      file: { type: 'string', short: 'f' }, from: { type: 'string' }, to: { type: 'string' },
      check: { type: 'boolean', short: 'c' }, 'dry-run': { type: 'boolean', short: 'n' }, excel: { type: 'boolean' }, force: { type: 'boolean' },
      workers: { type: 'string', short: 'j', default: '6' }, 'data-dir': { type: 'string', short: 'd' },
      verbose: { type: 'boolean', short: 'v' }, quiet: { type: 'boolean', short: 'q' }, 'no-color': { type: 'boolean' },
      version: { type: 'boolean', short: 'V' }, help: { type: 'boolean', short: 'h' },
    } });
    if (o.help) { console.log(USAGE); return 0; }
    if (o.version) { console.log(pkg.version); return 0; }
    if (o['no-color']) process.env.NO_COLOR = '1';
    const dataDir = o['data-dir'] ?? process.env.DAILY_REPORT_DATA_DIR ?? 'data', dbPath = join(dataDir, 'report.db');
    const [command, ...rest] = positionals;
    if (command === 'fields') { for (const f of CATALOG) console.log(`${f.id.padEnd(30)} ${f.label}`); return 0; }
    if (command === 'iec-missing') {
      if (!existsSync(dbPath)) { console.log('No database yet.'); return 0; }
      const db = open_db(dbPath, true);
      for (const m of missing_iecs(db)) console.log(`${m.iec}  ${m.bills} bill(s)`);
      return 0;
    }
    if (command === 'iec-set') {
      const [iec, name] = rest;
      if (!iec || !name?.trim() || rest.length !== 2 || !/^[0-9A-Za-z]{6,12}$/.test(iec)) throw new Error('Usage: iec-set IEC "NAME"');
      set_iec_name(open_db(dbPath), iec, name.trim(), 'user');
      console.log(`${tag('iec')} ${iec} = ${name.trim()}`);
      return 0;
    }
    if (command === 'mark-manual') {
      const [sb_no, date] = rest;
      if (!sb_no || !date || rest.length !== 2) throw new Error('Usage: mark-manual SBNO SB_DATE');
      const db = open_db(dbPath), bill = list_bills(db).find(b => b.sb_no === sb_no && b.sb_date === parse_date(date));
      if (!bill) throw new Error(`No bill ${sb_no} dated ${date}`);
      mark_manual(db, bill);
      console.log(`${tag('manual')} ${sb_no} is closed by hand`);
      return 0;
    }
    if (command !== undefined) throw new Error(`Unknown command ${command}`);
    if (o.from !== undefined) parse_date(o.from);
    if (o.to !== undefined && (!o.from || parse_date(o.to) < parse_date(o.from))) throw new Error('--to needs --from and must not be earlier');
    if (o.excel && (o.from || o.to || o.force)) throw new Error('--excel makes no ICEGATE calls, so --from, --to and --force do not apply');
    if (o.check && o['dry-run']) throw new Error('--check is a separate read-only mode');
    if (o.verbose && o.quiet) throw new Error('Use --verbose or --quiet, not both');
    const workers = Number(o.workers);
    if (!Number.isInteger(workers) || workers < 1 || workers > 8) throw new Error('--workers must be between 1 and 8');
    const path = o.file ?? process.env.EXCEL_PATH ?? 'report.xlsx';
    const say = (text: string) => { if (!o.quiet) console.log(text); };
    if (o.check) {
      if (!existsSync(dbPath)) { console.log('No database yet.'); return 0; }
      showCheck(open_db(dbPath, true));
      return 0;
    }
    const map = await load_map(join(ROOT, 'export-map.json'));
    const db = open_db(dbPath);
    const from = o.from ? parse_date(o.from) : default_from(db);
    if (!o.excel) say(`${tag('sync')} bills from ${from.slice(0, 7)}`);
    // A terminal gets a live counter; a pipe (the dashboard, a log file) gets one plain line per bill.
    const on_progress = (done: number, count: number, k: BillKey, outcome: { status: string }) => {
      if (o.quiet) return;
      if (process.stderr.isTTY) process.stderr.write(`\r${tag('sync')} ${done}/${count}`);
      else if (!process.stdout.isTTY) console.log(`[progress] ${done}/${count} SB ${k.sb_no} ${outcome.status}`);
    };
    const { results, warnings } = await sync(db, command_client(), { workers, excel_only: !!o.excel, force: !!o.force, from, to: o.to ? parse_date(o.to) : undefined, gatepass_dir: join(dataDir, 'gatepass'), gmail: gmail_gatepasses, on_progress });
    if (!o.quiet && process.stderr.isTTY) process.stderr.write('\r\x1b[K');
    for (const w of warnings) console.log(`${tag('warn')} ${w}`);
    const counts: Partial<Record<RowStatus, number>> = {};
    for (const [k, outcome] of results) {
      counts[outcome.status] = (counts[outcome.status] ?? 0) + 1;
      if (o.quiet ? outcome.status === 'Error' : true) console.log(line(k, paint(STATUS_COLOR[outcome.status], outcome.status.padEnd(9)), clip([outcome.detail, outcome.gatepass ? `gatepass: ${outcome.gatepass === 'saved' ? 'saved PDF (origin unknown)' : outcome.gatepass}` : ''].filter(Boolean).join('; '), !!o.verbose || !process.stdout.isTTY)));
    }
    const sources = gatepass_counts(results.map(([, outcome]) => outcome));
    if (Object.values(sources).some(Boolean)) console.log(`${tag('gatepass')} ICEGATE ${sources.icegate} · Gmail ${sources.gmail} · saved PDF (origin unknown) ${sources.saved} · unavailable ${sources.unavailable} · errors ${sources.error}`);
    const sheets = report_sheets(db, map), total = sheets.reduce((n, s) => n + s.rows.length, 0);
    if (!o['dry-run']) await write_report(path, sheets);
    const summary = (Object.keys(STATUS_COLOR) as RowStatus[]).filter(s => counts[s]).map(s => `${paint(STATUS_COLOR[s], s)} ${counts[s]}`).join(' · ');
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    console.log(`${tag('done')} ${summary || 'nothing to fetch'} · ${total} bills · ${seconds}s · ${o['dry-run'] ? 'dry run, report not written' : `${path} written`}`);
    return counts.Error ? 1 : 0;
  } catch (error) {
    console.error(paint(31, `ERROR: ${error instanceof Error ? error.message : String(error)}`));
    return 1;
  }
}

if (import.meta.main) process.exit(await main());
