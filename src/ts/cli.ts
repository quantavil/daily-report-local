/** Command-line entry point: yt-dlp-style tagged output over the local ICEGATE sync. */
import { parseArgs } from 'node:util';
import pkg from '../../package.json';
import { GmailClient } from './gmail';
import { WorkbookStore } from './workbook';
import { execute, type WorkflowRecord } from './workflow';
import * as rules from './rules';
import type { FieldName, RowStatus, WorkbookRow } from './models';

const USAGE = `daily-report ${pkg.version} - fill the Daily Working Report from ICEGATE PDFs

Usage: bun run sync [options]

Plain "bun run sync" goes through every FY sheet and fills the new/unfinished
rows (Success rows are skipped, so finished years cost nothing).

Examples:
  bun run sync                      fill all FY sheets
  bun run sync -c                   only list rows with missing fields
  bun run sync -n                   dry run: nothing is saved
  bun run sync --sb 7546404         one shipping bill
  bun run sync -r 1349-1351         rows 1349 to 1351
  bun run sync --latest             only the newest FY sheet
  bun run sync --fy 2025-26         only that year's sheet
  bun run sync --force              also recheck Success rows

Common:
  -c, --check            List rows with missing fields (read-only, no Gmail)
  -n, --dry-run          Do everything except save the workbook
  -r, --rows 2,4-6       Only these Excel rows (of the latest sheet unless --fy is given)
      --sb NNNNNNN       Only this Shipping Bill
      --latest           Only the newest FY sheet
      --fy 2026-27       Only this financial year's sheet (default: all FY sheets)
      --force            Also recheck Success rows for revised PDFs
  -v, --verbose          Show skipped rows and full details
  -h, --help             Show this help

Advanced:
  -f, --file PATH        Workbook (default: $EXCEL_PATH or "Daily Working Report.xlsx")
  -s, --sheet NAME       Exact sheet name instead of the FY sheets
      --leo-only         Process the SB22 LEO date only
      --verify-rows R    Re-parse saved local PDFs for rows R and compare with Excel (read-only)
      --refresh          Re-extract layout from stored PDFs
  -j, --workers N        Concurrent bills, 1-8 (default 3)
  -d, --data-dir PATH    Per-SB storage (default: $DAILY_REPORT_DATA_DIR or ./data)
  -q, --quiet            Only errors and the final summary
      --no-color         Disable colors (NO_COLOR is honored)
  -V, --version          Print the version

Exit status is 1 if any row errored.`;

const paint = (code: number, text: string) => process.stdout.isTTY && !process.env.NO_COLOR ? `\x1b[${code}m${text}\x1b[0m` : text;
const STATUS_COLOR: Record<RowStatus, number> = { Success: 32, Partial: 33, 'Not found': 36, Cancelled: 90, Error: 31 };
const tag = (name: string) => paint(36, `[${name}]`);

export function parseRowSpec(spec: string): Set<number> {
  const rows = new Set<number>();
  for (const part of spec.split(',')) {
    const bounds = part.trim().split('-');
    if (bounds.length > 2 || bounds.some(b => !/^[0-9]+$/.test(b))) throw new Error('Rows must be numbers or ascending ranges, e.g. 2,4-6');
    const first = Number(bounds[0]), last = Number(bounds.at(-1));
    if (!(first >= 2 && first <= last && last <= 1_048_576)) throw new Error('Row ranges must be ascending within Excel data rows 2-1048576');
    for (let row = first; row <= last; row++) rows.add(row);
  }
  return rows;
}

const clip = (text: string, verbose: boolean) => verbose || text.length <= 110 ? text : text.slice(0, 107) + '...';
const line = (row: WorkbookRow, status: string, detail: string, label = '') => `${tag(`${label}row ${row.row_idx}`)} SB ${row.identity.sb_no}  ${status}${detail ? '  ' + detail : ''}`;
const paintStatus = (status: RowStatus) => paint(STATUS_COLOR[status], status.padEnd(9));

