/** SQLite store: one row per bill plus invoices, containers and the IEC-name map. No ICEGATE or Excel code. */
import { Database } from 'bun:sqlite';
import { chmodSync, copyFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { BILL_FIELDS, OWN_FIELDS } from '../icegate/schemas';
import { BLANK_VALUES, CANCELLED_STATUS, fy_of, parse_date } from '../rules';

export interface BillKey { sb_no: string; sb_date: string; port_code: string }
export type Bill = BillKey & Record<string, string>;
export type Db = Database;

/** Portal data that cannot be stored (invalid date, conflicting identity): reported on the bill or run, never guessed. */
export class DataError extends Error {}

const SCHEMA_VERSION = 5;
const KIND = new Map<string, string>([...BILL_FIELDS, ...OWN_FIELDS].map(b => [b.col, b.kind]));
const BILL_COLS = [...KIND.keys(), 'fetched_job', 'fetched_enquiry', 'fetched_gatepass'];
const ident = (name: string) => { if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new Error(`Bad column ${name}`); return name; };

export function open_db(path: string, readonly = false): Db {
  if (path !== ':memory:' && !readonly) mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const db = readonly ? new Database(path, { readonly: true }) : new Database(path, { create: true });
  if (readonly) return db;
  db.run('PRAGMA journal_mode = WAL'); db.run('PRAGMA foreign_keys = ON'); db.run('PRAGMA busy_timeout = 10000');
  const version = (db.query('PRAGMA user_version').get() as { user_version: number }).user_version;
  if (version > SCHEMA_VERSION) throw new Error(`Database is newer (v${version}) than this program (v${SCHEMA_VERSION})`);
  const keep_copy = () => { // keep a copy of the old file before it changes
    if (version === 0 || path === ':memory:') return;
    db.run('PRAGMA wal_checkpoint(TRUNCATE)');
    const backup = `${path}.bak-v${version}`;
    copyFileSync(path, backup); chmodSync(backup, 0o600);
  };
  if (version === 4) { // only `shipper` became `exporter`: keep every fetched row
    keep_copy();
    db.transaction(() => { db.run('ALTER TABLE bills RENAME COLUMN shipper TO exporter'); db.run(`PRAGMA user_version = ${SCHEMA_VERSION}`); })();
  } else if (version < SCHEMA_VERSION) {
    keep_copy(); // the fetched tables are rebuilt below
    db.transaction(() => {
      for (const t of ['edocs', 'containers', 'invoices', 'bills']) db.run(`DROP TABLE IF EXISTS ${t}`); // fetched data is re-fetchable; iec_names is kept
      db.run(`CREATE TABLE bills (fy TEXT NOT NULL, sb_no TEXT NOT NULL, sb_date TEXT NOT NULL, port_code TEXT NOT NULL, ${BILL_COLS.map(c => `${ident(c)} TEXT NOT NULL DEFAULT ''`).join(', ')}, PRIMARY KEY (fy, sb_no))`);
      db.run(`CREATE TABLE invoices (fy TEXT NOT NULL, sb_no TEXT NOT NULL, position INTEGER NOT NULL, invoice_no TEXT NOT NULL, invoice_date TEXT NOT NULL DEFAULT '', igst_value TEXT NOT NULL DEFAULT '', PRIMARY KEY (fy, sb_no, invoice_no), FOREIGN KEY (fy, sb_no) REFERENCES bills ON DELETE CASCADE)`);
      db.run(`CREATE TABLE containers (fy TEXT NOT NULL, sb_no TEXT NOT NULL, position INTEGER NOT NULL, container_no TEXT NOT NULL, size TEXT NOT NULL DEFAULT '', seal TEXT NOT NULL DEFAULT '', egm_no TEXT NOT NULL DEFAULT '', egm_date TEXT NOT NULL DEFAULT '', PRIMARY KEY (fy, sb_no, container_no), FOREIGN KEY (fy, sb_no) REFERENCES bills ON DELETE CASCADE)`);
      db.run(`CREATE TABLE IF NOT EXISTS iec_names (iec TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('my-iec','user')))`);
      if (version > 0) { // earlier versions also allowed 'workbook-seed' names; the rebuilt table keeps every other row
        db.run(`ALTER TABLE iec_names RENAME TO iec_names_old`);
        db.run(`CREATE TABLE iec_names (iec TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('my-iec','user')))`);
        db.run(`INSERT INTO iec_names SELECT * FROM iec_names_old WHERE source <> 'workbook-seed'`);
        db.run(`DROP TABLE iec_names_old`);
      }
      db.run(`PRAGMA user_version = ${SCHEMA_VERSION}`);
    })();
    if (path !== ':memory:') chmodSync(path, 0o600);
  }
  return db;
}

/** Portal text to stored text: placeholders become empty, dates become ISO; a date that is not one throws DataError. */
export function clean(value: unknown, kind: string = 'text'): string {
  const text = String(value ?? '').trim();
  if (BLANK_VALUES.includes(text.toUpperCase())) return '';
  if (kind !== 'date') return text;
  try { return parse_date(text); } catch { throw new DataError(`Invalid date "${text.slice(0, 40)}"`); }
}

/** A bill is unique per financial year and SB number. */
const key_params = (k: BillKey) => [fy_of(k.sb_date), k.sb_no];
const where = 'fy = ? AND sb_no = ?';

/** A stored bill with this key but another date or port, as an error message; null when the key is free or identical. */
export function identity_conflict(db: Db, k: BillKey): string | null {
  const have = get_bill(db, k);
  return have && (have.sb_date !== k.sb_date || have.port_code !== k.port_code) ? `SB ${k.sb_no} in FY ${fy_of(k.sb_date)} is already stored with date ${have.sb_date} and port ${have.port_code}` : null;
}

/** First non-empty value wins: later sources only fill blanks. Creates the bill when new. */
export function fill_bill(db: Db, k: BillKey, values: Record<string, string>): void {
  const cols = Object.keys(values).map(ident);
  for (const c of cols) if (!BILL_COLS.includes(c)) throw new Error(`Unknown bill field ${c}`);
  db.transaction(() => {
    db.query('INSERT OR IGNORE INTO bills (fy, sb_no, sb_date, port_code) VALUES (?, ?, ?, ?)').run(...key_params(k), k.sb_date, k.port_code);
    const conflict = identity_conflict(db, k);
    if (conflict) throw new DataError(conflict);
    if (!cols.length) return;
    const set = cols.map(c => `${c} = COALESCE(NULLIF(${c}, ''), ?)`).join(', ');
    db.query(`UPDATE bills SET ${set} WHERE ${where}`).run(...cols.map(c => clean(values[c], KIND.get(c))), ...key_params(k));
  })();
}

export function stamp(db: Db, k: BillKey, source: 'job' | 'enquiry' | 'gatepass', at = new Date().toISOString()): void {
  db.query(`UPDATE bills SET fetched_${source} = ? WHERE ${where}`).run(at, ...key_params(k));
}

export function fill_invoices(db: Db, k: BillKey, rows: { invoice_no: string; invoice_date?: string; igst_value?: string }[]): void {
  db.transaction(() => rows.forEach((r, i) => {
    if (!clean(r.invoice_no)) return;
    db.query(`INSERT INTO invoices (fy, sb_no, position, invoice_no, invoice_date, igst_value) VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT DO UPDATE SET invoice_date = COALESCE(NULLIF(invoice_date, ''), excluded.invoice_date), igst_value = COALESCE(NULLIF(igst_value, ''), excluded.igst_value)`)
      .run(...key_params(k), i, clean(r.invoice_no), clean(r.invoice_date, 'date'), clean(r.igst_value));
  }))();
}

export interface ContainerIn { container_no: string; size?: string; seal?: string; egm_no?: string; egm_date?: string }
/** Keyed by container number: new containers are appended, known ones only gain blank fields. */
export function fill_containers(db: Db, k: BillKey, rows: ContainerIn[]): void {
  db.transaction(() => {
    for (const r of rows) {
      if (!clean(r.container_no)) continue;
      const next = (db.query(`SELECT COALESCE(MAX(position) + 1, 0) AS n FROM containers WHERE ${where}`).get(...key_params(k)) as { n: number }).n;
      db.query(`INSERT INTO containers (fy, sb_no, position, container_no, size, seal, egm_no, egm_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET ${['size', 'seal', 'egm_no', 'egm_date'].map(c => `${c} = COALESCE(NULLIF(${c}, ''), excluded.${c})`).join(', ')}`)
        .run(...key_params(k), next, clean(r.container_no), clean(r.size), clean(r.seal), clean(r.egm_no), clean(r.egm_date, 'date'));
    }
  })();
}

export function set_iec_name(db: Db, iec: string, name: string, source: 'my-iec' | 'user'): void {
  db.query(`INSERT INTO iec_names VALUES (?, ?, ?) ON CONFLICT DO UPDATE SET name = excluded.name, source = excluded.source
    WHERE excluded.source = 'user' OR iec_names.source <> 'user'`).run(iec, name, source);
}

export function get_bill(db: Db, k: BillKey): Bill | null { return db.query(`SELECT * FROM bills WHERE ${where}`).get(...key_params(k)) as Bill | null; }
export function list_bills(db: Db): Bill[] { return db.query('SELECT * FROM bills ORDER BY sb_date DESC, sb_no DESC').all() as Bill[]; }
export const list_invoices = (db: Db, k: BillKey) => db.query(`SELECT * FROM invoices WHERE ${where} ORDER BY position`).all(...key_params(k)) as Record<string, string>[];
export const list_containers = (db: Db, k: BillKey) => db.query(`SELECT * FROM containers WHERE ${where} ORDER BY position`).all(...key_params(k)) as Record<string, string>[];

/** The seven report fields that are still blank. Empty means the bill is complete. IEC comes from Job Status or the gatepass; the exporter from the IEC map or the gatepass. */
export function gaps(db: Db, k: BillKey): string[] {
  const bill = get_bill(db, k), out: string[] = [];
  if (!bill) return ['iec', 'exporter', 'invoice', 'leo_date', 'container', 'size', 'seal'];
  if (!bill.iec) out.push('iec');
  if (!bill.exporter) out.push('exporter');
  if (!list_invoices(db, k).length) out.push('invoice');
  if (!bill.leo_date) out.push('leo_date');
  const boxes = list_containers(db, k);
  if (!boxes.length) out.push('container');
  if (!boxes.length || boxes.some(b => !b.size)) out.push('size');
  if (!boxes.length || boxes.some(b => !b.seal)) out.push('seal');
  return out;
}

/** Fills blank exporter names from the IEC map. First non-empty wins, so a bill keeps the name it already has. */
export function apply_iec_names(db: Db): void {
  db.run(`UPDATE bills SET exporter = (SELECT name FROM iec_names WHERE iec_names.iec = bills.iec) WHERE exporter = '' AND iec <> '' AND EXISTS (SELECT 1 FROM iec_names WHERE iec_names.iec = bills.iec)`);
}
/** IECs on bills that have no name yet, with how many bills wait for it. */
export const missing_iecs = (db: Db) => db.query(`SELECT iec, COUNT(*) AS bills FROM bills WHERE exporter = '' AND iec <> '' AND iec NOT IN (SELECT iec FROM iec_names) GROUP BY iec ORDER BY bills DESC, iec`).all() as { iec: string; bills: number }[];

type BillStatus = 'Success' | 'Partial' | 'Cancelled' | 'Not found';
/** Judged from what the database holds: cancelled by the portal, complete, never seen by the portal, or still missing fields. */
export function status_of(db: Db, k: BillKey): BillStatus {
  const bill = get_bill(db, k);
  if (bill && CANCELLED_STATUS.test(bill.curr_status ?? '')) return 'Cancelled';
  if (!gaps(db, k).length) return 'Success';
  return bill && (bill.fetched_job || bill.fetched_enquiry) ? 'Partial' : 'Not found';
}

/** Containers carried by bills of more than one named exporter: shared boxes mean LCL. */
export const shared_containers = (db: Db): Set<string> => new Set((db.query(`SELECT c.container_no FROM containers c JOIN bills b USING (fy, sb_no) WHERE b.exporter <> '' GROUP BY c.container_no HAVING COUNT(DISTINCT b.exporter) > 1`).all() as { container_no: string }[]).map(r => r.container_no));
