import { Database } from 'bun:sqlite';
import { describe, expect, test } from 'bun:test';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply_iec_names, clean, DataError, fill_bill, fill_containers, fill_invoices, gaps, get_bill, list_containers, mark_manual, stamp, open_db, set_iec_name, status_of } from '../../src/ts/store/db';

const k = { sb_no: '6598990', sb_date: '2026-09-05', port_code: 'INMBD6' };

const iec_name = (db: ReturnType<typeof open_db>, iec: string) => (db.query('SELECT name FROM iec_names WHERE iec = ?').get(iec) as { name: string } | null)?.name ?? '';

describe('store', () => {
  test('a name the gatepasses gave an IEC fills its blank bills unless they disagree', () => {
    const db = open_db(':memory:');
    const bill = (n: number, iec: string, exporter: string) => { const key = { ...k, sb_no: String(n) }; fill_bill(db, key, { iec, exporter }); return key; };
    const [a, b, c, d, e] = [bill(1, 'IEC1', 'ACME'), bill(2, 'IEC1', ''), bill(3, 'IEC2', 'X LTD'), bill(4, 'IEC2', 'X LIMITED'), bill(5, 'IEC2', '')];
    apply_iec_names(db);
    expect([a, b, c, d, e].map(key => get_bill(db, key)!.exporter)).toEqual(['ACME', 'ACME', 'X LTD', 'X LIMITED', '']);
  });
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
  test('a gatepass and an EGM container with one seal are one box under the EGM number, in either order', () => {
    for (const egm_first of [true, false]) {
      const db = open_db(':memory:');
      fill_bill(db, k, {});
      const egm = { container_no: 'MSDU7862584', seal: 'S1', egm_no: '636', egm_date: '2024-02-28' }, pass = { container_no: 'MSDU7862548', size: '40', seal: 'S1' };
      fill_containers(db, k, [egm_first ? egm : pass]); fill_containers(db, k, [egm_first ? pass : egm]);
      expect(list_containers(db, k).map(b => [b.container_no, b.size, b.seal, b.egm_no])).toEqual([['MSDU7862584', '40', 'S1', '636']]);
    }
  });
  test('different seals stay different containers', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, {});
    fill_containers(db, k, [{ container_no: 'MSDU7862584', seal: 'S1', egm_no: '636' }, { container_no: 'MSDU7862548', size: '40', seal: 'S2' }]);
    expect(list_containers(db, k).length).toBe(2);
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
  test('the portal status Purge after 30 days is cancelled', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, { curr_status: 'Purge after 30 days' });
    expect(status_of(db, k)).toBe('Cancelled');
  });
  test('stale exam queue older than 30 days without LEO is cancelled', () => {
    const db = open_db(':memory:');
    const staleKey = { sb_no: '6598994', sb_date: '2026-06-01', port_code: 'INHTG6' };
    fill_bill(db, staleKey, { curr_status: 'TO BE MARKED' });
    expect(status_of(db, staleKey, new Date('2026-11-02T12:00:00Z'))).toBe('Cancelled');
  });
  test('a newer non-empty portal status replaces an older one', () => {
    const db = open_db(':memory:');
    fill_bill(db, k, { curr_status: 'In progress' });
    fill_bill(db, k, { curr_status: 'Purge after 30 days' });
    fill_bill(db, k, { curr_status: 'N.A.' });
    expect(get_bill(db, k)!.curr_status).toBe('Purge after 30 days');
    expect(status_of(db, k)).toBe('Cancelled');
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
  test('a bill closed by hand is Manual while it has gaps, and unknown bills cannot be closed', () => {
    const db = open_db(':memory:');
    expect(() => mark_manual(db, k)).toThrow();
    fill_bill(db, k, { leo_date: '07 SEP 2026 00:09' }); stamp(db, k, 'enquiry');
    expect(status_of(db, k)).toBe('Partial');
    mark_manual(db, k); mark_manual(db, k);
    expect(status_of(db, k)).toBe('Manual');
  });
  test('a date that is not a date is rejected, never stored', () => {
    const db = open_db(':memory:');
    expect(() => clean('not-a-date', 'date')).toThrow(DataError);
    expect(() => fill_bill(db, k, { leo_date: 'soon' })).toThrow('Invalid date');
    expect(get_bill(db, k)?.leo_date ?? '').toBe('');
    expect(clean('N.A.', 'date')).toBe('');
  });
  test.each([5, 6])('a v%i database keeps its rows and gains the missing columns', version => {
    const path = join(mkdtempSync(join(tmpdir(), 'daily-report-')), 'report.db'), old = open_db(path);
    old.run('ALTER TABLE bills DROP COLUMN sb_job_no'); if (version === 5) old.run('ALTER TABLE bills DROP COLUMN facilitation'); old.run(`PRAGMA user_version = ${version}`);
    old.query("INSERT INTO bills (fy, sb_no, sb_date, port_code, exporter) VALUES ('2026-27', ?, ?, ?, 'ACME')").run(k.sb_no, k.sb_date, k.port_code);
    old.close();
    const db = open_db(path);
    expect(get_bill(db, k)).toMatchObject({ exporter: 'ACME', facilitation: '', sb_job_no: '' }); expect(existsSync(`${path}.bak-v${version}`)).toBe(true);
  });
  test('upgrading a database keeps a copy of the old file', () => {
    const path = join(mkdtempSync(join(tmpdir(), 'daily-report-')), 'report.db'), old = new Database(path, { create: true });
    old.run('CREATE TABLE bills (x)'); old.run('PRAGMA user_version = 3'); old.close();
    open_db(path).close();
    expect(existsSync(`${path}.bak-v3`)).toBe(true);
  });
});
