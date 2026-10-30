import { expect, test } from 'bun:test';
import { main, parseRowSpec } from '../src/ts/cli';
import { fixture } from './fixture';

test('row specs expand lists and ranges', () => {
  expect([...parseRowSpec('2,4-6')]).toEqual([2, 4, 5, 6]);
});

test('row specs reject descending ranges, row 1 and junk', () => {
  for (const spec of ['6-4', '1-3', 'a', '2-3-4', '']) expect(() => parseRowSpec(spec)).toThrow();
});

test('conflicting options fail before touching the workbook', async () => {
  const original = console.error;
  console.error = () => {};
  try {
    expect(await main(['-c', '-n'])).toBe(1);
    expect(await main(['-v', '-q'])).toBe(1);
    expect(await main(['--fy', '2026-27', '-s', 'x'])).toBe(1);
    expect(await main(['--latest', '--fy', '2026-27'])).toBe(1);
    expect(await main(['--bogus'])).toBe(1);
  } finally {
    console.error = original;
  }
});

test('the check summary the dashboard parses keeps its shape and counts rows to sync', async () => {
  const path = await fixture([
    ['7546404', '07/10/2026', 'INMBD6', null, null, null, null, null, null, null, 'Partial'],
    ['7546405', '07/10/2026', 'INMBD6', null, null, null, null, null, null, 'Cancelled', null],
    ['7546406', '07/10/2026', 'INMBD6', 'INV-1', '08/10/2026', 'Acme Exports', 'ABCD1234567', '40', 'S1', null, 'Success'],
  ]);
  const lines: string[] = [], original = console.log;
  console.log = (text: string) => { lines.push(text); };
  try { expect(await main(['-c', '-f', path])).toBe(0); } finally { console.log = original; }
  expect(lines.some(l => /^\[row 2\] SB 7546404 {2}missing {2}/.test(l))).toBe(true);
  expect(lines.at(-1)).toMatch(/^\[check\] 1 incomplete · 1 cancelled · 0 identity error\(s\) · 1 to sync · 3 rows$/);
});

test('check reports invalid scoped identities with a nonzero exit status', async () => {
  const path = await fixture([['123', '07/10/2026', 'INMBD6']]);
  const lines: string[] = [], original = console.log;
  console.log = (text: string) => { lines.push(text); };
  try { expect(await main(['-c', '-f', path])).toBe(1); } finally { console.log = original; }
  expect(lines.some(line => line.includes('identity error(s)'))).toBe(true);
});
