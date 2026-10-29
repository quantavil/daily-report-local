/** Private retained originals and processed evidence. */
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, readdir, rename, rm, open, chmod } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import mupdf_package from '../../node_modules/mupdf/package.json';
import lockfile from 'proper-lockfile';
import type { Attachment, BillIdentity, BillResult, DocumentCode, DocumentLayout, DocumentMetadata, ParsedDocument } from './models';
import * as rules from './rules';

export class StorageError extends Error {}
type RecordData = { metadata: DocumentMetadata; content_hash: string; sources: {message_id:string;part_id:string|null}[]; layout?:DocumentLayout; layout_versions?:object; fields?:unknown; containers?:unknown; parser_version?:string };
const hash=(raw:Uint8Array)=>createHash('sha256').update(raw).digest('hex');
const same=(a:unknown,b:unknown)=>isDeepStrictEqual(a,b);
const source=(a:Attachment)=>({message_id:a.message_id,part_id:a.part_id||null});
const versions=()=>({layout:rules.LAYOUT_VERSION,rules:rules.RULES_VERSION,mupdf_js:mupdf_package.version});
async function names(path:string):Promise<string[]> { try { return (await readdir(path)).sort(); } catch(e) { if ((e as NodeJS.ErrnoException).code==='ENOENT') return []; throw e; } }
async function json(path:string):Promise<any> { try { return JSON.parse(await readFile(path,'utf8')); } catch(e) { if ((e as NodeJS.ErrnoException).code==='ENOENT' || e instanceof SyntaxError) return null; throw e; } }
async function exists(path:string):Promise<boolean> { try { await readFile(path); return true; } catch(e) { if ((e as NodeJS.ErrnoException).code==='ENOENT') return false; throw e; } }

