import { test, expect } from 'bun:test';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { BillStorage } from '../src/ts/storage';
import { parse_filename } from '../src/ts/rules';

const first = { metadata:parse_filename('754640407102026INMBD6SB22081020260915.pdf'),message_id:'m',part_id:'1' };
const newer = { metadata:parse_filename('754640407102026INMBD6SB22081020260916.pdf'),message_id:'m2',part_id:'1' };

test('reuse validates hashes and collisions preserve original bytes', async () => {
 const root=await mkdtemp(join(tmpdir(),'report-')); try {
  const store=new BillStorage(root,first.metadata.identity);
  await store.put_pdf(first,Buffer.from('original'));
  expect(Buffer.from((await store.get_pdf(first))!).toString()).toBe('original');
  expect(await store.get_pdf({...first,inline_data:Buffer.from('other').toString('base64url')})).toBeNull();
  await expect(store.put_pdf({...first,message_id:'other'},Buffer.from('different'))).rejects.toThrow();
  expect(Buffer.from((await store.get_pdf(first))!).toString()).toBe('original');
 } finally { await rm(root,{recursive:true,force:true}); }
});

test('newer failed candidate blocks older local evidence and survives scoped result', async () => {
 const root=await mkdtemp(join(tmpdir(),'report-')); try {
  const store=new BillStorage(root,first.metadata.identity); await store.put_pdf(first,Buffer.from('original'));
  await store.put_result({identity:first.metadata.identity,fields:{},containers:[],selected_sources:[newer.metadata],selected_hashes:{},issues:[],errors:['download failed']});
  await store.put_result({identity:first.metadata.identity,fields:{},containers:[],selected_sources:[],selected_hashes:{},issues:[],errors:[]});
  await expect(BillStorage.discover_local(root,first.metadata.identity,new Set(['SB22']))).rejects.toThrow('newest');
  await expect(store.put_pdf(first,Buffer.from('original'))).rejects.toThrow('newer');
 } finally { await rm(root,{recursive:true,force:true}); }
});

test('local discovery has no filesystem side effects', async () => {
 const root=await mkdtemp(join(tmpdir(),'report-')); try {
  const missing=join(root,'missing'); expect(await BillStorage.discover_local(missing,first.metadata.identity,new Set(['SB22']))).toEqual([]); expect(await readdir(root)).toEqual([]);
  const store=new BillStorage(root,first.metadata.identity); await store.put_pdf(first,Buffer.from('original'));
  const resultPath=join(store.path,'processed',first.metadata.filename.replace('.pdf','.json'));
  const before=await readFile(resultPath,'utf8');
  const found=await BillStorage.discover_local(root,first.metadata.identity,new Set(['SB22']));
  expect(found).toHaveLength(1); expect(await readFile(resultPath,'utf8')).toBe(before);
 } finally { await rm(root,{recursive:true,force:true}); }
});

test('old Python layout cache is invalidated while retained original remains reusable', async () => {
 const root=await mkdtemp(join(tmpdir(),'report-')); try {
  const store=new BillStorage(root,first.metadata.identity); const digest=await store.put_pdf(first,Buffer.from('original'));
  const path=join(store.path,'processed',first.metadata.filename.replace('.pdf','.json'));
  const record=JSON.parse(await readFile(path,'utf8')); record.layout_versions={layout:'2',rules:'5',pymupdf:'1.28.2'};
  record.layout={document_code:'SB22',pages:[]}; await writeFile(path,JSON.stringify(record));
  expect(await store.get_layout(first.metadata,digest)).toBeNull();
  expect((await store.get_pdf(first))?.toString()).toBe('original');
 } finally { await rm(root,{recursive:true,force:true}); }
});

test('concurrent stores preserve every source reference', async () => {
 const root=await mkdtemp(join(tmpdir(),'report-')); try {
  await Promise.all(Array.from({length:5},(_,i)=>new BillStorage(root,first.metadata.identity).put_pdf({...first,message_id:`m${i}`},Buffer.from('original'))));
  const store=new BillStorage(root,first.metadata.identity,true);
  for(let i=0;i<5;i++) expect((await store.get_pdf({...first,message_id:`m${i}`}))?.toString()).toBe('original');
 } finally { await rm(root,{recursive:true,force:true}); }
});
