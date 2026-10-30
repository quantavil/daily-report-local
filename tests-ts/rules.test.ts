import { describe, expect, test } from 'bun:test';
import { bill_identity, label_matches, parse_date, value_token } from '../src/ts/rules';

describe('ICEGATE policy', () => {
  test('portal placeholders are blank for tokens and stored values alike', () => {
    for (const p of ['N.A.', 'N/A', 'NONE', '-']) expect(value_token('customs_seal', p)).toBeNull();
  });
  test('dates normalise to ISO, with or without a time', () => {
    expect(parse_date('07/10/2026')).toBe('2026-10-07');
    expect(parse_date('07 SEP 2026 00:09')).toBe('2026-09-07');
    expect(parse_date('07-Oct-26')).toBe('2026-10-07');
    for (const bad of ['32/10/2026', '29/02/2023', '08/10-2026', 'soon']) expect(() => parse_date(bad)).toThrow();
  });
  test('bill identity needs seven SB digits, a date and a six-character port', () => {
    expect(bill_identity('7546404', '07/10/2026', 'inmbd6')).toEqual({ sb_no: '7546404', sb_date: '2026-10-07', port_code: 'INMBD6' });
    expect(() => bill_identity('../bad', '07/10/2026', 'INMBD6')).toThrow();
    expect(() => bill_identity('7546404', '07/10/2026', 'X')).toThrow();
    expect(() => bill_identity('7546404', 'soon', 'INMBD6')).toThrow();
  });
  test('field tokens reject unsafe cells', () => {
    expect(value_token('sb_date', '08/10/2026')).toBe('2026-10-08');
    expect(value_token('container_size', '40')).toBe('40');
    expect(value_token('customs_seal', 'N/A')).toBeNull();
    expect(() => value_token('customs_seal', 'BAD SEAL!')).toThrow();
    expect(() => value_token('container_no', 'ABC')).toThrow();
    expect(label_matches('container_no', '11. Container')).toBe(true);
  });
});