export class BillStorage {
 readonly identity:BillIdentity; readonly path:string;
 constructor(readonly root:string,identity:BillIdentity,readonly readonly_mode=false) {
  this.identity={...identity,document_date:null}; this.path=join(root,rules.storage_year(identity),rules.batch_name(identity));
 }
 private async locked<T>(fn:()=>Promise<T>):Promise<T> {
  if(this.readonly_mode) return fn();
  await mkdir(this.path,{recursive:true,mode:0o700});
  if(process.platform!=='win32') for(const directory of [this.root,dirname(this.path),this.path]) await chmod(directory,0o700);
  const release=await lockfile.lock(this.path,{realpath:false,retries:{retries:100,minTimeout:20,maxTimeout:100},stale:30000});
  try { return await fn(); } finally { await release(); }
 }
 private async atomic(path:string,content:Uint8Array|string):Promise<void> {
  if(this.readonly_mode) throw new StorageError('Read-only storage cannot write');
  await mkdir(dirname(path),{recursive:true,mode:0o700});
  if(process.platform!=='win32') await chmod(dirname(path),0o700);
  const temp=join(dirname(path),`.write-${randomUUID()}`);
  try { const f=await open(temp,'wx',0o600); try { await f.writeFile(content); await f.sync(); } finally { await f.close(); } await rename(temp,path); }
  finally { await rm(temp,{force:true}); }
 }
 private async write_json(path:string,value:unknown) { await this.atomic(path,JSON.stringify(value,null,2)); }
 private document_path(metadata:DocumentMetadata):string {
  let parsed:DocumentMetadata; try { parsed=rules.parse_filename(metadata.filename); } catch { throw new StorageError('Unsafe or noncanonical attachment filename'); }
  if(parsed.code!==metadata.code || parsed.document_at!==metadata.document_at || !rules.matches_identity(parsed.identity,metadata.identity) || !rules.matches_identity(metadata.identity,parsed.identity) || !rules.matches_identity(this.identity,metadata.identity)) throw new StorageError('Filename metadata does not match document metadata');
  return join(this.path,'processed',metadata.filename.replace(/\.pdf$/,'.json'));
 }
 private async record(metadata:DocumentMetadata,digest?:string):Promise<RecordData|null> {
  const record=await json(this.document_path(metadata));
  if(!record || !same(record.metadata,metadata) || (digest!==undefined&&record.content_hash!==digest)) return null;
  return record as RecordData;
 }
 private async saved_metadata():Promise<DocumentMetadata[]> {
  const result:DocumentMetadata[]=[];
  for(const name of (await names(join(this.path,'pdfs'))).filter(n=>n.endsWith('.pdf'))) {
   let metadata:DocumentMetadata; try { metadata=rules.parse_filename(name); } catch { throw new StorageError(`Malformed retained PDF filename: ${name}`); }
   if(!rules.matches_identity(this.identity,metadata.identity)) throw new StorageError(`Retained PDF identity conflicts with batch: ${name}`);
   result.push(metadata);
  }
  return result;
 }
 private async observed(payload?:any):Promise<Map<DocumentCode,DocumentMetadata>> {
  if(payload===undefined) { const p=join(this.path,'result.json'); payload=await json(p); if(payload===null && !(await exists(p))) return new Map(); }
  if(!payload||!Array.isArray(payload.selected_sources)) throw new StorageError('Corrupt saved batch source metadata');
  const result=new Map<DocumentCode,DocumentMetadata>();
  for(const item of payload.selected_sources) {
   try { this.document_path(item); } catch { throw new StorageError('Corrupt saved batch source metadata'); }
   const previous=result.get(item.code as DocumentCode); if(!previous || item.document_at>previous.document_at) result.set(item.code,item);
  }
  return result;
 }
 private async retained(metadata:DocumentMetadata,digest:string):Promise<RecordData> {
  const record=await this.record(metadata,digest); if(!record) throw new StorageError('Processing requires matching retained document');
  let raw:Buffer; try { raw=await readFile(join(this.path,'pdfs',metadata.filename)); } catch { throw new StorageError('Processing requires retained PDF'); }
  if(hash(raw)!==digest) throw new StorageError('Retained PDF hash does not match processing output');
  return record;
 }
 async get_pdf(item:Attachment):Promise<Buffer|null> { return this.locked(async()=>{
  let record:RecordData|null; try { record=await this.record(item.metadata); } catch { return null; }
  if(!record || !Array.isArray(record.sources) || !record.sources.some(s=>same(s,source(item)))) return null;
  let raw:Buffer; try { raw=await readFile(join(this.path,'pdfs',item.metadata.filename)); } catch(e) { if((e as NodeJS.ErrnoException).code==='ENOENT') return null; throw e; }
  if(hash(raw)!==record.content_hash) return null;
  if(item.inline_data!=null) { if(!/^[A-Za-z0-9_-]*={0,2}$/.test(item.inline_data)||hash(Buffer.from(item.inline_data,'base64url'))!==record.content_hash) return null; }
  return raw;
 }); }
 async put_pdf(item:Attachment,raw:Uint8Array):Promise<string> { return this.locked(async()=>{
  const path=this.document_path(item.metadata),digest=hash(raw), observed=(await this.observed()).get(item.metadata.code);
  if(observed&&observed.document_at>item.metadata.document_at) throw new StorageError('Cannot retain older revision after a newer observed candidate');
  if((await this.saved_metadata()).some(m=>m.code===item.metadata.code&&m.document_at>item.metadata.document_at)) throw new StorageError('Cannot retain older revision while newer canonical PDF exists');
  const dest=join(this.path,'pdfs',item.metadata.filename); let record=await this.record(item.metadata,digest);
  if(await exists(dest)) { if(hash(await readFile(dest))!==digest) { if(!record) throw new StorageError('Same canonical filename has different content; retained PDF preserved'); await this.atomic(dest,raw); } }
  else await this.atomic(dest,raw);
  record ||= {metadata:item.metadata,content_hash:digest,sources:[]};
  if(!Array.isArray(record.sources)) throw new StorageError('Corrupt source provenance');
  if(!record.sources.some(s=>same(s,source(item)))) record.sources.push(source(item));
  await this.write_json(path,record); return digest;
 }); }
 async prune_revisions(metadata:DocumentMetadata,digest:string):Promise<void> { await this.locked(async()=>{
  await this.retained(metadata,digest); const observed=(await this.observed()).get(metadata.code);
  if(observed&&observed.document_at>metadata.document_at) throw new StorageError('Cannot retain older revision after a newer observed candidate');
  const items=(await this.saved_metadata()).filter(m=>m.code===metadata.code);
  if(items.some(m=>m.document_at>metadata.document_at)) throw new StorageError('Cannot retain older revision while newer canonical PDF exists');
  for(const old of items.filter(m=>m.document_at<metadata.document_at)) {
   const record=this.document_path(old); for(const p of [join(this.path,'pdfs',old.filename),record,record.replace(/\.json$/,'.md')]) await rm(p,{force:true});
  }
 }); }
 async list_attachments(codes?:Set<DocumentCode>):Promise<Attachment[]> { return this.locked(async()=>{
  const items=await this.saved_metadata(), filenames=new Set(items.map(m=>m.filename));
  for(const m of (await this.observed()).values()) if((!codes||codes.has(m.code))&&!items.some(i=>i.code===m.code&&i.document_at>=m.document_at)) throw new StorageError(`Missing newest observed source: ${m.filename}`);
  for(const path of (await names(join(this.path,'processed'))).filter(n=>n.endsWith('.json'))) {
   let m:DocumentMetadata; try { m=rules.parse_filename(path.replace(/\.json$/,'.pdf')); } catch { throw new StorageError(`Malformed processed document filename: ${path}`); }
   if((!codes||codes.has(m.code))&&!filenames.has(m.filename)) throw new StorageError(`Missing retained PDF: ${m.filename}`);
  }
  const result:Attachment[]=[];
  for(const m of items) {
   if(codes&&!codes.has(m.code)) continue;
   const record=await this.record(m); if(!record) throw new StorageError(`Missing or corrupt document metadata: ${m.filename}`);
   if(!Array.isArray(record.sources)||!record.sources.length||record.sources.some(s=>!s||typeof s.message_id!=='string'||!s.message_id||(s.part_id!=null&&typeof s.part_id!=='string'))) throw new StorageError(`Missing or corrupt source provenance: ${m.filename}`);
   await this.retained(m,record.content_hash); result.push({metadata:m,message_id:record.sources[0]!.message_id,part_id:record.sources[0]!.part_id});
  }
  return result;
 }); }
 static async discover_local(root:string,identity:BillIdentity,codes:Set<DocumentCode>):Promise<Attachment[]> {
  const result:Attachment[]=[], years=identity.sb_date?[rules.storage_year(identity)]:await names(root);
  const prefix=identity.sb_no+(identity.sb_date?rules.batch_name({...identity,port_code:null}).slice(7,-7):'');
  for(const year of years) for(const batch of (await names(join(root,year))).filter(n=>n.startsWith(prefix))) {
   const dir=join(root,year,batch);
   if(identity.sb_date&&identity.port_code&&batch!==rules.batch_name(identity)) continue;
   let files=(await names(join(dir,'pdfs'))).filter(n=>n.endsWith('.pdf')); if(!files.length) files=(await names(join(dir,'processed'))).filter(n=>n.endsWith('.json')).map(n=>n.replace(/\.json$/,'.pdf'));
   let meta:DocumentMetadata;
   if(files.length) { try { meta=rules.parse_filename(files[0]!); } catch { throw new StorageError(`Malformed local batch artifact: ${files[0]}`); } }
   else { const saved=await json(join(dir,'result.json')); if(!saved) continue; if(!Array.isArray(saved.selected_sources)) throw new StorageError(`Corrupt saved batch source metadata: ${batch}`); if(!saved.selected_sources.length) continue; meta=saved.selected_sources[0]; }
   if(batch!==rules.batch_name(meta.identity)||year!==rules.storage_year(meta.identity)) throw new StorageError(`Local folder does not match retained bill identity: ${batch}`);
   if(!rules.matches_identity({...identity,document_date:null},meta.identity)) continue;
   result.push(...(await new BillStorage(root,meta.identity,true).list_attachments(codes)).filter(a=>rules.matches_identity(identity,a.metadata.identity)));
  }
  return result;
 }
 async get_layout(metadata:DocumentMetadata,digest:string):Promise<DocumentLayout|null> { return this.locked(async()=>{ const r=await this.record(metadata,digest); return r&&same(r.layout_versions,versions())&&r.layout&&Array.isArray(r.layout.pages)?r.layout:null; }); }
 async put_layout(metadata:DocumentMetadata,digest:string,layout:DocumentLayout):Promise<void> { await this.locked(async()=>{ const r=await this.retained(metadata,digest); r.layout=layout;r.layout_versions=versions();delete r.fields;delete r.containers;delete r.parser_version;await this.write_json(this.document_path(metadata),r); }); }
 async put_processed(metadata:DocumentMetadata,digest:string,parsed:ParsedDocument,markdown:string):Promise<void> { await this.locked(async()=>{
  if(parsed.content_hash!==digest||!same(parsed.metadata,metadata)) throw new StorageError('Parsed document hash/metadata does not match storage');
  const r=await this.retained(metadata,digest); r.fields=parsed.fields;r.containers=parsed.containers;r.parser_version=rules.PARSER_VERSION;
  const path=this.document_path(metadata); await this.atomic(path.replace(/\.json$/,'.md'),markdown); await this.write_json(path,r);
 }); }
 async put_result(result:BillResult):Promise<void> { await this.locked(async()=>{
  if(!rules.matches_identity({...result.identity,document_date:null},this.identity)) throw new StorageError('Bill result identity does not match storage');
  const path=join(this.path,'result.json'),previous_payload=await json(path),previous=await this.observed(),current=await this.observed(result),selected=new Map(previous);
  for(const [code,meta] of current) if(!selected.has(code)||meta.document_at>=selected.get(code)!.document_at) selected.set(code,meta);
  const hashes:Partial<Record<DocumentCode,string>>={};
  for(const [code,meta] of selected) {
   const digest=same(current.get(code),meta)&&result.selected_hashes[code]||same(previous.get(code),meta)&&previous_payload?.selected_hashes?.[code];
   if(digest) { try { await this.retained(meta,digest); hashes[code]=digest; } catch {} }
  }
  await this.write_json(path,{...result,selected_sources:[...selected.values()].sort((a,b)=>a.code.localeCompare(b.code)),selected_hashes:hashes});
 }); }
}
