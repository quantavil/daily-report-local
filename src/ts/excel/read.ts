/** Reads cell text back from a workbook, ours or one saved by Excel or LibreOffice. No database, ICEGATE or business rules. */
import { strFromU8, unzipSync } from 'fflate';
import { readFile } from 'node:fs/promises';

export interface SheetText { name: string; headers: string[]; rows: Record<string, string>[] }

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
const decode = (t: string) => t.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (all, e: string) => e[0] === '#' ? String.fromCodePoint(e[1]!.toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : +e.slice(1)) : ENTITIES[e] ?? all);
const attr = (tag: string, name: string) => tag.match(new RegExp(`\\s${name}="([^"]*)"`))?.[1];
const text_of = (xml: string) => decode([...xml.replace(/<rPh\b[\s\S]*?<\/rPh>/g, '').matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)].map(m => m[1]).join(''));
const column = (ref: string) => [...ref.replace(/\d+$/, '')].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);

/** Every sheet of the workbook with its first row as headers; null when the file does not exist. A file that cannot be read throws. */
export async function read_sheets(path: string): Promise<SheetText[] | null> {
  let bytes: Uint8Array;
  try { bytes = new Uint8Array(await readFile(path)); } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null; throw error; }
  try {
    const zip = unzipSync(bytes), part = (name: string) => { const f = zip[name.replace(/^\//, '')]; return f ? strFromU8(f) : ''; };
    const shared = [...part('xl/sharedStrings.xml').matchAll(/<si\b[^>]*>([\s\S]*?)<\/si>/g)].map(m => text_of(m[1]!));
    const targets = new Map([...part('xl/_rels/workbook.xml.rels').matchAll(/<Relationship\b[^>]*>/g)].map(m => [attr(m[0], 'Id'), attr(m[0], 'Target')]));
    const sheets = [...part('xl/workbook.xml').matchAll(/<sheet\b[^>]*>/g)].map(m => ({ name: decode(attr(m[0], 'name') ?? ''), target: targets.get(attr(m[0], 'r:id')) }));
    if (!sheets.length) throw new Error('no sheets');
    return sheets.map(({ name, target }) => {
      if (!target) throw new Error(`sheet ${name} has no part`);
      const xml = part(target.startsWith('/') ? target : `xl/${target}`), grid = new Map<number, Map<number, string>>();
      for (const m of xml.matchAll(/<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
        const ref = attr(m[1]!, 'r'), type = attr(m[1]!, 't'), inner = m[2] ?? '';
        if (!ref) continue;
        const raw = decode(inner.match(/<v>([\s\S]*?)<\/v>/)?.[1] ?? '');
        const value = type === 's' ? shared[+raw] ?? '' : type === 'inlineStr' ? text_of(inner) : type === 'e' ? '' : raw;
        if (value === '') continue;
        const row = +ref.replace(/^[A-Z]+/, '');
        grid.set(row, (grid.get(row) ?? new Map()).set(column(ref), value));
      }
      const head = grid.get(1) ?? new Map<number, string>(), headers = [...head.values()];
      const rows = [...grid].filter(([r]) => r > 1).sort(([a], [b]) => a - b).map(([, cells]) => Object.fromEntries([...head].map(([c, h]) => [h, cells.get(c) ?? ''])));
      return { name, headers, rows };
    });
  } catch (error) { throw new Error(`${path} cannot be read (${error instanceof Error ? error.message : error}); it was left untouched`); }
}
