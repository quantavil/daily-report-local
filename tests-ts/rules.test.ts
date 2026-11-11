import { describe, expect, test } from 'bun:test';
import { bill_identity, is_cancelled, label_matches, parse_date, stuffing_of, value_token } from '../src/ts/rules';

describe('ICEGATE policy', () => {
  test('stuffing: every seal needs a letter for factory stuffing; no seal stays blank', () => {
    expect(stuffing_of(['BOLT52524390', 'KLIK20805680'])).toBe('Factory stuffing');
    expect(stuffing_of(['0227948'])).toBe('');
    expect(stuffing_of(['BOLT52524390', '0227948'])).toBe('');
    expect(stuffing_of(['', ''])).toBe('');
  });
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
    expect(value_token('container_size', '40.')).toBe('40');
    expect(value_token('container_size', '4410')).toBe('40');
    expect(value_token('customs_seal', 'BOLT51634360.')).toBe('BOLT51634360');
    expect(value_token('customs_seal', 'BOL.T51738219')).toBe('BOLT51738219');
    expect(value_token('customs_seal', 'N/A')).toBeNull();
    expect(() => value_token('customs_seal', 'BAD SEAL!')).toThrow();
    expect(() => value_token('container_no', 'ABC')).toThrow();
    expect(label_matches('container_no', '11. Container')).toBe(true);
  });
  test('cancellation policy recognises portal cancellations and stale exam queues without LEO', () => {
    const now = new Date('2026-11-02T12:00:00Z');
    // Explicit portal cancel statuses
    expect(is_cancelled({ curr_status: 'Purge after 30 days' }, now)).toBe(true);
    expect(is_cancelled({ curr_status: 'SB is Cancelled' }, now)).toBe(true);
    expect(is_cancelled({ curr_status: 'LEO CANCELLED' }, now)).toBe(true);
    expect(is_cancelled({ curr_status: 'CANCELLED (set by hand)' }, now)).toBe(true);

    // Stale exam queue (>30 days without LEO)
    expect(is_cancelled({ curr_status: 'TO BE MARKED', sb_date: '2026-09-01', leo_date: '' }, now)).toBe(true);
    expect(is_cancelled({ curr_status: 'TO BE EXAMINED', sb_date: '2026-01-28', leo_date: '' }, now)).toBe(true);

    // LEO check: if LEO date is present, it is NOT cancelled even if older than 30 days
    expect(is_cancelled({ curr_status: 'TO BE MARKED', sb_date: '2026-09-01', leo_date: '2026-09-02' }, now)).toBe(false);

    // Fresh exam queue (<30 days) is NOT cancelled (active live shipment)
    expect(is_cancelled({ curr_status: 'TO BE MARKED', sb_date: '2026-10-25', leo_date: '' }, now)).toBe(false);

    // Other non-cancel statuses
    expect(is_cancelled({ curr_status: 'STUFF ALLOW', sb_date: '2026-09-01', leo_date: '2026-09-02' }, now)).toBe(false);
    expect(is_cancelled({ curr_status: 'EXAM OVER', sb_date: '2026-09-01', leo_date: '2026-09-02' }, now)).toBe(false);
  });
});
