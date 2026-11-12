/** Localhost dashboard: runs the CLI as a child process, serves its output and a read-only view of report.db; no fetch, workflow or workbook logic. */
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { load_map, parse_map, type ExportMap } from './excel/export-map';
import { CATALOG } from './icegate/schemas';
import { gaps, list_bills, missing_iecs, open_db } from './store/db';

const ROOT = join(import.meta.dir, '../..');
const MODES = { check: ['-c'], sync: [], dry: ['-n'], write: ['--excel'] } as const;
type Mode = keyof typeof MODES;
export interface RunRequest { mode?: string }

class Bad extends Error {}

/** Allowlisted CLI arguments for a dashboard request; the CLI validates values again. */
export function toArgs(body: RunRequest): string[] {
  const mode = body.mode as Mode;
  if (!(mode in MODES)) throw new Bad('Unknown mode');
  return [...MODES[mode]];
}

interface Run { id: number; command: string; lines: string[]; running: boolean; exitCode: number | null; child?: Bun.Subprocess }
let run: Run = { id: 0, command: '', lines: [], running: false, exitCode: null };

async function pump(stream: ReadableStream<Uint8Array>, target: Run) {
  let rest = '';
  const decoder = new TextDecoder();
  for await (const chunk of stream) {
    const parts = (rest + decoder.decode(chunk, { stream: true })).split('\n');
    rest = parts.pop()!;
    target.lines.push(...parts);
  }
  if (rest) target.lines.push(rest);
}

const spawnCli = (args: string[]) => Bun.spawn([process.execPath, join(ROOT, 'src/ts/cli.ts'), ...args, '--no-color'], { cwd: ROOT, stdin: 'ignore', stdout: 'pipe', stderr: 'pipe' });

/** Read-only `--check` output for the overview tiles; it never occupies the run slot. */
async function scan(): Promise<string[]> {
  const child = spawnCli(['-c']);
  const [out, err] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  return (out + err).split('\n').filter(Boolean);
}

function start(args: string[]) {
  const current: Run = { id: run.id + 1, command: ['bun run sync', ...args].join(' '), lines: [], running: true, exitCode: null };
  const child = spawnCli(args);
  current.child = child;
  run = current;
  void Promise.all([pump(child.stdout, current), pump(child.stderr, current), child.exited]).then(([, , code]) => { current.exitCode = code; current.running = false; });
}

const dataDir = () => process.env.DAILY_REPORT_DATA_DIR ?? join(ROOT, 'data');
const MAP_PATH = join(ROOT, 'export-map.json');

/** What report.db holds: bills with their blank fields, plus IECs waiting for a name. Read-only. */
export function dbView(path = join(dataDir(), 'report.db')) {
  if (!existsSync(path)) return { total: 0, bills: [], missing_iecs: [] };
  const db = open_db(path, true);
  try {
    const all = list_bills(db);
    return {
      total: all.length,
      bills: all.slice(0, 500).map(b => ({ sb_no: b.sb_no, sb_date: b.sb_date, port_code: b.port_code, iec: b.iec, exporter: b.exporter, leo_date: b.leo_date, job_status: b.job_status, fetched_enquiry: b.fetched_enquiry, fetched_gatepass: b.fetched_gatepass, gaps: gaps(db, b) })),
      missing_iecs: missing_iecs(db),
    };
  } finally { db.close(); }
}

const json = (data: unknown, status = 200) => Response.json(data, { status });

/** Exported so tests need no listening socket. `begin` and `overview` are injectable to keep tests from spawning the CLI. */
export async function handle(req: Request, begin: (args: string[]) => void = start, overview: () => Promise<string[]> = scan, save_map: (map: ExportMap) => Promise<void> = map => Bun.write(MAP_PATH, JSON.stringify(map, null, 2) + '\n').then(() => {})): Promise<Response> {
  const url = new URL(req.url);
  // Anything on this machine's browser can reach localhost; only same-origin pages may drive it.
  if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') return json({ error: 'Bad host' }, 403);
  if (req.method === 'GET' && url.pathname === '/') return new Response(Bun.file(join(ROOT, 'src/ui/index.html')));
  if (req.method === 'GET' && url.pathname === '/api/log') {
    const from = Math.max(0, Number(url.searchParams.get('from')) || 0);
    return json({ id: run.id, command: run.command, running: run.running, exitCode: run.exitCode, lines: run.lines.slice(from) });
  }
  if (req.method === 'GET' && url.pathname === '/api/db') return json(dbView());
  if (req.method === 'GET' && url.pathname === '/api/fields') return json({ catalog: CATALOG, map: await load_map(MAP_PATH) });
  if (req.method === 'GET' && url.pathname === '/api/overview') return json({ lines: await overview() });
  if (req.method !== 'POST') return json({ error: 'Not found' }, 404);
  if (req.headers.get('origin') !== url.origin || !req.headers.get('content-type')?.startsWith('application/json')) return json({ error: 'Forbidden' }, 403);
  if (url.pathname === '/api/map') {
    if (run.running) return json({ error: 'A run is already in progress' }, 409);
    try { await save_map(parse_map(await req.json())); return json({ ok: true }); }
    catch (error) { return json({ error: error instanceof SyntaxError ? 'Bad JSON' : (error as Error).message }, 400); }
  }
  if (url.pathname === '/api/stop') { run.child?.kill('SIGINT'); return json({ ok: true }); }
  if (url.pathname !== '/api/run') return json({ error: 'Not found' }, 404);
  if (run.running) return json({ error: 'A run is already in progress' }, 409);
  try {
    begin(toArgs(await req.json() as RunRequest));
    return json({ ok: true });
  } catch (error) {
    return json({ error: error instanceof Bad || error instanceof SyntaxError ? (error as Error).message : 'Bad request' }, 400);
  }
}

if (import.meta.main) {
  const port = Number(process.env.PORT) || 4317;
  try {
    Bun.serve({ hostname: '127.0.0.1', port, fetch: req => handle(req) });
  } catch {
    console.error(`Port ${port} is busy. Stop the other dashboard or set PORT=...`);
    process.exit(1);
  }
  console.log(`Dashboard: http://127.0.0.1:${port}  (Ctrl-C to quit)`);
  const quit = () => { run.child?.kill('SIGINT'); process.exit(0); };
  process.on('SIGINT', quit);
  process.on('SIGTERM', quit);
}