function showCheck(rows: WorkbookRow[], fields: Set<FieldName>, label: string): void {
  let incomplete = 0, cancelled = 0, errors = 0, todo = 0;
  for (const row of rows) {
    if (row.error) { errors++; console.log(line(row, paint(31, 'Error'), row.error, label)); continue; }
    if (row.cancelled) { cancelled++; continue; }
    if (row.status !== 'Success') todo++;
    const absent = rules.DATA_FIELDS.filter(name => fields.has(name) && !row.values[name]);
    if (absent.length) { incomplete++; console.log(line(row, paint(33, 'missing'), absent.join(', '), label)); }
  }
  console.log(`${tag(`${label}check`)} ${incomplete} incomplete · ${cancelled} cancelled · ${errors} identity error(s) · ${todo} to sync · ${rows.length} rows`);
}

function showVerification(records: WorkflowRecord[], label: string): void {
  for (const [row, result] of records) {
    if (!result) continue;
    console.log(line(row, '', '', label));
    for (const [name, observation] of Object.entries(result.fields)) {
      const current = row.values[name as FieldName] ?? null;
      const attr = rules.CONTAINER_ATTRS[name as keyof typeof rules.CONTAINER_ATTRS];
      const value = observation.value_type === 'records' ? result.containers.map(record => record[attr] ?? '').join('\n') : observation.value ?? null;
      const verdict = observation.state === 'accepted' ? (value === current ? paint(32, 'MATCH') : paint(33, 'DIFFERENT')) : paint(31, observation.state.toUpperCase());
      console.log(`  ${name}: Excel=${current}; source=${value}; ${verdict}`);
      for (const e of observation.evidence ?? []) console.log(`    ${e.filename}, page ${e.page_number}, ${e.label}: ${e.quote}`);
      if (observation.reason) console.log(`    ${observation.reason}`);
    }
  }
}

