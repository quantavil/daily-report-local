/** Native PDF text geometry and rule-driven source association. */
import * as mupdf from 'mupdf';
import * as rules from './rules';
import type { ContainerRecord, DocumentCode, DocumentLayout, DocumentMetadata, Evidence, FieldName, FieldResult, LayoutPage, LayoutSpan, ParsedDocument } from './models';

type ContainerField = keyof typeof rules.CONTAINER_ATTRS;
const containerFields = rules.CONTAINER_FIELDS as ContainerField[];
const eligible = (number: number, code: DocumentCode) => rules.PAGES[code] === null || rules.PAGES[code]!.includes(number);
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

export function extract_layout(pdf: Uint8Array, metadata: DocumentMetadata): DocumentLayout {
 let doc: mupdf.Document;
 try { doc = mupdf.Document.openDocument(pdf, 'application/pdf'); }
 catch { throw new Error('Unreadable PDF'); }
 try {
  if (!doc.isPDF()) throw new Error('Unreadable PDF');
  if (doc.needsPassword() && !doc.authenticatePassword('')) throw new Error('Encrypted PDF');
  const pages: LayoutPage[] = [];
  for (let i=0;i<doc.countPages();i++) {
   const page=doc.loadPage(i);
   try {
    const bounds=page.getBounds(), spans: LayoutSpan[]=[];
    const st=page.toStructuredText('preserve-whitespace');
    try {
     let text='', box: [number,number,number,number] | null=null;
     const flush=()=>{ if(text.trim() && box) spans.push({text:text.trim(),bbox:box}); text=''; box=null; };
     st.walk({
      beginLine:()=>flush(),
      onChar:(c,_origin,_font,_size,q)=>{
       const xs=[q[0],q[2],q[4],q[6]], ys=[q[1],q[3],q[5],q[7]];
       const b: [number,number,number,number]=[Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
       if(box && b[0]-box[2]>rules.MAX_INLINE_GAP) flush();
       text+=c;
       box=box ? [Math.min(box[0],b[0]),Math.min(box[1],b[1]),Math.max(box[2],b[2]),Math.max(box[3],b[3])] : b;
      }, endLine:flush,
     });
     flush();
    } finally { st.destroy(); }
    if (!spans.length && eligible(i+1,metadata.code)) throw new Error(`Page ${i+1} has no native text; OCR is disabled`);
    if(spans.length) pages.push({number:i+1,width:bounds[2]-bounds[0],height:bounds[3]-bounds[1],spans});
   } finally { page.destroy(); }
  }
  if(!pages.length) throw new Error('PDF contains no eligible pages');
  return {document_code:metadata.code,pages};
 } finally { doc.destroy(); }
}

function box(spans: LayoutSpan[]): [number,number,number,number] {
 return [Math.min(...spans.map(s=>s.bbox[0])),Math.min(...spans.map(s=>s.bbox[1])),Math.max(...spans.map(s=>s.bbox[2])),Math.max(...spans.map(s=>s.bbox[3]))];
}
function lines(page: LayoutPage): LayoutSpan[] {
 const rows: LayoutSpan[][]=[];
 for(const span of [...page.spans].sort((a,b)=>a.bbox[1]-b.bbox[1]||a.bbox[0]-b.bbox[0])) {
  const row=rows.find(r=>Math.abs(r[0]!.bbox[1]-span.bbox[1])<=rules.ROW_TOLERANCE);
  if(row) row.push(span); else rows.push([span]);
 }
 const cells: LayoutSpan[]=[];
 for(const row of rows) {
  let current: LayoutSpan[]=[];
  const flush=()=>{if(current.length) cells.push({text:current.map(s=>s.text).join(' '),bbox:box(current)}); current=[];};
  for(const s of row.sort((a,b)=>a.bbox[0]-b.bbox[0])) { if(current.length&&s.bbox[0]-current[current.length-1]!.bbox[2]>rules.MAX_INLINE_GAP) flush(); current.push(s); }
  flush();
 }
 return cells;
}
function labels(page: LayoutPage,field: string): LayoutSpan[] {
 const found=page.spans.filter(s=>rules.label_matches(field,s.text));
 for(const s of lines(page)) if(rules.label_matches(field,s.text)&&!found.some(c=>c.bbox.every((v,i)=>v===s.bbox[i]))) found.push(s);
 return found;
}
function boundaries(page: LayoutPage): LayoutSpan[] {
 const found=page.spans.filter(s=>rules.NUMBERED_LABEL.test(s.text)||Object.keys(rules.LABELS).some(f=>rules.label_matches(f,s.text)));
 for(const f of Object.keys(rules.LABELS)) for(const s of labels(page,f)) if(!found.some(x=>x.text===s.text&&x.bbox.every((v,i)=>v===s.bbox[i]))) found.push(s);
 for(const prefix of page.spans) {
  if(!rules.NUMBERED_PREFIX.test(prefix.text.trim())) continue;
  const neighbor=page.spans.filter(s=>s.bbox[0]>=prefix.bbox[2]&&Math.abs(s.bbox[1]-prefix.bbox[1])<=rules.ROW_TOLERANCE).sort((a,b)=>a.bbox[0]-b.bbox[0])[0];
  if(neighbor&&neighbor.bbox[0]-prefix.bbox[2]<=rules.MAX_INLINE_GAP) {
   const merged={text:prefix.text+' '+neighbor.text,bbox:box([prefix,neighbor])}; if(rules.NUMBERED_LABEL.test(merged.text)) found.push(merged);
  }
 }
 return found;
}
function cell(page: LayoutPage,label: LayoutSpan,field: string): LayoutSpan[] {
 const headings=boundaries(page);
 const isLabel=(s: LayoutSpan)=>headings.some(h=>h.bbox[0]<=s.bbox[0]&&h.bbox[2]>=s.bbox[2]&&Math.abs(h.bbox[1]-s.bbox[1])<=rules.ROW_TOLERANCE);
 const right=Math.min(page.width,...headings.filter(s=>Math.abs(s.bbox[1]-label.bbox[1])<=rules.ROW_TOLERANCE&&s.bbox[0]>=label.bbox[2]).map(s=>s.bbox[0]-Math.max(0,...Object.keys(rules.LABELS).filter(f=>rules.label_matches(f,s.text)).map(f=>rules.CELL_LEFT_MARGIN[f]??0))));
 const inline=page.spans.filter(s=>Math.abs(s.bbox[1]-label.bbox[1])<=rules.ROW_TOLERANCE&&label.bbox[2]<=s.bbox[0]&&s.bbox[0]<right&&!isLabel(s));
 if(inline.length&&rules.INLINE_VALUE_FIELDS.has(field)) return inline.sort((a,b)=>a.bbox[0]-b.bbox[0]);
 const left=label.bbox[0]-(rules.CELL_LEFT_MARGIN[field]??rules.COLUMN_TOLERANCE);
 const bottom=Math.min(label.bbox[3]+rules.MAX_CELL_DEPTH,...headings.filter(s=>s.bbox[1]>label.bbox[1]+rules.ROW_TOLERANCE&&s.bbox[0]<right&&s.bbox[2]>left).map(s=>s.bbox[1]));
 const candidates=page.spans.filter(s=>s.bbox[1]>label.bbox[1]+rules.ROW_TOLERANCE&&s.bbox[1]<bottom&&s.bbox[0]>=left&&s.bbox[0]<right&&!isLabel(s));
 if(!candidates.length) return [];
 const top=Math.min(...candidates.map(s=>s.bbox[1]));
 return candidates.filter(s=>Math.abs(s.bbox[1]-top)<=rules.ROW_TOLERANCE).sort((a,b)=>a.bbox[0]-b.bbox[0]);
}
function evidence(page: LayoutPage,label: LayoutSpan,values: LayoutSpan[],metadata: DocumentMetadata,hash: string): Evidence {
 return {filename:metadata.filename,content_hash:hash,page_number:page.number,label:label.text,quote:values.map(s=>s.text).join(' '),bbox:box([label,...values])};
}
function field(pages: LayoutPage[],name: FieldName,metadata: DocumentMetadata,hash: string): FieldResult {
 let hits=pages.flatMap(page=>labels(page,name).map(label=>({page,label})));
 const priority=rules.SOURCE_PAGE_PRIORITY[name];
 if(priority&&hits.length) {const number=hits.map(h=>h.page.number).sort((a,b)=>priority.indexOf(a)-priority.indexOf(b))[0]; hits=hits.filter(h=>h.page.number===number);}
 if(hits.length!==1) return {state:'rejected',reason:'Eligible source label missing or ambiguous'};
 const {page,label}=hits[0]!, values=cell(page,label,name);
 if(!values.length) return {state:'blank',reason:'Eligible source cell is empty'};
 try {
  const value=rules.value_token(name,values.map(s=>s.text).join(' '));
  const source=[evidence(page,label,values,metadata,hash)];
  return value===null?{state:'blank',evidence:source}:{state:'accepted',value,value_type:name==='leo_date'?'date':'text',evidence:source};
 } catch(error) { return {state:'rejected',reason:message(error)}; }
}
function identity(pages: LayoutPage[],metadata: DocumentMetadata): string | null {
 let primary=0;
 for(const page of pages) for(const anchor of labels(page,'sb_no')) {
  let sources: Partial<Record<(typeof rules.IDENTITY_FIELDS)[number],LayoutSpan>>={};
  for(const f of rules.IDENTITY_FIELDS) {const found=labels(page,f).filter(s=>Math.abs(s.bbox[1]-anchor.bbox[1])<=rules.ROW_TOLERANCE); if(found.length===1) sources[f]=found[0];}
  if(rules.IDENTITY_FIELDS.every(f=>sources[f])) primary++;
  else if(rules.secondary_identity_label(anchor.text,'anchor')) {
   const p=rules.SECONDARY_IDENTITY;
   const companions=labels(page,p.field).filter(s=>rules.secondary_identity_label(s.text,'companion')&&Math.abs(s.bbox[0]-anchor.bbox[0])<=rules.COLUMN_TOLERANCE&&Math.abs(s.bbox[1]-anchor.bbox[1]-p.row_offset)<=rules.ROW_TOLERANCE);
   if(companions.length!==1) return 'PDF secondary identity header missing or ambiguous';
   sources={sb_no:anchor,sb_date:companions[0]};
  } else return 'PDF identity header missing or ambiguous';
  for(const f of rules.IDENTITY_FIELDS) {
   const source=sources[f],expected=metadata.identity[f]; if(!source||expected==null) continue;
   try { if(rules.value_token(f,cell(page,source,f).map(s=>s.text).join(' '))!==expected) return 'PDF identity conflicts with filename'; }
   catch { return 'PDF identity source is invalid'; }
  }
 }
 return primary ? null : 'PDF primary identity header missing';
}
function stacked(page: LayoutPage,headers: Record<ContainerField,LayoutSpan[]>,metadata: DocumentMetadata,hash: string,rejected: Partial<Record<ContainerField,string>>): ContainerRecord[] {
 const records: ContainerRecord[]=[], starts=[...headers.container_no].sort((a,b)=>a.bbox[1]-b.bbox[1]);
 for(let i=0;i<starts.length;i++) {
  const start=starts[i]!, bottom=starts[i+1]?.bbox[1]??start.bbox[1]+rules.MAX_CELL_DEPTH;
  const record: ContainerRecord={number:null,size:null,seal:null,evidence:[]};
  for(const f of containerFields) {
   const found=headers[f].filter(s=>start.bbox[1]<=s.bbox[1]&&s.bbox[1]<bottom);
   if(found.length!==1) throw new Error('Stacked movement record has missing or ambiguous source header');
   const source=found[0]!, values=cell(page,source,f);
   try {record[rules.CONTAINER_ATTRS[f]]=values.length ? rules.value_token(f,values.map(s=>s.text).join(' ')) : null;}
   catch(error) {rejected[f]=message(error);}
   if(values.length) record.evidence.push(evidence(page,source,values,metadata,hash));
  }
  records.push(record);
 }
 return records;
}
function movement(pages: LayoutPage[],metadata: DocumentMetadata,hash: string): {records: ContainerRecord[], rejected: Partial<Record<ContainerField,string>>} {
 const records: ContainerRecord[]=[], rejected: Partial<Record<ContainerField,string>>={};
 let columns: Record<ContainerField,LayoutSpan> | null=null, stackedSeen=false;
 for(const page of pages) {
  const headers=Object.fromEntries(containerFields.map(f=>[f,labels(page,f)])) as Record<ContainerField,LayoutSpan[]>;
  if(containerFields.every(f=>headers[f].length)) {
   const ys=containerFields.flatMap(f=>headers[f].map(s=>s.bbox[1]));
   if(Math.max(...ys)-Math.min(...ys)>rules.ROW_TOLERANCE) {records.push(...stacked(page,headers,metadata,hash,rejected)); stackedSeen=true; continue;}
  }
  let top: number;
  if(containerFields.some(f=>headers[f].length)) {
   if(!containerFields.every(f=>headers[f].length===1)) throw new Error('Incomplete or ambiguous movement headers');
   columns=Object.fromEntries(containerFields.map(f=>[f,headers[f][0]!])) as Record<ContainerField,LayoutSpan>;
   const ys=containerFields.map(f=>columns![f].bbox[1]);
   if(Math.max(...ys)-Math.min(...ys)>rules.ROW_TOLERANCE) throw new Error('Movement headers are not aligned');
   top=Math.max(...containerFields.map(f=>columns![f].bbox[3]));
  } else if(columns) {
   const cols=columns;
   const identityHeaders=rules.IDENTITY_FIELDS.flatMap(f=>labels(page,f).map(label=>({label,f})));
   top=Math.max(0,...identityHeaders.flatMap(({label,f})=>[label,...cell(page,label,f)].map(s=>s.bbox[3])));
   const anchors=page.spans.filter(s=>s.bbox[1]>=top&&Math.abs(s.bbox[0]-cols.container_no.bbox[0])<=rules.COLUMN_TOLERANCE);
   const valid=anchors.filter(s=>{try{return rules.value_token('container_no',s.text)!==null;}catch{return false;}});
   if(!valid.length) {
    if(page.spans.some(s=>s.bbox[1]>=top&&!rules.MOVEMENT_STOP.test(s.text))) throw new Error('Unsupported movement continuation geometry');
    continue;
   }
   if(page.spans.some(s=>rules.MOVEMENT_STOP.test(s.text)&&s.bbox[1]>=top&&s.bbox[1]<Math.max(...valid.map(a=>a.bbox[1])))) throw new Error('Movement continuation has intervening unsupported section');
  } else {
   if(stackedSeen&&page.spans.some(s=>{try{return rules.value_token('container_no',s.text)!==null;}catch{return false;}})) throw new Error('Headerless stacked movement continuation requires supported source geometry');
   continue;
  }
  const cols=columns!,order=[...containerFields].sort((a,b)=>cols[a].bbox[0]-cols[b].bbox[0]);
  const rows: LayoutSpan[][]=[];
  for(const s of [...page.spans].sort((a,b)=>a.bbox[1]-b.bbox[1]||a.bbox[0]-b.bbox[0])) {
   if(s.bbox[1]<top) continue;
   if(rules.MOVEMENT_STOP.test(s.text)) {
    const later=page.spans.filter(x=>x.bbox[1]>s.bbox[1]);
    if(later.some(x=>{try{return rules.value_token('container_no',x.text)!==null;}catch{return false;}})) throw new Error('Movement records occur after unsupported section boundary');
    break;
   }
   const row=rows.find(r=>Math.abs(r[0]!.bbox[1]-s.bbox[1])<=rules.ROW_TOLERANCE); if(row) row.push(s); else rows.push([s]);
  }
  for(const row of rows) {
   const record: ContainerRecord={number:null,size:null,seal:null,evidence:[]}; let any=false;
   for(let i=0;i<order.length;i++) {
    const f=order[i]!,left=cols[f].bbox[0]-rules.COLUMN_TOLERANCE,right=i+1<order.length?cols[order[i+1]!].bbox[0]-rules.COLUMN_TOLERANCE:page.width;
    const values=row.filter(s=>s.bbox[0]>=left&&s.bbox[0]<right);
    if(!values.length) continue; any=true;
    try {record[rules.CONTAINER_ATTRS[f]]=rules.value_token(f,values.map(s=>s.text).join(' '));}
    catch(error) {rejected[f]=message(error);}
    record.evidence.push(evidence(page,cols[f],values,metadata,hash));
   }
   if(any) records.push(record);
  }
 }
 if(!columns&&!records.length) throw new Error('Movement table headers missing');
 return {records,rejected};
}
export function parse_document(layout: DocumentLayout,metadata: DocumentMetadata,content_hash: string): ParsedDocument {
 const pages=layout.pages.filter(p=>eligible(p.number,metadata.code));
 const owned=rules.DATA_FIELDS.filter(f=>rules.FIELD_DOCUMENTS[f]===metadata.code);
 const fields: Partial<Record<FieldName,FieldResult>>=Object.fromEntries(owned.map(f=>[f,{state:'rejected',reason:'No eligible native layout'}]));
 const result: ParsedDocument={metadata,content_hash,fields,containers:[]};
 const reason=identity(layout.pages,metadata);
 if(reason) {for(const f of owned) fields[f]={state:'rejected',reason}; return result;}
 if(metadata.code==='SB35') {
  let extracted: ReturnType<typeof movement>;
  try {extracted=movement(pages,metadata,content_hash);} catch(error) {for(const f of owned) fields[f]={state:'rejected',reason:message(error)};return result;}
  result.containers=extracted.records;
  for(const f of containerFields) {
   if(extracted.rejected[f]) {fields[f]={state:'rejected',reason:extracted.rejected[f]};continue;}
   const values=result.containers.map(r=>r[rules.CONTAINER_ATTRS[f]]), sources=result.containers.flatMap(r=>r.evidence.filter(e=>rules.label_matches(f,e.label)));
   fields[f]=values.some(Boolean) ? {state:'accepted',value_type:'records',evidence:sources,reason:values.every(Boolean)?null:'Movement records contain verified empty positions'} : {state:'blank',value_type:'records',evidence:sources,reason:'Movement records contain empty positions'};
  }
 } else for(const f of owned) fields[f]=field(pages,f,metadata,content_hash);
 return result;
}
export function render_markdown(layout: DocumentLayout,metadata: DocumentMetadata): string {
 return layout.pages.filter(p=>eligible(p.number,metadata.code)).map(p=>`--- SOURCE PAGE ${p.number} ---\n${lines(p).map(s=>s.text).join('\n\n')}`).join('\n\n');
}
