import { expect, test } from 'bun:test';
import { dbView, handle, toArgs } from '../src/ts/server';

const post = (path: string, body: unknown, headers: Record<string, string> = {}) => new Request(`http://127.0.0.1:4317${path}`, {
  method: 'POST', headers: { origin: 'http://127.0.0.1:4317', 'content-type': 'application/json', ...headers }, body: JSON.stringify(body),
});

test('requests become allowlisted CLI arguments', () => {
  expect(toArgs({ mode: 'sync' })).toEqual([]);
  expect(toArgs({ mode: 'check' })).toEqual(['-c']);
  expect(toArgs({ mode: 'dry' })).toEqual(['-n']);
  expect(toArgs({ mode: 'write' })).toEqual(['--excel']);
});

test('bad input never reaches the CLI', () => {
  for (const body of [{ mode: 'rm' }, { mode: 'verify' }, {}]) expect(() => toArgs(body)).toThrow();
});

test('only same-origin JSON posts on localhost are accepted', async () => {
  const started: string[][] = [];
  const begin = (args: string[]) => { started.push(args); };
  expect((await handle(new Request('http://evil.example/api/log'), begin)).status).toBe(403);
  expect((await handle(post('/api/run', { mode: 'check' }, { origin: 'http://evil.example' }), begin)).status).toBe(403);
  expect((await handle(post('/api/run', { mode: 'check' }, { 'content-type': 'text/plain' }), begin)).status).toBe(403);
  expect((await handle(post('/api/run', { mode: 'nope' }), begin)).status).toBe(400);
  expect(started).toEqual([]);
  expect((await handle(post('/api/run', { mode: 'check' }), begin)).status).toBe(200);
  expect(started).toEqual([['-c']]);
});

test('the overview endpoint serves read-only check lines', async () => {
  const res = await handle(new Request('http://127.0.0.1:4317/api/overview'), () => {}, async () => ['[check] 1 incomplete']);
  expect(await res.json()).toEqual({ lines: ['[check] 1 incomplete'] });
});

test('the column map is validated before it is saved', async () => {
  const saved: unknown[] = [];
  const save = async (m: unknown) => { saved.push(m); };
  const call = (body: unknown) => handle(post('/api/map', body), () => {}, async () => [], save as never);
  expect((await call({ 'Inv No': 'nope.field' })).status).toBe(400);
  expect((await call({})).status).toBe(400);
  expect((await call({ 'LEO Date': 'bills.leo_date' })).status).toBe(200);
  expect(saved).toEqual([{ 'LEO Date': 'bills.leo_date' }]);
});

test('the database view is empty without report.db', () => {
  expect(dbView('/nonexistent/report.db')).toEqual({ total: 0, bills: [], missing_iecs: [] });
});

test('the log endpoint reports an idle server', async () => {
  const body = await (await handle(new Request('http://localhost:4317/api/log?from=0'))).json() as { running: boolean; lines: string[] };
  expect(body.running).toBe(false);
  expect(body.lines).toEqual([]);
});
