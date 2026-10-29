/** Coordinate Gmail discovery, native parsing and safe workbook updates. */
import { createHash } from 'node:crypto';
import type { Attachment, BillIdentity, BillResult, DocumentCode, FieldName, FieldResult, RowUpdate, WorkbookRow } from './models';
import { attachment_key } from './models';
import { extract_layout, parse_document, render_markdown } from './documents';
import { BillStorage } from './storage';
import * as rules from './rules';

export interface MailService {
  authenticate(): Promise<void>;
  discover(identity: BillIdentity, codes: Set<DocumentCode>): Promise<Attachment[]>;
  download(attachment: Attachment): Promise<Uint8Array>;
}
export interface WorkbookService {
  apply(sheet: string, updates: RowUpdate[], force?: boolean, fields?: Set<FieldName>): void;
  save(): Promise<void>;
}
export interface ExecuteOptions { selected: WorkbookRow[]; force?: boolean; leo_only?: boolean; workers?: number; refresh?: boolean; verify?: boolean; on_progress?: (record: WorkflowRecord) => void }
export type WorkflowRecord = [WorkbookRow, BillResult | null, RowUpdate];
const hash = (value: Uint8Array): string => createHash('sha256').update(value).digest('hex');
const error_text = (error: unknown): string => error instanceof Error ? error.message : String(error);
const missing = (fields: Iterable<FieldName>, reason: string, state: FieldResult['state'] = 'missing'): Partial<Record<FieldName, FieldResult>> => Object.fromEntries([...fields].map(name => [name, {state, reason}]));
const owns = (fields: Set<FieldName>, code: DocumentCode): FieldName[] => [...fields].filter(name => rules.FIELD_DOCUMENTS[name] === code);