export async function main(argv: string[] = Bun.argv.slice(2)): Promise<number> {
  const started = performance.now();
  try {
    const { values: o } = parseArgs({ args: argv, strict: true, options: {
      file: { type: 'string', short: 'f' }, sheet: { type: 'string', short: 's' }, fy: { type: 'string' }, latest: { type: 'boolean' }, rows: { type: 'string', short: 'r' }, sb: { type: 'string' },
      check: { type: 'boolean', short: 'c' }, 'verify-rows': { type: 'string' }, 'leo-only': { type: 'boolean' }, 'dry-run': { type: 'boolean', short: 'n' },
      force: { type: 'boolean' }, refresh: { type: 'boolean' }, workers: { type: 'string', short: 'j', default: '3' }, 'data-dir': { type: 'string', short: 'd' },
      verbose: { type: 'boolean', short: 'v' }, quiet: { type: 'boolean', short: 'q' }, 'no-color': { type: 'boolean' },
      version: { type: 'boolean', short: 'V' }, help: { type: 'boolean', short: 'h' },
    } });
    if (o.help) { console.log(USAGE); return 0; }
    if (o.version) { console.log(pkg.version); return 0; }
    if (o['no-color']) process.env.NO_COLOR = '1';
    if (o.sb !== undefined && !/^[0-9]{7}$/.test(o.sb)) throw new Error('--sb must contain exactly seven digits');
    if (o.fy !== undefined && (o.sheet !== undefined || !/^[0-9]{4}-[0-9]{2}$/.test(o.fy))) throw new Error('--fy takes yyyy-yy (e.g. 2026-27) and cannot be combined with --sheet');
    if (o.latest && (o.sheet !== undefined || o.fy !== undefined)) throw new Error('--latest cannot be combined with --fy or --sheet');
    if (o.rows !== undefined && o['verify-rows'] !== undefined) throw new Error('Use --rows or --verify-rows, not both');
    if (o.check && (o['verify-rows'] !== undefined || o['dry-run'])) throw new Error('--check is a separate read-only mode');
    if (o.verbose && o.quiet) throw new Error('Use --verbose or --quiet, not both');
    const verify = o['verify-rows'] !== undefined, spec = o['verify-rows'] ?? o.rows, workers = Number(o.workers);
    if (!Number.isInteger(workers) || workers < 1 || workers > 8) throw new Error('--workers must be between 1 and 8');
    const path = o.file ?? process.env.EXCEL_PATH ?? 'Daily Working Report.xlsx';
    const workbook = await WorkbookStore.open(path);
    const years = workbook.sheets_names().filter(n => /^FY \d{4}-\d{2}$/.test(n)).sort();
    const named = o.sheet ?? (o.fy ? `FY ${o.fy}` : undefined);
    // Rows only make sense within one sheet, so --rows/--verify-rows without a sheet means the latest one.
    const sheets = named ? [named] : o.latest || spec !== undefined ? years.slice(-1) : years;
    if (!sheets.length) throw new Error('No "FY yyyy-yy" sheet found; pass --sheet');
    const rowSet = spec !== undefined ? parseRowSpec(spec) : undefined;
    const plan = sheets.map(sheet => ({ sheet, selected: workbook.rows(sheet, rowSet, o.sb), label: sheets.length > 1 ? sheet.slice(3) + ' ' : '' }));
    const fields = new Set<FieldName>(o['leo-only'] ? rules.LEO_FIELDS : rules.DATA_FIELDS);
    const say = (text: string) => { if (!o.quiet) console.log(text); };
    say(`${tag('workbook')} ${path}`);
    say(`${tag('sheets')} ${sheets.length === years.length ? 'all: ' : ''}${sheets.map(n => paint(32, n)).join(' · ')}`);
    if (o.check) { for (const { selected, label } of plan) showCheck(selected, fields, label); return 0; }
    const total = plan.reduce((sum, p) => sum + p.selected.length, 0);
    if (!total) { say(`${tag('sync')} No matching workbook rows.`); return 0; }
    const worth = (r: WorkbookRow) => !r.cancelled && !r.error && (o.force || verify || r.status !== 'Success');
    const todo = plan.reduce((sum, p) => sum + p.selected.filter(worth).length, 0);
    say(`${tag(verify ? 'verify' : 'sync')} ${todo} to process · ${total - todo} skipped${o.force || verify ? '' : ' (Success, cancelled or invalid; --force rechecks Success)'}`);
    let done = 0, dirty = false;
    // A terminal gets a live counter; a pipe (the dashboard, a log file) gets one plain line per bill.
    // These are provisional: the workbook may still downgrade a status when the sheet is patched.
    const progress = ([row, , update]: WorkflowRecord) => {
      done++;
      if (o.quiet) return;
      if (process.stderr.isTTY) process.stderr.write(`\r${tag('sync')} ${done}/${todo}`);
      else if (!process.stdout.isTTY) console.log(`[progress] ${done}/${todo} SB ${row.identity.sb_no} ${update.status}`);
    };
    // One save at the end, so a multi-sheet run makes one backup instead of rotating several out.
    const target = { apply: (...args: Parameters<typeof workbook.apply>) => { dirty = true; return workbook.apply(...args); }, save: async () => {} };
    const gmail = new GmailClient(), dataDir = o['data-dir'] ?? process.env.DAILY_REPORT_DATA_DIR ?? 'data';
    const counts: Partial<Record<RowStatus, number>> = {};
    const verified: [string, WorkflowRecord[]][] = [];
    for (const { sheet, selected, label } of plan) {
      if (!selected.length) continue;
      const records = await execute(target, sheet, gmail, dataDir, {
        selected, force: o.force, leo_only: o['leo-only'], workers, refresh: o.refresh, verify, on_progress: progress,
      });
      if (done && !o.quiet && process.stderr.isTTY) process.stderr.write('\r\x1b[K');
      for (const [row, , update] of records) {
        counts[update.status] = (counts[update.status] ?? 0) + 1;
        const skipped = update.detail === 'Already successful; skipped' || update.detail === 'Cancelled bill skipped';
        if (o.quiet ? update.status === 'Error' : skipped ? o.verbose : true) console.log(line(row, paintStatus(update.status), clip(update.detail, !!o.verbose || !process.stdout.isTTY), label));
      }
      verified.push([label, records]);
    }
    const saved = dirty && !verify && !o['dry-run'] && await workbook.save();
    if (verify) for (const [label, records] of verified) showVerification(records, label);
    const summary = (Object.keys(STATUS_COLOR) as RowStatus[]).filter(s => counts[s]).map(s => `${paint(STATUS_COLOR[s], s)} ${counts[s]}`).join(' · ');
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    const outcome = saved ? 'workbook saved' : dirty && o['dry-run'] ? 'dry run, workbook not saved' : 'workbook unchanged';
    console.log(`${tag('done')} ${summary} · ${seconds}s · ${outcome}`);
    return counts.Error ? 1 : 0;
  } catch (error) {
    console.error(paint(31, `ERROR: ${error instanceof Error ? error.message : String(error)}`));
    return 1;
  }
}

if (import.meta.main) process.exit(await main());
