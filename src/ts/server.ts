/** Localhost dashboard: runs the CLI as a child process and serves its output; no parsing or workbook logic. */
import { join } from 'node:path';

const ROOT = join(import.meta.dir, '../..');
const MODES = { check: ['-c'], sync: [], dry: ['-n'], verify: [] } as const;
type Mode = keyof typeof MODES;
export interface RunRequest { mode?: string; scope?: string; fy?: string; sb?: string; rows?: string; force?: boolean; leoOnly?: boolean }

class Bad extends Error {}

/** Allowlisted CLI arguments for a dashboard request; the CLI validates values again. */
export function toArgs(body: RunRequest): string[] {
  const mode = body.mode as Mode;
  if (!(mode in MODES)) throw new Bad('Unknown mode');
  const args: string[] = [...MODES[mode]];
  if (body.scope === 'latest') args.push('--latest');
  else if (body.scope === 'fy') {
    if (!/^[0-9]{4}-[0-9]{2}$/.test(body.fy ?? '')) throw new Bad('Year must look like 2026-27');
    args.push('--fy', body.fy!);
  } else if (body.scope !== 'all') throw new Bad('Unknown scope');
  if (body.sb) { if (!/^[0-9]{7}$/.test(body.sb)) throw new Bad('SB must be seven digits'); args.push('--sb', body.sb); }
  const rows = body.rows?.trim();
  if (rows && !/^[0-9,\- ]+$/.test(rows)) throw new Bad('Rows look like 2,4-6');
  if (mode === 'verify') { if (!rows) throw new Bad('Verify needs rows'); args.push('--verify-rows', rows); }
  else if (rows) args.push('-r', rows);
  if (body.force && (mode === 'sync' || mode === 'dry')) args.push('--force');
  if (body.leoOnly) args.push('--leo-only');
  return args;
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

const json = (data: unknown, status = 200) => Response.json(data, { status });

/** Exported so tests need no listening socket. `begin` and `overview` are injectable to keep tests from spawning the CLI. */
export async function handle(req: Request, begin: (args: string[]) => void = start, overview: () => Promise<string[]> = scan): Promise<Response> {
  const url = new URL(req.url);
  // Anything on this machine's browser can reach localhost; only same-origin pages may drive it.
  if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') return json({ error: 'Bad host' }, 403);
  if (req.method === 'GET' && url.pathname === '/') return new Response(Bun.file(join(ROOT, 'src/ui/index.html')));
  if (req.method === 'GET' && url.pathname === '/api/log') {
    const from = Math.max(0, Number(url.searchParams.get('from')) || 0);
    return json({ id: run.id, command: run.command, running: run.running, exitCode: run.exitCode, lines: run.lines.slice(from) });
  }
  if (req.method === 'GET' && url.pathname === '/api/overview') return json({ lines: await overview() });
  if (req.method !== 'POST') return json({ error: 'Not found' }, 404);
  if (req.headers.get('origin') !== url.origin || !req.headers.get('content-type')?.startsWith('application/json')) return json({ error: 'Forbidden' }, 403);
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
