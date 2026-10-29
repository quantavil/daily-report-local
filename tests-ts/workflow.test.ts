import { describe, test, expect } from 'bun:test';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execute, make_update } from '../src/ts/workflow';
import type { WorkbookRow, BillResult, Evidence } from '../src/ts/models';

const identity = { sb_no: '7546404', sb_date: '2026-10-07', port_code: 'INMBD6' };
const row = (overrides: Partial<WorkbookRow> = {}): WorkbookRow => ({row_idx: 2, identity, values: {}, cancelled: false, ...overrides});
class Book {
  applied = 0; saved = 0;
  apply() { this.applied++; }
  async save() { this.saved++; }
}
class Mail {
  auth = 0; discoveries = 0; fail = false;
  async authenticate() { this.auth++; if (this.fail) throw new Error('login needed'); }
  async discover() { this.discoveries++; return []; }
  async download(): Promise<Uint8Array> { throw new Error('unexpected'); }
}
const empty = (fields: BillResult['fields'] = {}): BillResult => ({identity, fields, containers: [], selected_sources: [], selected_hashes: {}, issues: [], errors: []});
const evidence: Evidence = {filename:'synthetic.pdf',content_hash:'x',page_number:1,label:'6. LEO Date',quote:'08/10/2026',bbox:[0,0,1,1]};

describe('workflow', () => {
 test('skips successful rows without authentication, Gmail, or writes', async () => {
   const root = await mkdtemp(join(tmpdir(), 'workflow-')); const b = new Book(), g = new Mail();
   try {
     const records = await execute(b, 'FY 2026-27', g, root, {selected:[row({status:'Success'})]});
     expect(records[0]![2].status).toBe('Success'); expect(g.auth).toBe(0); expect(g.discoveries).toBe(0); expect(b.applied).toBe(0); expect(await readdir(root)).toEqual([]);
   } finally { await rm(root,{recursive:true,force:true}); }
 });
 test('failed authentication stops all processing before workbook and artifacts change', async () => {
   const root = await mkdtemp(join(tmpdir(), 'workflow-')); const b = new Book(), g = new Mail(); g.fail = true;
   try {
     await expect(execute(b, 'FY 2026-27', g, root, {selected:[row()]})).rejects.toThrow('login needed');
     expect(g.discoveries).toBe(0); expect(b.applied).toBe(0); expect(await readdir(root)).toEqual([]);
   } finally { await rm(root,{recursive:true,force:true}); }
 });
 test('force rechecks successful rows', async () => {
   const root = await mkdtemp(join(tmpdir(), 'workflow-')); const b = new Book(), g = new Mail();
   try { await execute(b, 'FY 2026-27', g, root, {selected:[row({status:'Success'})], force:true}); expect(g.auth).toBe(1); expect(g.discoveries).toBe(1); }
   finally { await rm(root,{recursive:true,force:true}); }
 });
 test('verify reads local evidence only and creates no directories', async () => {
   const root = await mkdtemp(join(tmpdir(), 'workflow-')); const b = new Book(), g = new Mail();
   try { await execute(b, 'FY 2026-27', g, root, {selected:[row()], verify:true}); expect(g.auth).toBe(0); expect(g.discoveries).toBe(0); expect(b.applied).toBe(0); expect(await readdir(root)).toEqual([]); }
   finally { await rm(root,{recursive:true,force:true}); }
 });
 test('cancelled wins over identity error and never authenticates', async () => {
   const root = await mkdtemp(join(tmpdir(), 'workflow-')); const b = new Book(), g = new Mail();
   try { const records = await execute(b, 'FY 2026-27', g, root, {selected:[row({cancelled:true,error:'invalid identity'})]}); expect(records[0]![2].status).toBe('Cancelled'); expect(g.auth).toBe(0); }
   finally { await rm(root,{recursive:true,force:true}); }
 });
 test('protects formulas and mismatched retained container columns', () => {
   const full = row({values:{shipper_name:'E',inv_no:'I',leo_date:'=TODAY()',container_no:'A',container_size:'20',customs_seal:'S'}});
   const update = make_update(full, empty({leo_date:{state:'accepted',value:'2026-10-08',evidence:[evidence]}}), new Set(['leo_date']), true);
   expect(update.status).toBe('Partial'); expect(update.values.leo_date).toBeUndefined();
   const triplet = make_update(row({values:{container_no:'OTHER',container_size:'20'}}), {...empty({customs_seal:{state:'accepted',value_type:'records',evidence:[evidence]},container_no:{state:'accepted',value_type:'records',evidence:[evidence]},container_size:{state:'accepted',value_type:'records',evidence:[evidence]}}),containers:[{number:'ABCU1234567',size:'20',seal:'S',evidence:[evidence]}]}, new Set(['container_no','container_size','customs_seal']), false);
   expect(triplet.container_fields.size).toBe(0); expect(triplet.status).toBe('Partial');
 });
});
