import { Database } from 'bun:sqlite';
import { describe, expect, test } from 'bun:test';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { clean, DataError, fill_bill, fill_containers, fill_invoices, gaps, get_bill, list_containers, open_db, set_iec_name, status_of } from '../../src/ts/store/db';

const k = { sb_no: '6598990', sb_date: '2026-09-05', port_code: 'INMBD6' };

const iec_name = (db: ReturnType<typeof open_db>, iec: string) => (db.query('SELECT name FROM iec_names WHERE iec = ?').get(iec) as { name: string } | null)?.name ?? '';

describe('store', () => {
  test('clean drops placeholders and normalises dates', () => {
    expect(clean('N.A.')).toBe(''); expect(clean('NONE')).toBe(''); expect(clean(' - ')).toBe('');
    expect(clean('07 SEP 2026 00:09', 'date')).toBe('2026-09-07');
    expect(clean('2026-09-07 00:00:00.0', 'date')).toBe('2026-09-07');
    expect(clean('05-09-2026', 'date')).toBe('2026-09-05');
  });
  test('first non-empty value wins', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, { leo_date: '07 SEP 2026 00:09', job_no: '' });
    fill_bill(db, k, { leo_date: '09 SEP 2026 00:09', job_no: '1144099' });
    const bill = get_bill(db, k)!;
    expect(bill.leo_date).toBe('2026-09-07'); expect(bill.job_no).toBe('1144099');
  });
  test('a bill is unique per financial year and SB number', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, {});
    expect(() => fill_bill(db, { ...k, sb_date: '2026-09-06' }, {})).toThrow('already stored');
    fill_bill(db, { ...k, sb_date: '2027-04-01' }, {}); // next FY may reuse the number
    expect(db.query('SELECT COUNT(*) AS n FROM bills').get()).toEqual({ n: 2 });
  });
  test('unknown columns are rejected, never interpolated', () => {
    const db = open_db(':memory:');
    expect(() => fill_bill(db, k, { 'x = 1; DROP TABLE bills; --': 'a' })).toThrow();
    expect(() => fill_bill(db, k, { nonsense: 'a' })).toThrow('Unknown bill field');
  });
  test('gatepass adds size and missing containers; existing seal stays', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, {});
    fill_containers(db, k, [{ container_no: 'MSDU7862584', seal: '0216530' }]);
    fill_containers(db, k, [{ container_no: 'MSDU7862584', size: '40', seal: 'OTHER' }, { container_no: 'TGHU1234567', size: '20', seal: 'S2' }]);
    const boxes = list_containers(db, k);
    expect(boxes.map(b => [b.container_no, b.size, b.seal, String(b.position)])).toEqual([['MSDU7862584', '40', '0216530', '0'], ['TGHU1234567', '20', 'S2', '1']]);
  });
  test('gaps lists blank report fields', () => {
    const db = open_db(':memory:');
    expect(gaps(db, k)).toEqual(['iec', 'exporter', 'invoice', 'leo_date', 'container', 'size', 'seal']);
    fill_bill(db, k, { iec: '0123456789', exporter: 'ACME', leo_date: '2026-09-07' });
    fill_invoices(db, k, [{ invoice_no: 'ESC-357' }]);
    fill_containers(db, k, [{ container_no: 'MSDU7862584', seal: '1' }]);
    expect(gaps(db, k)).toEqual(['size']);
    fill_containers(db, k, [{ container_no: 'MSDU7862584', size: '40' }]);
    expect(gaps(db, k)).toEqual([]);
  });
  test('user IEC names are not replaced by my-iec', () => {
    const db = open_db(':memory:');
    set_iec_name(db, '1', 'Mine', 'my-iec'); expect(iec_name(db, '1')).toBe('Mine');
    set_iec_name(db, '1', 'Me', 'user'); set_iec_name(db, '1', 'Portal', 'my-iec'); expect(iec_name(db, '1')).toBe('Me');
  });
  test('status follows the portal: cancelled, complete, partial, never seen', () => {
    const db = open_db(':memory:'), cancelled = { ...k, sb_no: '6598991' }, seen = { ...k, sb_no: '6598992' }, unseen = { ...k, sb_no: '6598993' };
    fill_bill(db, k, { leo_date: '07 SEP 2026', iec: '0123456789', exporter: 'A' }); fill_invoices(db, k, [{ invoice_no: 'I1' }]); fill_containers(db, k, [{ container_no: 'ABCU1234567', size: '40', seal: 'S' }]);
    fill_bill(db, cancelled, { curr_status: 'SB is Cancelled' });
    fill_bill(db, seen, {}); db.query("UPDATE bills SET fetched_enquiry = 'x' WHERE sb_no = ?").run(seen.sb_no);
    fill_bill(db, unseen, {});
    expect([k, cancelled, seen, unseen].map(b => status_of(db, b))).toEqual(['Success', 'Cancelled', 'Partial', 'Not found']);
  });
  test('an older database keeps its IEC names except the retired workbook seed', () => {
    const path = join(mkdtempSync(join(tmpdir(), 'daily-report-')), 'report.db'), old = new Database(path, { create: true });
    old.run("CREATE TABLE iec_names (iec TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL)");
    old.run("INSERT INTO iec_names VALUES ('1','Seed','workbook-seed'), ('2','Mine','my-iec'), ('3','Me','user')");
    old.run('PRAGMA user_version = 3'); old.close();
    const db = open_db(path);
    expect(db.query('SELECT iec FROM iec_names ORDER BY iec').all()).toEqual([{ iec: '2' }, { iec: '3' }]);
    db.close();
  });
  test('a date that is not a date is rejected, never stored', () => {
    const db = open_db(':memory:');
    expect(() => clean('not-a-date', 'date')).toThrow(DataError);
    expect(() => fill_bill(db, k, { leo_date: 'soon' })).toThrow('Invalid date');
    expect(get_bill(db, k)?.leo_date ?? '').toBe('');
    expect(clean('N.A.', 'date')).toBe('');
  });
  test('a v4 database keeps its rows when shipper becomes exporter', () => {
    const path = join(mkdtempSync(join(tmpdir(), 'daily-report-')), 'report.db'), old = open_db(path);
    old.run('ALTER TABLE bills RENAME COLUMN exporter TO shipper'); old.run('PRAGMA user_version = 4');
    old.query("INSERT INTO bills (fy, sb_no, sb_date, port_code, shipper) VALUES ('2026-27', ?, ?, ?, 'ACME')").run(k.sb_no, k.sb_date, k.port_code);
    old.close();
    const db = open_db(path);
    expect(get_bill(db, k)!.exporter).toBe('ACME'); expect(existsSync(`${path}.bak-v4`)).toBe(true);
  });
  test('upgrading a database keeps a copy of the old file', () => {
    const path = join(mkdtempSync(join(tmpdir(), 'daily-report-')), 'report.db'), old = new Database(path, { create: true });
    old.run('CREATE TABLE bills (x)'); old.run('PRAGMA user_version = 3'); old.close();
    open_db(path).close();
    expect(existsSync(`${path}.bak-v3`)).toBe(true);
  });
});
