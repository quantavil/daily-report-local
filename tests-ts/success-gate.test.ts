import { describe, expect, test } from 'bun:test';
import { DATA_FIELDS, LEO_FIELDS, FIELD_DOCUMENTS } from '../src/ts/rules';
import { make_update } from '../src/ts/workflow';
import type { BillResult, DocumentCode, Evidence, FieldName, WorkbookRow } from '../src/ts/models';

const identity = { sb_no: '7546404', sb_date: '2026-10-07', port_code: 'INMBD6' };
const codes: DocumentCode[] = ['SB21', 'SB22', 'SB35'];
const evidence = (code: DocumentCode, over: Partial<Evidence> = {}): Evidence => ({ filename: `${code}.pdf`, content_hash: `h-${code}`, page_number: 1, label: 'x', quote: 'x', bbox: [0, 0, 1, 1], ...over });
const values: Record<FieldName, string> = { shipper_name: 'ACME', inv_no: 'INV1', leo_date: '2026-10-08', container_no: 'ABCU1234567', container_size: '20', customs_seal: 'S1' };
function result(over: Partial<BillResult> = {}, evidenceOver: Partial<Evidence> = {}): BillResult {
  const fields: BillResult['fields'] = {};
  for (const name of DATA_FIELDS) {
    const code = FIELD_DOCUMENTS[name], records = ['container_no', 'container_size', 'customs_seal'].includes(name);
    fields[name] = records ? { state: 'accepted', value_type: 'records', evidence: [evidence(code, evidenceOver)] } : { state: 'accepted', value: values[name], evidence: [evidence(code, evidenceOver)] };
  }
  return {
    identity, fields, containers: [{ number: values.container_no, size: '20', seal: 'S1', evidence: [evidence('SB35')] }],
    selected_sources: codes.map(code => ({ identity, code, document_at: '2026-10-08T10:00', filename: `${code}.pdf` })),
    selected_hashes: { SB21: 'h-SB21', SB22: 'h-SB22', SB35: 'h-SB35' }, issues: [], errors: [], ...over,
  };
}
const row = (v: Partial<Record<FieldName, string>> = {}): WorkbookRow => ({ row_idx: 2, identity, cancelled: false, values: v });

describe('Success gate (Success rows are skipped on later runs)', () => {
  test('all fields from consistent sources and an empty row is Success', () => {
    expect(make_update(row(), result()).status).toBe('Success');
  });
  test('a --leo-only run never marks a row Success, even if other columns are filled manually', () => {
    const filled = row(values);
    expect(make_update(filled, result(), new Set(LEO_FIELDS)).status).toBe('Partial');
  });
  test('evidence that does not match the selected source hash is Partial', () => {
    expect(make_update(row(), result({}, { content_hash: 'tampered' })).status).toBe('Partial');
  });
  test('evidence from a page outside the allowed pages is Partial', () => {
    expect(make_update(row(), result({}, { page_number: 9 })).status).toBe('Partial');
  });
  test('a missing document source is Partial', () => {
    const r = result(); r.selected_sources = r.selected_sources.filter(s => s.code !== 'SB22');
    expect(make_update(row(), r).status).toBe('Partial');
  });
  test('incomplete container records are Partial', () => {
    const r = result(); r.containers = [{ number: 'ABCU1234567', size: '20', seal: null, evidence: [] }];
    expect(make_update(row(), r).status).toBe('Partial');
  });
});
