import { describe, expect, test } from 'bun:test';
import { gatepass_identity, parse_gatepass, parse_layout } from '../../src/ts/gatepass/parse';
import { pdf } from '../fixture';
import type { LayoutSpan } from '../../src/ts/models';

const bill = { sb_no: '7546404', sb_date: '2026-10-07', port_code: 'INMBD6' };
const span = (text: string, x: number, y: number, w: number): LayoutSpan => ({ text, bbox: [x, y, x + w, y + 10] });
const identity = (): LayoutSpan[] => [span('SB No', 20, 20, 50), span('7546404', 80, 20, 70), span('SB Date', 200, 20, 60), span('07/10/2026', 270, 20, 90), span('Port Code', 390, 20, 70), span('INMBD6', 470, 20, 65)];
const layout = (...pages: LayoutSpan[][]) => pages.map((spans, i) => ({ number: i + 1, width: 600, height: 800, spans }));
const headers = () => [span('11. Container', 20, 100, 150), span('12. Seal', 200, 100, 120), span('14. Size', 400, 100, 80)];
const stackedHeaders = () => [span('11,Container', 144, 183, 46), span('ABCU1234567', 263, 183, 54), span('12.Seal', 72, 197, 27), span('SEAL1', 120, 197, 56), span('13.Seal Type', 184, 197, 48), span('BOLT', 266, 197, 20), span('14.Size', 315, 197, 27), span('40', 366, 197, 9)];
const row = (no: string, seal: string | null, size: string, y: number) => [span(no, 20, y, 120), ...(seal ? [span(seal, 200, y, 90)] : []), span(size, 400, y, 40)];