export async function process_bill(gmail: MailService, data_root: string, identity: BillIdentity, fields: Set<FieldName> = new Set(rules.DATA_FIELDS), options: {refresh?: boolean; verify?: boolean} = {}): Promise<BillResult> {
  const result: BillResult = {identity, fields:missing(fields,'Matching document has not arrived'), containers:[], selected_sources:[], selected_hashes:{}, issues:[], errors:[]};
  const codes = rules.discovery_codes(fields);
  let attachments: Attachment[] = [];
  if (options.verify) {
    for (const code of codes) {
      try { attachments.push(...await BillStorage.discover_local(data_root, identity, new Set([code]))); }
      catch (error) { const reason = `${code}: ${error_text(error)}`; Object.assign(result.fields, missing(owns(fields,code),reason,'error')); result.errors.push(reason); }
    }
  } else {
    attachments = await gmail.discover(identity, codes);
  }
  attachments = attachments.filter(item => codes.has(item.metadata.code) && rules.matches_identity(identity,item.metadata.identity));
  const identities = new Set(attachments.map(item => JSON.stringify([item.metadata.identity.sb_date,item.metadata.identity.port_code])));
  if (identities.size > 1) {
    result.fields = missing(fields,'Multiple bill dates/ports match incomplete workbook identity','rejected');
    result.issues.push('Conflicting bill identities; fill the SB date and port in Excel');
    if (!options.verify) await new BillStorage(data_root,identity).put_result(result);
    return result;
  }
  const latest = new Map<DocumentCode, Attachment[]>();
  for (const item of attachments) {
    const code = item.metadata.code, existing = latest.get(code);
    if (!existing || item.metadata.document_at > existing[0]!.metadata.document_at) latest.set(code,[item]);
    else if (item.metadata.document_at === existing[0]!.metadata.document_at) existing.push(item);
  }
  let result_store: BillStorage | undefined;
  for (const code of rules.document_codes(fields)) {
    const candidates = latest.get(code);
    if (!candidates?.length) continue;
    candidates.sort((a,b) => attachment_key(a).localeCompare(attachment_key(b)));
    result.selected_sources.push(candidates[0]!.metadata);
    const successes: Array<{item:Attachment; store:BillStorage; raw:Uint8Array; digest:string}> = [];
    let failed: string | null = null;
    for (const item of candidates) {
      const store = new BillStorage(data_root,item.metadata.identity,!!options.verify);
      if (!result_store) result_store = store;
      try {
        let raw = await store.get_pdf(item);
        if (raw === null) {
          if (options.verify) throw new Error('Stored PDF missing or corrupt');
          raw = Buffer.from(await gmail.download(item));
          await store.put_pdf(item,raw);
        }
        const digest = hash(raw);
        if (!options.verify) await store.prune_revisions(item.metadata,digest);
        successes.push({item,store,raw,digest});
      } catch (error) { failed = `${item.metadata.filename}: ${error_text(error)}`; result.errors.push(failed); }
    }
    if (failed) { Object.assign(result.fields,missing(owns(fields,code),failed,'error')); continue; }
    if (!successes.length) continue;
    if (new Set(successes.map(entry => entry.digest)).size !== 1) {
      const reason = 'Equal newest filename timestamps have different content';
      result.issues.push(reason); Object.assign(result.fields,missing(owns(fields,code),reason,'rejected')); continue;
    }
    const {item,store,raw,digest} = successes[0]!;
    result.selected_hashes[code] = digest;
    try {
      let layout = options.refresh ? null : await store.get_layout(item.metadata,digest);
      if (!layout || (layout.document_code != null && layout.document_code !== code)) {
        layout = extract_layout(raw,item.metadata);
        if (!options.verify) await store.put_layout(item.metadata,digest,layout);
      }
      const parsed = parse_document(layout,item.metadata,digest);
      if (parsed.content_hash !== digest || JSON.stringify(parsed.metadata) !== JSON.stringify(item.metadata)) throw new Error('Parser returned mismatching source identity');
      if (!options.verify) await store.put_processed(item.metadata,digest,parsed,render_markdown(layout,item.metadata));
      for (const name of owns(fields,code)) result.fields[name] = parsed.fields[name] ?? {state:'rejected',reason:'Parser omitted owned field'};
      if (owns(fields,code).some(name => rules.CONTAINER_FIELDS.includes(name))) result.containers = parsed.containers;
    } catch (error) { const reason = `${item.metadata.filename}: ${error_text(error)}`; Object.assign(result.fields,missing(owns(fields,code),reason,'error')); result.errors.push(reason); }
  }
  if (!options.verify && (result.selected_sources.length || result.errors.length)) await (result_store ?? new BillStorage(data_root,identity)).put_result(result);
  return result;
}

export function make_update(row: WorkbookRow, result: BillResult, selected: Set<FieldName> = new Set(rules.DATA_FIELDS), force = false): RowUpdate {
  const writable = new Set([...selected].filter(name => force || !row.values[name]));
  const accepted = new Set([...writable].filter(name => result.fields[name]?.state === 'accepted'));
  const values: RowUpdate['values'] = {};
  let container_fields = new Set([...accepted].filter(name => rules.CONTAINER_FIELDS.includes(name)));
  const failures: string[] = [];
  for (const name of accepted) {
    if (rules.CONTAINER_FIELDS.includes(name)) continue;
    if (row.values[name]?.startsWith('=')) failures.push(`${name}: formula is protected`);
    else values[name] = result.fields[name]!.value ?? null;
  }
  if (container_fields.size) {
    for (const name of rules.CONTAINER_FIELDS) {
      const existing = row.values[name];
      if (!existing || (container_fields.has(name) && !existing.startsWith('='))) continue;
      const attr = rules.CONTAINER_ATTRS[name as keyof typeof rules.CONTAINER_ATTRS];
      const source = result.containers.map(record => record[attr] ?? '').join('\n');
      if (result.fields[name]?.state !== 'accepted' || existing !== source) {
        failures.push(`Container records unchanged: retained ${name} does not match selected source`); container_fields = new Set(); break;
      }
    }
  }
  for (const name of selected) {
    const field = result.fields[name];
    if (!field || field.state !== 'accepted') { failures.push(`${name}: ${field?.reason ?? field?.state ?? 'missing'}`); continue; }
    if (!writable.has(name)) {
      const attr = rules.CONTAINER_ATTRS[name as keyof typeof rules.CONTAINER_ATTRS];
      const source = attr ? result.containers.map(record => record[attr] ?? '').join('\n') : field.value;
      if (row.values[name] !== source) failures.push(`${name}: existing Excel value differs from validated source; use --force to repair`);
    }
  }
  const complete = rules.DATA_FIELDS.every(name => !!row.values[name] || values[name] != null || container_fields.has(name));
  const states = [...selected].map(name => result.fields[name]?.state ?? 'missing');
  const status = states.includes('error') || result.errors.length ? 'Error' : !result.selected_sources.length && states.every(state => state === 'missing') ? 'Not found' : failures.length || result.issues.length || !complete || selected.size < rules.DATA_FIELDS.length || !rules.result_is_success(result) ? 'Partial' : 'Success';
  return {row_idx:row.row_idx,values,containers:result.containers.length ? result.containers : null,container_fields,status,detail:[...failures,...result.issues,...result.errors].join('; ') || `${Object.keys(values).length + container_fields.size} validated field(s) selected`};
}

