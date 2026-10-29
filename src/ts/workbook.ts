/** Scoped workbook reads and sequential, guarded updates; edits patch only the touched cells (see ooxml.ts). */
import { createHash, randomBytes } from 'node:crypto';
import { copyFile, readFile, readdir, rename, unlink } from 'node:fs/promises';
import { basename, dirname, extname, join } from 'node:path';
import { colIndex, colLetters, mergedRanges, openBook, patchCells, readCells, save as saveBook, serialToIso, sheetNames, splitCellRef, unesc, type Book, type Edit } from './ooxml';
import * as rules from './rules';
import type { FieldName, RowUpdate, WorkbookRow } from './models';

type Cell = ReturnType<typeof readCells> extends Map<string, infer C> ? C : never;
type Sheet = { cells: Map<string, Cell>; maxRow: number; maxCol: number };
const blank = (value: unknown) => value == null || rules.EXCEL_BLANK_VALUES.includes(String(value).trim().toUpperCase());
const header = (value: unknown) => String(value ?? '').trim().replace(/\s+/g, ' ').toLowerCase().replace(/[.:]+$/, '');
const digest = (data: Uint8Array) => createHash('sha256').update(data).digest('hex');
const isFormula = (c?: Cell) => c?.f != null;
/** Cell content as text; numbers in date fields become ISO dates, formulas keep their '=' prefix. */
function text(c: Cell | undefined, asDate = false): string | null {
  if (!c) return null;
  if (c.f != null) return '=' + unesc(c.f);
  if (c.v == null) return null;
  if (c.t === 'n') return asDate ? serialToIso(+c.v) : String(+c.v);
  return c.v;
}
function date(c: Cell | undefined): string | null {
  const t = text(c, true);
  return blank(t) ? null : rules.parse_date(t!.trim());
}