describe('gatepass parsing', () => {
  test('keeps ordered records and an empty seal position across a continuation page', () => {
    const first = [...identity(), ...headers(), ...row('ABCU1234567', 'SEAL1', '40', 130), ...row('DEFU7654321', null, '20', 150)];
    const second = row('GHIU1234567', 'SEAL3', '40', 40);
    expect(parse_layout(layout(first, second), bill).containers).toEqual([
      { container_no: 'ABCU1234567', size: '40', seal: 'SEAL1' }, { container_no: 'DEFU7654321', size: '20', seal: '' }, { container_no: 'GHIU1234567', size: '40', seal: 'SEAL3' }]);
  });
  test('reads stacked movement fields on page four', () => {
    expect(parse_layout(layout(identity(), [], [], stackedHeaders()).filter(p => p.spans.length), bill).containers).toEqual([{ container_no: 'ABCU1234567', size: '40', seal: 'SEAL1' }]);
  });
  test('continues a table after the next page bill header', () => {
    const next = identity(); next[0] = span('1. SB No', 20, 20, 55);
    next.push(...row('DEFU7654321', 'SEAL2', '20', 100));
    const first = [...identity(), ...headers(), ...row('ABCU1234567', 'SEAL1', '40', 130)];
    expect(parse_layout(layout(first, next), bill).containers.map(c => c.container_no)).toEqual(['ABCU1234567', 'DEFU7654321']);
  });
  test('rejects an unsupported continuation instead of truncating', () => {
    const first = [...identity(), ...headers(), ...row('ABCU1234567', 'SEAL1', '40', 130)];
    const second = [span('1. UNKNOWN TABLE', 20, 20, 180), ...row('DEFU7654321', 'SEAL2', '20', 100)];
    expect(() => parse_layout(layout(first, second), bill)).toThrow('continuation');
  });
  test('rejects a headerless stacked continuation', () => {
    const continuation = [span('DEFU7654321', 263, 40, 54), span('SEAL2', 120, 54, 56), span('20', 366, 54, 9)];
    expect(() => parse_layout(layout([...identity(), ...stackedHeaders()], continuation), bill)).toThrow('Headerless stacked');
  });
  test('an invalid seal rejects the whole document, never guessed', () => {
    expect(() => parse_layout(layout([...identity(), ...headers(), ...row('ABCU1234567', 'BAD SEAL!', '40', 130)]), bill)).toThrow('Invalid customs_seal');
  });
  test('identity must match the requested bill', () => {
    const other = identity(); other[1] = span('1111111', 80, 20, 70);
    expect(() => parse_layout(layout([...other, ...headers(), ...row('ABCU1234567', 'S', '40', 130)]), bill)).toThrow('conflicts');
    expect(() => parse_layout(layout([...headers(), ...row('ABCU1234567', 'S', '40', 130)]), bill)).toThrow('identity');
  });
  test('the secondary gatepass header must match the bill date', () => {
    const page = [...identity(), span('1.SB NO', 72, 107, 32), span('7546404', 124, 107, 31), span('2.BL NO', 166, 107, 33), span('3.BL DT', 315, 107, 30), span('4.SB DT', 72, 120.5, 30), span('07-Oct-26', 117, 120.5, 40), span('5.HBL No', 166, 120.5, 34), ...stackedHeaders()];
    expect(parse_layout(layout(page), bill).containers).toHaveLength(1);
    page[11] = span('08-Oct-26', 117, 120.5, 40);
    expect(() => parse_layout(layout(page), bill)).toThrow('conflicts');
  });
  test('reads the IEC beside its label; two different IECs reject the document', () => {
    const iec = (v: string, y: number) => [span('IEC/Br', 20, y, 40), span(v, 100, y, 80)];
    const page = [...identity(), ...iec('0123456789', 40), ...headers(), ...row('ABCU1234567', 'S', '40', 130)];
    expect(parse_layout(layout(page), bill).iec).toBe('0123456789');
    expect(parse_layout(layout([...page, ...iec('0123456789', 60)]), bill).iec).toBe('0123456789');
    expect(() => parse_layout(layout([...page, ...iec('9999999999', 60)]), bill)).toThrow('several IECs');
    expect(() => parse_layout(layout([...identity(), ...iec('BAD', 40), ...headers(), ...row('ABCU1234567', 'S', '40', 130)]), bill)).toThrow('Invalid iec');
  });
  test('an unreadable PDF is an explicit failure', () => {
    expect(() => parse_gatepass(new TextEncoder().encode('not a pdf'), bill)).toThrow();
  });
  test('reads container rows from real PDF geometry', () => {
    const lines: [string, number, number][] = [['SB No', 20, 760], ['7546404', 100, 760], ['SB Date', 200, 760], ['07/10/2026', 280, 760], ['Port Code', 400, 760], ['INMBD6', 480, 760], ['IEC/Br', 20, 730], ['0123456789', 100, 730],
      ['11. Container', 20, 700], ['12. Seal', 200, 700], ['14. Size', 400, 700], ['ABCU1234567', 20, 680], ['SEAL1', 200, 680], ['40', 400, 680]];
    expect(parse_gatepass(pdf(lines), bill)).toEqual({ iec: '0123456789', exporter: '', containers: [{ container_no: 'ABCU1234567', size: '40', seal: 'SEAL1' }] });
  });
  test('reads the exporter on the row of its 3.Exporter label, not the CHA beside it', () => {
    const head = [span('3.Exporter', 20, 60, 50), span('ACME  EXPORTS PVT LTD', 80, 60, 140), span('4.CHA', 300, 60, 30), span('VISHAL LOGISTICS', 340, 60, 100)];
    expect(parse_layout(layout([...identity(), ...head, ...headers(), ...row('ABCU1234567', 'S', '40', 130)]), bill).exporter).toBe('ACME EXPORTS PVT LTD');
  });
  test('a gatepass without an exporter label leaves the name blank; differing exporters reject it', () => {
    const body = [...identity(), ...headers(), ...row('ABCU1234567', 'S', '40', 130)];
    const named = (name: string, y = 60) => [span('3.Exporter', 20, y, 50), span(name, 80, y, 100)];
    expect(parse_layout(layout(body), bill).exporter).toBe('');
    const next = (name: string) => [...row('DEFU7654321', 'S2', '20', 100), ...named(name, 200)];
    expect(parse_layout(layout([...body, ...named('A LTD')], next('A LTD')), bill).exporter).toBe('A LTD');
    expect(() => parse_layout(layout([...body, ...named('A LTD')], next('B LTD')), bill)).toThrow('several exporters');
  });
  test('names the bill a PDF is for, and rejects a PDF whose headers disagree or are missing', () => {
    const id = (no: string, date: string, y = 760): [string, number, number][] => [['SB No', 20, y], [no, 100, y], ['SB Date', 200, y], [date, 280, y], ['Port Code', 400, y], ['INMBD6', 480, y]];
    expect(gatepass_identity(pdf(id('7546404', '07/10/2026')))).toEqual(bill);
    expect(() => gatepass_identity(pdf([...id('7546404', '07/10/2026'), ...id('7546404', '08/10/2026', 600)]))).toThrow('no single sb_date');
    expect(() => gatepass_identity(pdf([['3.Exporter', 20, 740]]))).toThrow();
  });
});
