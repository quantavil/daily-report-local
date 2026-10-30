import { expect, test } from 'bun:test';
import { strFromU8, unzipSync } from 'fflate';
import { mkdtemp, readdir, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { build_report, write_report, type Sheet } from '../../src/ts/excel/report';

const sheets: Sheet[] = [{ name: 'FY 2026-27', headers: ['Shipping Bill Date', 'Exporter', 'Container No.'], rows: [{ 'Shipping Bill Date': { value: '2026-10-07', date: true }, 'Exporter': { value: 'A & B <LTD>' }, 'Container No.': { value: 'X1\nX2' } }, {}] }];

test('dates are real serial numbers, text is escaped, multi-line cells wrap', () => {
  const files = unzipSync(build_report(sheets)), xml = strFromU8(files['xl/worksheets/sheet1.xml']!);
  expect(strFromU8(files['xl/workbook.xml']!)).toContain('name="FY 2026-27"');
  expect(xml).toContain('<c r="A2" s="4"><v>46302</v></c>');
  expect(xml).toContain('A &amp; B &lt;LTD&gt;');
  expect(xml).toContain('<c r="C2" s="5" t="inlineStr">');
  expect(strFromU8(files['xl/styles.xml']!)).toContain('dd\\-mmm\\-yyyy');
  expect(xml).toContain('<c r="A3" s="6"/>'); // blank cells keep the alternating row colour
  expect(strFromU8(files['xl/styles.xml']!)).toContain('FF1F4E78'); // dark blue header
});

test('the report is replaced atomically and leaves no temporary file', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx');
  await write_report(path, sheets); await write_report(path, sheets);
  expect(await readdir(dir)).toEqual(['report.xlsx']);
  expect(Object.keys(unzipSync(new Uint8Array(await readFile(path))))).toContain('xl/workbook.xml');
});
