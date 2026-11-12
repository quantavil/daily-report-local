/** Writes `report.xlsx` from scratch: one sheet per financial year, inline strings, real dates. No ICEGATE or database code. */
import { strToU8, zipSync } from 'fflate';
import { randomBytes } from 'node:crypto';
import { rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { EXCEL_DATE_NUMBER_FORMAT, parse_date } from '../rules';

export interface Cell { value: string; date?: boolean }
export interface Sheet { name: string; headers: string[]; rows: Record<string, Cell>[] }

const esc = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '');
const letters = (n: number): string => (n > 26 ? letters(Math.floor((n - 1) / 26)) : '') + String.fromCharCode(65 + (n - 1) % 26);
/** Excel serial day of an ISO date. */
const serial = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 86_400_000 + 25569;
/** Styles 2..9: body cells by row parity (white / pale grey) and kind. Style 1 is the header. */
const HEADER = 1, CENTER = 0, LEFT = 1, DATE = 2, WRAP = 3;
const style = (row: number, kind: number) => 2 + (row % 2) * 4 + kind;

function cell(ref: string, row: number, c: Cell | undefined, left: boolean): string {
  const blank = `<c r="${ref}" s="${style(row, left ? LEFT : CENTER)}"/>`;
  if (!c || !c.value) return blank;
  if (c.date) try { return `<c r="${ref}" s="${style(row, DATE)}"><v>${serial(parse_date(c.value))}</v></c>`; } catch { /* not a date: written as text */ }
  return `<c r="${ref}" s="${style(row, c.value.includes('\n') ? WRAP : left ? LEFT : CENTER)}" t="inlineStr"><is><t xml:space="preserve">${esc(c.value)}</t></is></c>`;
}

/** Width fits the longest line (header included); long text columns read better left-aligned. */
function widths(s: Sheet): { width: number; left: boolean }[] {
  return s.headers.map(h => {
    const longest = Math.max(...s.rows.flatMap(r => r[h]?.value && !r[h]!.date ? r[h]!.value.split('\n').map(l => l.length) : [0]));
    return { width: Math.min(45, Math.max(12, h.length + 4, longest + 3)), left: longest > 20 };
  });
}

function sheetXml(s: Sheet): string {
  const w = widths(s), last = `${letters(s.headers.length)}${s.rows.length + 1}`;
  const head = s.headers.map((h, i) => `<c r="${letters(i + 1)}1" s="${HEADER}" t="inlineStr"><is><t>${esc(h)}</t></is></c>`).join('');
  const body = s.rows.map((r, i) => `<row r="${i + 2}">${s.headers.map((h, j) => cell(letters(j + 1) + (i + 2), i, r[h], w[j]!.left)).join('')}</row>`).join('');
  const cols = w.map((c, i) => `<col min="${i + 1}" max="${i + 1}" width="${c.width}" customWidth="1"/>`).join('');
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetPr><tabColor rgb="FF107C41"/></sheetPr><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="19.5"/><cols>${cols}</cols><sheetData><row r="1" ht="30" customHeight="1">${head}</row>${body}</sheetData><autoFilter ref="A1:${last}"/></worksheet>`;
}

const side = (color: string) => `<left style="thin"><color rgb="${color}"/></left><right style="thin"><color rgb="${color}"/></right><top style="thin"><color rgb="${color}"/></top><bottom style="thin"><color rgb="${color}"/></bottom>`;
const body_xf = (fill: number, align: string, num = 0) => `<xf numFmtId="${num}" fontId="2" fillId="${fill}" borderId="2" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment ${align}/></xf>`;
const BODY_XFS = [3, 4].flatMap(fill => [body_xf(fill, 'horizontal="center" vertical="center"'), body_xf(fill, 'horizontal="left" vertical="center"'), body_xf(fill, 'horizontal="center" vertical="center"', 164), body_xf(fill, 'horizontal="left" vertical="center" wrapText="1"')]);
const STYLES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="${EXCEL_DATE_NUMBER_FORMAT}"/></numFmts><fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font><font><sz val="10"/><color rgb="FF1E293B"/><name val="Calibri"/></font></fonts><fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/></patternFill></fill></fills><borders count="3"><border/><border>${side('FF326B9C')}</border><border>${side('FFE2E8F0')}</border></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="${2 + BODY_XFS.length}"><xf/><xf fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>${BODY_XFS.join('')}</cellXfs></styleSheet>`;

export function build_report(sheets: Sheet[]): Uint8Array {
  const ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"', rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
  const files: Record<string, Uint8Array> = {
    '[Content_Types].xml': strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>${sheets.map((_, i) => `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join('')}</Types>`),
    '_rels/.rels': strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="${rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>`),
    'xl/workbook.xml': strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook ${ns} xmlns:r="${rel}"><sheets>${sheets.map((s, i) => `<sheet name="${esc(s.name)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join('')}</sheets></workbook>`),
    'xl/_rels/workbook.xml.rels': strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${sheets.map((_, i) => `<Relationship Id="rId${i + 1}" Type="${rel}/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`).join('')}<Relationship Id="rId${sheets.length + 1}" Type="${rel}/styles" Target="styles.xml"/></Relationships>`),
    'xl/styles.xml': strToU8(STYLES),
  };
  sheets.forEach((s, i) => { files[`xl/worksheets/sheet${i + 1}.xml`] = strToU8(sheetXml(s)); });
  return zipSync(files);
}

/** Atomic replace: the report is regenerated from the database, so nothing else needs a backup. */
export async function write_report(path: string, sheets: Sheet[]): Promise<void> {
  const temporary = join(dirname(path), `.report-${randomBytes(6).toString('hex')}.tmp.xlsx`);
  try { await writeFile(temporary, build_report(sheets)); await rename(temporary, path); } finally { await unlink(temporary).catch(() => {}); }
}
