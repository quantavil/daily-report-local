import { describe, expect, test } from 'bun:test';
import { parse_filename, filename_prefix, batch_name, storage_year, gmail_identity_terms, matches_identity, value_token, label_matches, discovery_codes, create_identity } from '../src/ts/rules';
import { attachment_key } from '../src/ts/models';

const filename = '754640407102026INMBD6SB22081020261432.pdf';
describe('ICEGATE policy', () => {
  test('canonical filename parsing preserves calendar identity', () => {
    const m = parse_filename(filename);
    expect(m.identity).toEqual({sb_no:'7546404', sb_date:'2026-10-07', port_code:'INMBD6', document_date:'2026-10-08'});
    expect(m.document_at).toBe('2026-10-08T14:32:00');
    expect(filename_prefix(m.identity, 'SB22')).toBe(filename.slice(0,-8));
    expect(batch_name(m.identity)).toBe('754640407102026INMBD6');
    expect(storage_year(m.identity)).toBe('2026');
    expect(gmail_identity_terms(m.identity)).toEqual(['7546404', 'No7546404Dt07102026']);
  });
  test('rejects invalid dates, times, codes and unsafe names', () => {
    for (const name of [filename.replace('07102026','32102026'),filename.replace('1432','2460'),filename.replace('SB22','SB23'),'../'+filename,filename.replace('07102026','29022023')]) expect(() => parse_filename(name)).toThrow();
  });
  test('identity normalization and unknown components', () => {
    const id = create_identity({sb_no:'7546404',port_code:'inmbd6'});
    expect(id.port_code).toBe('INMBD6');
    expect(matches_identity(id,parse_filename(filename).identity)).toBe(true);
    expect(filename_prefix(id,'SB22')).toBe('7546404');
    expect(gmail_identity_terms(id)).toEqual([]);
    expect(() => create_identity({sb_no:'../bad'})).toThrow();
  });
  test('field tokens retain ownership and reject unsafe cells', () => {
    expect(value_token('leo_date','08/10/2026')).toBe('2026-10-08');
    expect(value_token('inv_no','INV/5 08/10/2026 USD 10')).toBe('INV/5');
    expect(() => value_token('inv_no','08/10/2026')).toThrow();
    expect(() => value_token('leo_date','08/10-2026')).toThrow();
    expect(() => value_token('shipper_name','123 MAIN ROAD')).toThrow();
    expect(label_matches('shipper_name',"1. EXPORTER'S NAME & ADDRESS")).toBe(true);
    expect([...discovery_codes(new Set(['leo_date']))]).toEqual(['SB22']);
  });
  test('stable MIME identity', () => {
    const metadata = parse_filename(filename);
    const a = {metadata,message_id:'m',part_id:'1',attachment_id:'old'};
    expect(attachment_key(a)).toBe(attachment_key({...a,attachment_id:'new'}));
  });
});
