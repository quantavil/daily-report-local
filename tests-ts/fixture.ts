import { strToU8, zipSync } from 'fflate';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const SHEET = 'FY 2026-27';
export const HEADERS = ['Shipping Bill No','SB Date','Port Code','Inv No','LEO Date','Shipper Name','Container No','Container Size','Customs Seal','Billing Status','Status'];
const col = (i: number) => String.fromCharCode(65 + i);
const str = (ref: string, v: string) => `<c r="${ref}" t="inlineStr"><is><t>${v}</t></is></c>`;
const cellRow = (r: number, cells: (string | null)[]) => `<row r="${r}">${cells.map((v, i) => v == null ? '' : v.startsWith('<') ? v : str(col(i) + r, v)).join('')}</row>`;

/** A minimal openpyxl-style workbook: inline strings, a styles part and a merged cell, plus an untouched extra part. */
export const SHEET2 = 'FY 2025-26';
export async function fixture(rows: (string | null)[][], extra = '', twoSheets = false) {
  const sheet = `<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:K${rows.length + 1}"/><sheetData>${cellRow(1, HEADERS)}${rows.map((r, i) => cellRow(i + 2, r)).join('')}</sheetData>${extra}</worksheet>`;
  const files = {
    '[Content_Types].xml': strToU8('<Types/>'),
    'xl/workbook.xml': strToU8(`<workbook xmlns:r="r"><sheets><sheet name="${SHEET}" sheetId="1" r:id="rId1"/>${twoSheets ? `<sheet name="${SHEET2}" sheetId="2" r:id="rId2"/>` : ''}</sheets></workbook>`),
    'xl/_rels/workbook.xml.rels': strToU8(`<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>${twoSheets ? '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>' : ''}</Relationships>`),
    ...(twoSheets ? { 'xl/worksheets/sheet2.xml': strToU8(sheet) } : {}),
    'xl/styles.xml': strToU8('<styleSheet><numFmts count="1"><numFmt numFmtId="164" formatCode="dd\\-mmm\\-yyyy"/></numFmts><cellXfs count="1"><xf numFmtId="0"/></cellXfs></styleSheet>'),
    'xl/worksheets/sheet1.xml': strToU8(sheet),
    'xl/theme/theme1.xml': strToU8('<theme>untouched</theme>'),
  };
  const path = join(await mkdtemp(join(tmpdir(), 'daily-report-')), 'book.xlsx');
  await writeFile(path, zipSync(files));
  return path;
}
