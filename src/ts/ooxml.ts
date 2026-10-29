/** Surgical XLSX edits: string-level <c> patches; every other zip entry is copied untouched (ExcelJS/SheetJS rewrite styles). */
import { unzipSync, zipSync, strFromU8, strToU8, type Zippable } from "fflate";

export type Edit = { ref: string; value: string | number | Date; wrap?: boolean; numFmt?: string; like?: string };  // Date => default numFmt dd\-mmm\-yyyy
export type Book = { files: Record<string, Uint8Array>; text: Map<string, string> };

export const DATE_FORMAT = "dd\\-mmm\\-yyyy";
const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
export const unesc = (s: string) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, "&");
const colNum = (c: string) => [...c].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
const splitRef = (r: string) => { const m = /^([A-Z]+)(\d+)$/.exec(r)!; return { col: m[1]!, row: +m[2]! }; };
const serial = (d: Date) => d.getTime() / 864e5 + 25569;
export const serialToIso = (n: number) => new Date(Math.round((n - 25569) * 864e5)).toISOString().slice(0, 10);
export const colLetters = (n: number) => { let s = ""; for (; n > 0; n = ((n - 1) / 26) | 0) s = String.fromCharCode(65 + ((n - 1) % 26)) + s; return s; };
export const colIndex = colNum;
/** Merged ranges of a sheet as 1-based rectangles. */
export function mergedRanges(b: Book, sheet: string) {
  return [...getText(b, sheetPath(b, sheet)).matchAll(/<mergeCell ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"/g)].map(m => ({ left: colNum(m[1]!), top: +m[2]!, right: colNum(m[3]!), bottom: +m[4]! }));
}
export const splitCellRef = splitRef;

export const openBook = async (source: Uint8Array): Promise<Book> => ({ files: unzipSync(source), text: new Map() });
export const getText = (b: Book, name: string) => {
  if (!b.text.has(name)) { const f = b.files[name]; if (!f) throw new Error(`missing ${name}`); b.text.set(name, strFromU8(f)); }
  return b.text.get(name)!;
};
/** Sheet names in workbook order. */
export const sheetNames = (b: Book) => [...getText(b, "xl/workbook.xml").matchAll(/<sheet\b[^>]*>/g)].map(m => unesc(/\bname="([^"]*)"/.exec(m[0])![1]!));
/** Sheet name -> part path via workbook.xml + rels. */
export function sheetPath(b: Book, name: string): string {
  const wb = getText(b, "xl/workbook.xml");
  const tag = [...wb.matchAll(/<sheet\b[^>]*>/g)].map(m => m[0]).find(t => unesc(/\bname="([^"]*)"/.exec(t)![1]!) === name);
  if (!tag) throw new Error(`no sheet ${name}`);
  const rid = /\br:id="([^"]*)"/.exec(tag)![1]!;
  const rel = [...getText(b, "xl/_rels/workbook.xml.rels").matchAll(/<Relationship\b[^>]*>/g)].map(m => m[0]).find(t => t.includes(`Id="${rid}"`))!;
  const target = /Target="([^"]*)"/.exec(rel)![1]!;
  return target.startsWith("/") ? target.slice(1) : "xl/" + target;
}
export const save = async (b: Book, path: string) => {
  const out: Zippable = {};
  for (const [k, v] of Object.entries(b.files)) out[k] = b.text.has(k) ? strToU8(b.text.get(k)!) : v;
  await Bun.write(path, zipSync(out, { level: 6 }));
};

/** Formula text of a cell body, "" for a shared-formula child (`<f t="shared" si="0"/>`), null when it has none. */
const formula = (body: string) => { const m = /<f\b[^>]*?(?:\/>|>([\s\S]*?)<\/f>)/.exec(body); return m ? m[1] ?? "" : null; };