export class WorkbookStore {
  private dirty = false;
  private sheets = new Map<string, Sheet>();
  private constructor(readonly path: string, private book: Book, private originalHash: string) {}
  static async open(path: string): Promise<WorkbookStore> {
    const bytes = new Uint8Array(await readFile(path));
    return new WorkbookStore(path, await openBook(bytes), digest(bytes));
  }
  /** Sheet names in workbook order. */
  sheets_names(): string[] { return sheetNames(this.book); }
  private sheet(name: string): Sheet {
    let sheet = this.sheets.get(name);
    if (!sheet) {
      const cells = readCells(this.book, name); let maxRow = 0, maxCol = 0;
      for (const ref of cells.keys()) { const { col, row } = splitCellRef(ref); maxRow = Math.max(maxRow, row); if (row === 1) maxCol = Math.max(maxCol, colIndex(col)); }
      this.sheets.set(name, sheet = { cells, maxRow, maxCol });
    }
    return sheet;
  }
  private columns(name: string): Record<string,number> {
    const sheet = this.sheet(name), result: Record<string,number> = {};
    for (const [field, aliases] of Object.entries(rules.EXCEL_COLUMNS)) {
      const indices: number[] = [];
      for (let col = 1; col <= sheet.maxCol; col++) if (aliases.some(a => header(a) === header(text(sheet.cells.get(colLetters(col) + 1))))) indices.push(col);
      if (indices.length > 1) throw new Error(`Ambiguous columns for ${field} in ${name}`);
      if (indices[0]) result[field] = indices[0];
    }
    const missing = rules.EXCEL_REQUIRED_COLUMNS.filter(f => !result[f]);
    if (missing.length) throw new Error(`Missing required columns in ${name}: ${missing.join(', ')}`);
    return result;
  }
  rows(name: string, selected?: Set<number>, sb_no?: string): WorkbookRow[] {
    const sheet = this.sheet(name), cols = this.columns(name), rows: WorkbookRow[] = [];
    for (let row = 2; row <= sheet.maxRow; row++) {
      if (selected && !selected.has(row)) continue;
      const cell = (key: string): Cell | undefined => cols[key] ? sheet.cells.get(colLetters(cols[key]!) + row) : undefined;
      const raw = text(cell('sb_no'));
      if (blank(raw)) continue;
      const sb = raw!.trim().replace(/\.0$/, '');
      if (!/^[0-9]{7}$/.test(sb) || (sb_no && sb !== sb_no)) continue;
      const status = text(cell('status'));
      const cancelled = text(cell('billing_status'))?.trim().toUpperCase() === rules.EXCEL_CANCELLED_VALUE;
      if (cancelled) { rows.push({row_idx:row, identity:{sb_no:sb}, values:{}, cancelled:true,status}); continue; }
      try {
        const port = text(cell('port_code'));
        const identity = rules.create_identity({sb_no:sb, sb_date:date(cell('sb_date')), port_code:blank(port) ? null : port, document_date:date(cell('document_date'))});
        const values: WorkbookRow['values'] = {};
        for (const field of rules.DATA_FIELDS) {
          const c = cell(field), v = text(c, field === 'leo_date');
          if (blank(v)) values[field] = null;
          else if (field === 'leo_date' && !isFormula(c)) {
            try { values[field] = date(c); } catch { values[field] = v; }
          } else values[field] = v;
        }
        rows.push({row_idx:row, identity, values, cancelled:false,status});
      } catch (error) { rows.push({row_idx:row, identity:{sb_no:sb}, values:{}, cancelled:false,status,error:`${name} row ${row}: ${String(error)}`}); }
    }
    return rows;
  }
  apply(name: string, updates: RowUpdate[], force = false, allowed = new Set<FieldName>(rules.DATA_FIELDS)): void {
    if (!updates.length) return;
    const sheet = this.sheet(name), cols = this.columns(name);
    const at = (row: number, col: number) => sheet.cells.get(colLetters(col) + row);
    const selected = new Set(updates.map(u => u.row_idx));
    if (selected.size !== updates.length) throw new Error('Duplicate row updates');
    const cancelled = new Set<number>();
    for (let r = 2; r <= sheet.maxRow; r++) if (cols.billing_status && text(at(r, cols.billing_status))?.trim().toUpperCase() === rules.EXCEL_CANCELLED_VALUE) cancelled.add(r);
    const merges = mergedRanges(this.book, name);
    const parent = (row: number, col: number) => {
      const m = merges.find(m => row >= m.top && row <= m.bottom && col >= m.left && col <= m.right);
      return m ? {row:m.top, col:m.left, rows:Array.from({length:m.bottom-m.top+1}, (_, i) => m.top+i)} : {row, col, rows:[row]};
    };
    const flag = (u: RowUpdate, reason: string) => { if (u.status !== 'Cancelled' && u.status !== 'Error') u.status = 'Partial'; u.detail = [u.detail, reason].filter(Boolean).join('; '); };
    const plans = new Map<string, { row:number; field:FieldName; value:string; update:RowUpdate }[]>();
    for (const u of updates) {
      if (cancelled.has(u.row_idx)) { u.status = 'Cancelled'; continue; }
      const values = {...u.values};
      for (const field of u.container_fields) {
        if (!u.containers?.length || !rules.CONTAINER_FIELDS.includes(field)) throw new Error('Invalid container update');
        values[field] = u.containers.map(r => r[rules.CONTAINER_ATTRS[field as keyof typeof rules.CONTAINER_ATTRS]] ?? '').join('\n');
      }
      for (const [key, val] of Object.entries(values)) {
        const field = key as FieldName;
        if (!allowed.has(field) || blank(val)) continue;
        const p = parent(u.row_idx, cols[field]!), k = `${p.row}:${p.col}`;
        const list = plans.get(k) ?? []; list.push({row:u.row_idx, field, value:val!, update:u}); plans.set(k, list);
      }
    }
    const addr = (r: number, c: number) => colLetters(c) + r;
    const blocked = new Set<string>();
    for (const [k, entries] of plans) {
      const [r, c] = k.split(':').map(Number) as [number, number], p = parent(r, c);
      if (p.rows.some(row => !selected.has(row) || cancelled.has(row)) || new Set(entries.map(e => e.value)).size > 1) {
        blocked.add(k); for (const e of entries) flag(e.update, `Blocked shared cell ${addr(r, c)}`);
      }
      if (isFormula(at(r, c))) { blocked.add(k); for (const e of entries) flag(e.update, `Protected formula ${addr(r, c)}`); }
    }
    let changed = true;
    while (changed) {
      changed = false;
      const affected = new Set([...blocked].flatMap(k => (plans.get(k) ?? []).filter(e => rules.CONTAINER_FIELDS.includes(e.field)).map(e => e.row)));
      for (const [k, entries] of plans) if (!blocked.has(k) && entries.some(e => affected.has(e.row) && rules.CONTAINER_FIELDS.includes(e.field))) {
        blocked.add(k); changed = true; for (const e of entries) flag(e.update, 'Container record member blocked');
      }
    }
    const edits: Edit[] = [];
    for (const [k, entries] of plans) {
      if (blocked.has(k)) continue;
      const [r, c] = k.split(':').map(Number) as [number, number], e = entries[0]!, current = at(r, c);
      if (!force && !blank(text(current))) continue;
      const isDate = e.field === 'leo_date', iso = isDate ? rules.parse_date(e.value) : null;
      if (text(current, isDate) === (iso ?? e.value)) continue;
      edits.push(isDate ? {ref:addr(r, c), value:new Date(`${iso}T00:00:00.000Z`), numFmt:rules.EXCEL_DATE_NUMBER_FORMAT}
        : {ref:addr(r, c), value:e.value, wrap:e.value.includes('\n') || undefined});
    }
    if (!cols.status) {
      cols.status = sheet.maxCol + 1;
      edits.push({ref:addr(1, cols.status), value:'Status', like:addr(1, sheet.maxCol)});
    }
    const statuses = new Map<string, RowUpdate[]>();
    for (const u of updates) { const p = parent(u.row_idx, cols.status); const k = `${p.row}:${p.col}`; statuses.set(k, [...(statuses.get(k) ?? []), u]); }
    for (const [k, items] of statuses) {
      const [r, c] = k.split(':').map(Number) as [number, number], p = parent(r, c);
      if (p.rows.some(row => !selected.has(row)) || new Set(items.map(u => u.status)).size > 1 || isFormula(at(r, c))) { for (const u of items) flag(u, `Blocked status ${addr(r, c)}`); continue; }
      if (text(at(r, c)) !== items[0]!.status) edits.push({ref:addr(r, c), value:items[0]!.status});
    }
    if (!edits.length) return;
    patchCells(this.book, name, edits);
    this.sheets.delete(name); this.dirty = true;
  }
  /** Returns whether the file was rewritten (false when no cell changed). */
  async save(): Promise<boolean> {
    if(!this.dirty) return false;
    const dir=dirname(this.path), ext=extname(this.path), stem=basename(this.path,ext);
    const suffix=randomBytes(6).toString('hex'), temporary=join(dir,`.${stem}-${suffix}.tmp.xlsx`);
    try {
      await saveBook(this.book,temporary);
      if(digest(await readFile(this.path))!==this.originalHash) throw new Error('Workbook changed since opening; no update saved');
      const now=new Date(), stamp=now.toISOString().replace(/[-:]/g,'').replace('T','-').replace(/\..*/, '') + '-' + String(now.getMilliseconds()).padStart(3,'0') + suffix.slice(0,3);
      await copyFile(this.path,join(dir,`${stem}.bak-${stamp}${ext}`));
      await rename(temporary,this.path);
      this.originalHash=digest(await readFile(this.path)); this.dirty=false;
      const backups=(await readdir(dir)).filter(n=>n.startsWith(`${stem}.bak-`)&&n.endsWith(ext)).sort();
      for(const old of backups.slice(0,-5)) await unlink(join(dir,old));
      return true;
    } finally {await unlink(temporary).catch(()=>{});}
  }
}
