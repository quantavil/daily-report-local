import { describe, expect, test } from 'bun:test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_MAP, load_map, parse_map } from '../../src/ts/excel/export-map';

describe('export map', () => {
  test('accepts catalog fields and rejects unknown ones', () => {
    expect(parse_map({ 'LEO Date': 'bills.leo_date' })).toEqual({ 'LEO Date': 'bills.leo_date' });
    expect(() => parse_map({ 'LEO Date': 'bills.nope' })).toThrow('export-map.json');
    expect(() => parse_map({})).toThrow('maps no columns');
  });
  test('falls back to the default map when the file is absent', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'map-'));
    expect(await load_map(join(dir, 'export-map.json'))).toEqual(DEFAULT_MAP);
    await writeFile(join(dir, 'm.json'), '{"Exporter":"bills.exporter"}');
    expect(await load_map(join(dir, 'm.json'))).toEqual({ 'Exporter': 'bills.exporter' });
    await writeFile(join(dir, 'bad.json'), 'not json');
    await expect(load_map(join(dir, 'bad.json'))).rejects.toThrow('not valid JSON');
  });
});