/** Read cells as ref -> {t, v, s}. Handles inline + shared strings. */
export function readCells(b: Book, sheet: string) {
  const sst = b.files["xl/sharedStrings.xml"] ? [...getText(b, "xl/sharedStrings.xml").matchAll(/<si>([\s\S]*?)<\/si>/g)]
    .map(m => unesc([...m[1]!.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(x => x[1]).join(""))) : [];
  const cells = new Map<string, { t: string; v: string | null; f: string | null; s: number }>();
  for (const m of getText(b, sheetPath(b, sheet)).matchAll(/<c r="([A-Z]+\d+)"([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
    const [, ref, attrs, body = ""] = m;
    const t = /\bt="(\w+)"/.exec(attrs!)?.[1] ?? "n", s = +(/\bs="(\d+)"/.exec(attrs!)?.[1] ?? 0);
    const raw = /<v>([\s\S]*?)<\/v>/.exec(body)?.[1];
    const v = t === "inlineStr" ? unesc([...body.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(x => x[1]).join(""))
      : t === "s" && raw != null ? sst[+raw]! : raw != null ? unesc(raw) : null;
    cells.set(ref!, { t, v, f: formula(body), s });
  }
  return cells;
}

/** Return xf index = copy of `base` with optional numFmt/wrap; deduped; mutates styles text. */
function xfFor(st: { xml: string }, base: number, numFmt?: string, wrap?: boolean): number {
  const sec = /<cellXfs\b[^>]*>([\s\S]*?)<\/cellXfs>/.exec(st.xml)!;
  const xfs = [...sec[1]!.matchAll(/<xf\b[^>]*?(?:\/>|>[\s\S]*?<\/xf>)/g)].map(m => m[0]);
  let xf = xfs[base]!;
  if (numFmt) {
    const nf = [...st.xml.matchAll(/<numFmt numFmtId="(\d+)" formatCode="([^"]*)"/g)];
    let id = +(nf.find(m => unesc(m[2]!) === numFmt)?.[1] ?? 0);
    if (!id) {
      id = Math.max(163, ...nf.map(m => +m[1]!)) + 1;
      const el = `<numFmt numFmtId="${id}" formatCode="${esc(numFmt).replace(/"/g, "&quot;")}" />`;
      st.xml = /<numFmts\b/.test(st.xml)
        ? st.xml.replace(/<numFmts count="\d+"/, () => `<numFmts count="${nf.length + 1}"`).replace("</numFmts>", el + "</numFmts>")
        : st.xml.replace(/(<styleSheet\b[^>]*>)/, `$1<numFmts count="1">${el}</numFmts>`);
    }
    xf = xf.replace(/numFmtId="\d+"/, `numFmtId="${id}"`).replace(/ applyNumberFormat="\d"/, "").replace(/^<xf/, '<xf applyNumberFormat="1"');
  }
  if (wrap) {
    xf = /<alignment\b[^>]*\/>/.test(xf) ? xf.replace(/<alignment\b([^>]*?)\s*\/>/, (_, a) => `<alignment${a.replace(/ wrapText="\d"/, "")} wrapText="1" />`)
      : xf.endsWith("/>") ? xf.replace(/\s*\/>$/, '><alignment wrapText="1" /></xf>') : xf.replace(/^(<xf\b[^>]*>)/, '$1<alignment wrapText="1" />');
    if (!/applyAlignment/.test(xf)) xf = xf.replace(/^<xf/, '<xf applyAlignment="1"');
  }
  const hit = xfs.indexOf(xf);
  if (hit >= 0) return hit;
  st.xml = st.xml.replace(/<cellXfs count="\d+">/, `<cellXfs count="${xfs.length + 1}">`).replace("</cellXfs>", xf + "</cellXfs>");
  return xfs.length;
}

/** Apply edits to one sheet. Existing cell style `s` is kept (cloned only when numFmt/wrap is requested). */
export function patchCells(b: Book, sheet: string, edits: Edit[]) {
  const path = sheetPath(b, sheet), st = { xml: getText(b, "xl/styles.xml") };
  let xml = getText(b, path);
  xml = xml.replace(/<sheetData\s*\/>/, "<sheetData></sheetData>");
  let maxCol = 0, maxRow = 0;
  for (const e of edits) {
    const { col, row } = splitRef(e.ref);
    maxCol = Math.max(maxCol, colNum(col)); maxRow = Math.max(maxRow, row);
    // 1. locate (or create) the row
    let rs = xml.indexOf(`<row r="${row}"`), rowTagEnd: number;
    if (rs < 0) {
      let at = xml.indexOf("</sheetData>");
      for (const m of xml.matchAll(/<row r="(\d+)"/g)) if (+m[1]! > row) { at = m.index!; break; }
      xml = xml.slice(0, at) + `<row r="${row}"></row>` + xml.slice(at); rs = at;
    }
    rowTagEnd = xml.indexOf(">", rs);
    if (xml[rowTagEnd - 1] === "/") { xml = xml.slice(0, rowTagEnd - 1).trimEnd() + "></row>" + xml.slice(rowTagEnd + 1); rowTagEnd = xml.indexOf(">", rs); }
    const rowEnd = xml.indexOf("</row>", rs);
    // 2. locate the cell (or its column-ordered insertion point)
    let cs = -1, ce = -1, at = rowEnd, attrs = "";
    for (const m of xml.slice(rowTagEnd + 1, rowEnd).matchAll(/<c r="([A-Z]+)\d+"([^>]*?)(\/>|>[\s\S]*?<\/c>)/g)) {
      const c = colNum(m[1]!), pos = rowTagEnd + 1 + m.index!;
      if (c === colNum(col)) { cs = pos; ce = pos + m[0].length; attrs = m[2]!; break; }
      if (c > colNum(col)) { at = pos; break; }
    }
    // 3. build the new element
    if (!attrs && e.like) attrs = new RegExp(`<c r="${e.like}"([^>]*)>`).exec(xml)?.[1] ?? "";  // inherit style of another cell
    let s = +(/\bs="(\d+)"/.exec(attrs)?.[1] ?? 0);
    const isDate = e.value instanceof Date;
    const numFmt = e.numFmt ?? (isDate ? DATE_FORMAT : undefined);
    if (numFmt || e.wrap) s = xfFor(st, s, numFmt, e.wrap);
    const sa = s ? ` s="${s}"` : "";
    const cell = typeof e.value === "string"
      ? `<c r="${e.ref}"${sa} t="inlineStr"><is><t${/^\s|\s$|\n/.test(e.value) ? ' xml:space="preserve"' : ""}>${esc(e.value)}</t></is></c>`
      : `<c r="${e.ref}"${sa} t="n"><v>${isDate ? serial(e.value as Date) : e.value}</v></c>`;
    xml = cs >= 0 ? xml.slice(0, cs) + cell + xml.slice(ce) : xml.slice(0, at) + cell + xml.slice(at);
  }
  // 4. widen <dimension> if needed
  xml = xml.replace(/<dimension ref="A1:([A-Z]+)(\d+)"/, (_, c, r) => `<dimension ref="A1:${colLetters(Math.max(colNum(c), maxCol))}${Math.max(+r, maxRow)}"`);
  b.text.set(path, xml); b.text.set("xl/styles.xml", st.xml);
}