export async function execute(workbook: WorkbookService, sheet: string, gmail: MailService, data_root: string, options: ExecuteOptions): Promise<WorkflowRecord[]> {
  const {selected,force=false,leo_only=false,workers=3,refresh=false,verify=false,on_progress} = options;
  if (!Number.isInteger(workers) || workers < 1 || workers > 8) throw new Error('Workers must be between 1 and 8');
  const fields = leo_only ? new Set<FieldName>(rules.LEO_FIELDS) : new Set(rules.DATA_FIELDS);
  const records: WorkflowRecord[] = [], pending: WorkbookRow[] = [];
  const status = (row:WorkbookRow, value:RowUpdate['status'], detail:string): WorkflowRecord => [row,null,{row_idx:row.row_idx,values:{},container_fields:new Set(),status:value,detail}];
  for (const row of selected) {
    if (row.cancelled) records.push(status(row,'Cancelled','Cancelled bill skipped'));
    else if (!force && !verify && row.status === 'Success') records.push(status(row,'Success','Already successful; skipped'));
    else if (row.error) records.push(status(row,'Error',row.error));
    else pending.push(row);
  }
  if (pending.length && !verify) await gmail.authenticate();
  const groups = new Map<string,WorkbookRow[]>();
  for (const row of pending) { const key = JSON.stringify([row.identity.sb_no,row.identity.sb_date,row.identity.port_code,row.identity.document_date]); groups.set(key,[...(groups.get(key) ?? []),row]); }
  // Separate workers never mutate the same SB folder concurrently.
  const buckets = new Map<string,WorkbookRow[][]>();
  for (const group of groups.values()) { const sb = group[0]!.identity.sb_no; buckets.set(sb,[...(buckets.get(sb) ?? []),group]); }
  const tasks = [...buckets.values()]; let next = 0;
  await Promise.all(Array.from({length:Math.min(workers,tasks.length)},async()=>{
    while (next < tasks.length) {
      const task = tasks[next++]!;
      for (const group of task) {
        let made: WorkflowRecord[];
        try {
          const result = await process_bill(gmail,data_root,group[0]!.identity,fields,{refresh,verify});
          made = group.map(row => [row,result,make_update(row,result,fields,force || verify)] as WorkflowRecord);
        } catch (error) { made = group.map(row => status(row,'Error',error_text(error))); }
        records.push(...made);
        // Provisional: workbook.apply below can still downgrade a status (blocked cell, formula).
        for (const record of made) on_progress?.(record);
      }
    }
  }));
  records.sort((a,b) => a[0].row_idx-b[0].row_idx);
  const updates = records.filter(([row]) => verify ? false : row.cancelled || (force || row.status !== 'Success')).map(([, ,update]) => update);
  if (updates.length) { workbook.apply(sheet,updates,force,fields); await workbook.save(); }
  return records;
}
