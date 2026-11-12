/** `export-map.json`: which workbook column receives which database field. Checked against the field catalog. */
import * as v from 'valibot';
import { CATALOG } from '../icegate/schemas';
import shipped from '../../../export-map.json';

export type ExportMap = Record<string, string>;
const IDS = CATALOG.map(f => f.id);
const MapSchema = v.record(v.pipe(v.string(), v.trim(), v.minLength(1)), v.picklist(IDS));

/** The map shipped in `export-map.json`, used when no map file is given or present. */
export const DEFAULT_MAP: ExportMap = parse_map(shipped);

/** An unknown field id fails loudly; so does an empty map. */
export function parse_map(data: unknown): ExportMap {
  const parsed = v.safeParse(MapSchema, data);
  if (!parsed.success) throw new Error(`export-map.json: ${parsed.issues[0]!.message}`);
  if (!Object.keys(parsed.output).length) throw new Error('export-map.json maps no columns');
  return parsed.output;
}

export async function load_map(path: string): Promise<ExportMap> {
  const file = Bun.file(path);
  if (!(await file.exists())) return DEFAULT_MAP;
  let data: unknown;
  try { data = JSON.parse(await file.text()); } catch { throw new Error(`${path} is not valid JSON`); }
  return parse_map(data);
}

export const field_kind = (id: string) => CATALOG.find(f => f.id === id)?.kind ?? 'text';
