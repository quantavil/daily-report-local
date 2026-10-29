import { expect, test } from 'bun:test';
import { handle, toArgs } from '../src/ts/server';

const post = (path: string, body: unknown, headers: Record<string, string> = {}) => new Request(`http://127.0.0.1:4317${path}`, {
  method: 'POST', headers: { origin: 'http://127.0.0.1:4317', 'content-type': 'application/json', ...headers }, body: JSON.stringify(body),
});

test('requests become allowlisted CLI arguments', () => {
  expect(toArgs({ mode: 'sync', scope: 'all' })).toEqual([]);
  expect(toArgs({ mode: 'check', scope: 'latest' })).toEqual(['-c', '--latest']);
  expect(toArgs({ mode: 'dry', scope: 'fy', fy: '2025-26', sb: '7546404', rows: '2, 4-6', force: true })).toEqual(['-n', '--fy', '2025-26', '--sb', '7546404', '-r', '2, 4-6', '--force']);
  expect(toArgs({ mode: 'verify', scope: 'all', rows: '1349', leoOnly: true })).toEqual(['--verify-rows', '1349', '--leo-only']);
});

test('bad input never reaches the CLI', () => {
  for (const body of [{ mode: 'rm' }, { mode: 'sync', scope: 'x' }, { mode: 'sync', scope: 'fy', fy: '--file' }, { mode: 'sync', scope: 'all', sb: '12' },
    { mode: 'sync', scope: 'all', rows: '1; ls' }, { mode: 'verify', scope: 'all' }]) expect(() => toArgs(body)).toThrow();
});

test('only same-origin JSON posts on localhost are accepted', async () => {
  const started: string[][] = [];
  const begin = (args: string[]) => { started.push(args); };
  expect((await handle(new Request('http://evil.example/api/log'), begin)).status).toBe(403);
  expect((await handle(post('/api/run', { mode: 'check', scope: 'all' }, { origin: 'http://evil.example' }), begin)).status).toBe(403);
  expect((await handle(post('/api/run', { mode: 'check', scope: 'all' }, { 'content-type': 'text/plain' }), begin)).status).toBe(403);
  expect((await handle(post('/api/run', { mode: 'nope', scope: 'all' }), begin)).status).toBe(400);
  expect(started).toEqual([]);
  expect((await handle(post('/api/run', { mode: 'check', scope: 'all' }), begin)).status).toBe(200);
  expect(started).toEqual([['-c']]);
});

test('the overview endpoint serves read-only check lines', async () => {
  const res = await handle(new Request('http://127.0.0.1:4317/api/overview'), () => {}, async () => ['[check] 1 incomplete']);
  expect(await res.json()).toEqual({ lines: ['[check] 1 incomplete'] });
});

test('the log endpoint reports an idle server', async () => {
  const body = await (await handle(new Request('http://localhost:4317/api/log?from=0'))).json() as { running: boolean; lines: string[] };
  expect(body.running).toBe(false);
  expect(body.lines).toEqual([]);
});
