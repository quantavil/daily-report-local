import { expect, test } from 'bun:test';
import { strFromU8, unzipSync } from 'fflate';
import { mkdtemp, readdir, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { read_sheets } from '../../src/ts/excel/read';
import { build_csv, build_report, write_report, type Sheet } from '../../src/ts/excel/report';

const sheets: Sheet[] = [{ name: 'FY 2026-27', headers: ['Shipping Bill Date', 'Exporter', 'Container No.'], derived: ['Container No.'], rows: [{ 'Shipping Bill Date': { value: '2026-10-07', date: true }, 'Exporter': { value: 'A & B <LTD>' }, 'Container No.': { value: 'X1\nX2' } }, {}] }];

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

test('the highlight flag colours the target column live and limits the flag column to Y or N', () => {
  const marked: Sheet[] = [{ name: 'FY 2026-27', headers: ['SB Job No', 'Own'], highlight: { flag: 'Own', target: 'SB Job No' }, rows: [{ 'SB Job No': { value: '12' }, Own: { value: 'Y' } }, { 'SB Job No': { value: '13' } }] }];
  const files = unzipSync(build_report(marked)), xml = strFromU8(files['xl/worksheets/sheet1.xml']!);
  expect(xml).toContain('<conditionalFormatting sqref="A2:A3"><cfRule type="expression" dxfId="0" priority="1"><formula>$B2="Y"</formula>');
  expect(xml).toContain('sqref="B2:B3"><formula1>"Y,N"</formula1>');
  expect(xml.indexOf('<autoFilter')).toBeLessThan(xml.indexOf('<conditionalFormatting'));
  expect(xml.indexOf('<conditionalFormatting')).toBeLessThan(xml.indexOf('<dataValidations'));
  expect(strFromU8(files['xl/styles.xml']!)).toContain('<dxfs count="1">');
  expect(strFromU8(unzipSync(build_report(sheets))['xl/worksheets/sheet1.xml']!)).not.toContain('conditionalFormatting');
});

test('the report is replaced atomically, leaves no temporary file and keeps the previous file as .bak', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx');
  await write_report(path, sheets);
  expect((await readdir(dir)).sort()).toEqual(['report.csv', 'report.xlsx']); // nothing to back up yet
  const first = await readFile(path);
  await write_report(path, [{ ...sheets[0]!, name: 'FY 2025-26' }]);
  expect((await readdir(dir)).sort()).toEqual(['report.csv', 'report.csv.bak', 'report.xlsx', 'report.xlsx.bak']);
  expect(await readFile(`${path}.bak`)).toEqual(first);
  expect(Object.keys(unzipSync(new Uint8Array(await readFile(path))))).toContain('xl/workbook.xml');
});

test('read_sheets returns what write_report wrote: headers and text, blank cells as empty strings, empty rows left out', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx');
  await write_report(path, sheets);
  expect(await read_sheets(path)).toEqual([{ name: 'FY 2026-27', headers: ['Shipping Bill Date', 'Exporter', 'Container No.'], rows: [{ 'Shipping Bill Date': '46302', Exporter: 'A & B <LTD>', 'Container No.': 'X1\nX2' }] }]);
});

test('read_sheets understands the shared strings an Excel save produces; a missing file is null, a broken one throws', async () => {
  const { strToU8, zipSync } = await import('fflate'), { writeFile } = await import('node:fs/promises');
  const dir = await mkdtemp(join(tmpdir(), 'daily-report-')), path = join(dir, 'saved.xlsx');
  await writeFile(path, zipSync({
    'xl/workbook.xml': strToU8('<workbook xmlns:r="x"><sheets><sheet name="FY 2026-27" sheetId="1" r:id="rId1"/></sheets></workbook>'),
    'xl/_rels/workbook.xml.rels': strToU8('<Relationships><Relationship Id="rId1" Type="t" Target="/xl/worksheets/sheet1.xml"/></Relationships>'),
    'xl/sharedStrings.xml': strToU8('<sst><si><t>Remark</t></si><si><r><t>agent </t></r><r><t>&amp; co</t></r></si></sst>'),
    'xl/worksheets/sheet1.xml': strToU8('<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>Skipped column</t></is></c></row><row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>7</v></c></row></sheetData></worksheet>'),
  }));
  expect(await read_sheets(path)).toEqual([{ name: 'FY 2026-27', headers: ['Remark', 'Skipped column'], rows: [{ Remark: 'agent & co', 'Skipped column': '' }] }]);
  expect(await read_sheets(join(dir, 'none.xlsx'))).toBeNull();
  await writeFile(join(dir, 'broken.xlsx'), 'not a zip');
  await expect(read_sheets(join(dir, 'broken.xlsx'))).rejects.toThrow('left untouched');
});

test('the CSV has the workbook headers and rows, all sheets in order, quoting only where needed, dates as ISO text', async () => {
  const two: Sheet[] = [sheets[0]!, { ...sheets[0]!, name: 'FY 2027-28', rows: [{ 'Shipping Bill Date': { value: '2027-04-02', date: true }, Exporter: { value: 'Say "hi", Ltd' }, 'Container No.': { value: 'X3' } }] }];
  expect(build_csv(two)).toBe('Shipping Bill Date,Exporter,Container No.\n2026-10-07,A & B <LTD>,"X1\nX2"\n,,\n2027-04-02,"Say ""hi"", Ltd",X3\n');
  const dir = await mkdtemp(join(tmpdir(), 'daily-report-')), path = join(dir, 'report.xlsx');
  await write_report(path, two);
  expect(await readFile(join(dir, 'report.csv'), 'utf8')).toBe(build_csv(two));
});
