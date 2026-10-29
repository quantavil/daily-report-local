/** ICEGATE rules and data validation; no filesystem, Gmail or workbook side effects. */
import type { BillIdentity, BillResult, DocumentCode, DocumentMetadata, FieldName } from './models';
export const SENDER = 'noreply@icegate.gov.in';
export const RULES_VERSION = '6', PARSER_VERSION = '6', LAYOUT_VERSION = '3';
export const FIELD_DOCUMENTS: Record<FieldName,DocumentCode> = {shipper_name:'SB21',inv_no:'SB21',leo_date:'SB22',container_no:'SB35',container_size:'SB35',customs_seal:'SB35'};
export const DATA_FIELDS = Object.keys(FIELD_DOCUMENTS) as FieldName[];
export const PAGES: Record<DocumentCode,number[] | null> = {SB21:[1,2],SB22:[1],SB35:null};
export const EXCEL_COLUMNS: Record<string,string[]> = {
 port_code:['Port Code / ICD Code','Port Code','ICD Code'], inv_no:['Inv No','Invoice No','Invoice Number','Inv No.','Invoice No.'],
 sb_no:['Shipping Bill No','Shipping Bill Number','SB No','SB Number'], sb_date:['Shipping Bill Date','SB Date','Shipping Bill Dt','SB Dt'], document_date:['Document Date','Document Dt'],
 leo_date:['LEO Date','LEO Dt','LEO Date.','Let Export Date'], shipper_name:['Shipper Name','Exporter Name','Shipper','Exporter','Party Name'],
 container_no:['Container No.','Container No','Container Number','Container','Containers'], container_size:['Container Size','Size','Cont Size','Cntr Size'],
 customs_seal:['Customs Seal Number','Customs Seal No','Customs Seal No.','Customs Seal','Seal Number','Seal No'], billing_status:['Billing Status','Billing status','Billing'],status:['Status']
};
export const EXCEL_REQUIRED_COLUMNS = ['sb_no',...DATA_FIELDS], EXCEL_DATE_NUMBER_FORMAT = 'dd\\-mmm\\-yyyy';
export const EXCEL_BLANK_VALUES = ['', '-', 'N/A', 'NA', 'NONE', 'NULL'], EXCEL_CANCELLED_VALUE = 'CANCELLED';
export const LABELS: Record<string,RegExp> = {
 shipper_name:/^1\s*\.\s*EXPORTER['’]?S\s+NAME\s*&\s*ADDRESS$/i,
 inv_no:/^2\s*\.\s*INVOICE\s+No\.?\s*&\s*Dt\.?$/i, leo_date:/^6\s*\.\s*LEO\s+Date\.?$/i,
 container_no:/^11\s*[.,]\s*Container(?:\s+No\.?)?$/i, customs_seal:/^12\s*\.\s*Seal(?:\s+No\.?)?$/i,
 container_size:/^14\s*\.\s*Size$/i, sb_no:/^(?:\d+\s*\.\s*)?(?:SB\s*No\.?|Shipping\s+Bill\s+No\.?)$/i,
 sb_date:/^(?:\d+\s*\.\s*)?(?:SB\s*(?:Date|DT)|Shipping\s+Bill\s+Date)$/i, port_code:/^(?:\d+\s*\.\s*)?Port\s+Code$/i
};
export const NUMBERED_LABEL = /^\d+\s*\.\s*[A-Za-z]/, NUMBERED_PREFIX = /^\d+\s*[.,]$/;
export const ROW_TOLERANCE=3, COLUMN_TOLERANCE=3, MAX_CELL_DEPTH=65, MAX_INLINE_GAP=25;
export const CONTAINER_FIELDS: FieldName[] = ['container_no','customs_seal','container_size'];
export const CONTAINER_ATTRS = {container_no:'number',container_size:'size',customs_seal:'seal'} as const;
export const LEO_FIELDS = new Set<FieldName>(['leo_date']);
export const MOVEMENT_STOP = /^(?:\d+\s*\.\s*[A-Za-z]|DECLARATION|NOTE\b|SIGNATURE|PAGE\s+\d)/i;
export const CELL_LEFT_MARGIN: Record<string,number> = {sb_no:12,sb_date:12,port_code:12,inv_no:40};
export const INLINE_VALUE_FIELDS = new Set(['leo_date','sb_no','sb_date','port_code',...CONTAINER_FIELDS]);
export const SOURCE_PAGE_PRIORITY: Record<string,number[]> = {shipper_name:[1,2]};
export const IDENTITY_FIELDS = ['sb_no','sb_date','port_code'] as const;
export const SECONDARY_IDENTITY = {anchor:/^1\s*\.\s*SB\s*NO$/i,companion:/^4\s*\.\s*SB\s*DT$/i,field:'sb_date',row_offset:13.5} as const;
export const ADDRESS_ONLY = /^(?:\d+\b|(?:P\.?\s*O\.?\s*BOX|ADDRESS|ROAD|STREET|PLOT|HOUSE|FLOOR|SECTOR|VILLAGE|DISTRICT|PIN\s*CODE)\b)|\b(?:ADDRESS\s+ROAD|ROAD|STREET|LANE|POST\s+OFFICE|PIN\s*CODE)\s*$/i;
export const EXPORTER_NAME = /^[A-Za-z][A-Za-z0-9 &'().,/-]*$/;
const MONTHS=['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
function iso_date(y: number, m: number, d: number): string {
 if (y < 1 || y > 9999 || m < 1 || m > 12 || d < 1 || d > 31) throw new Error('Invalid date');
 const dt = new Date(0); dt.setUTCFullYear(y,m-1,d); dt.setUTCHours(0,0,0,0);
 if (dt.getUTCFullYear()!==y || dt.getUTCMonth()!==m-1 || dt.getUTCDate()!==d) throw new Error('Invalid date');
 return `${String(y).padStart(4,'0')}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
export function parse_date(text: string): string {
 let m: RegExpMatchArray | null;
 if ((m=text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) return iso_date(+m[1]!,+m[2]!,+m[3]!);
 if ((m=text.match(/^(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})$/))) return iso_date(+m[4]!,+m[3]!,+m[1]!);
 if ((m=text.match(/^(\d{2})(\d{2})(\d{4})$/))) return iso_date(+m[3]!,+m[2]!,+m[1]!);
 if ((m=text.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{2}|\d{4})$/))) {
  const month=MONTHS.indexOf(m[2]!.toLowerCase())+1; const yr=+m[3]!; return iso_date(m[3]!.length===2 ? (yr<=68?2000+yr:1900+yr) : yr,month,+m[1]!);
 }
 throw new Error('Invalid associated date');
}
function compact(date: string): string { const iso=parse_date(date); return iso.slice(8,10)+iso.slice(5,7)+iso.slice(0,4); }
export function create_identity(input: BillIdentity): BillIdentity {
 if (!/^[0-9]{7}$/.test(input.sb_no)) throw new Error('SB number must contain seven ASCII digits');
 const port=input.port_code?.trim().toUpperCase() || null;
 if (port && !/^[A-Z0-9]{6}$/.test(port)) throw new Error('Port code must contain six ASCII letters/digits');
 return {sb_no:input.sb_no,sb_date:input.sb_date ? parse_date(input.sb_date) : null,port_code:port,document_date:input.document_date ? parse_date(input.document_date) : null};
}
export function parse_filename(filename: string): DocumentMetadata {
 if (!/^[0-9]{15}[A-Z0-9]{6}(?:SB21|SB22|SB35)[0-9]{12}\.pdf$/.test(filename)) throw new Error('Filename is not canonical ICEGATE metadata');
 const sb_date=parse_date(filename.slice(7,15)), document_date=parse_date(filename.slice(25,33));
 const hour=+filename.slice(33,35),minute=+filename.slice(35,37);
 if (hour>23 || minute>59) throw new Error('Invalid filename date or time');
 return {identity:create_identity({sb_no:filename.slice(0,7),sb_date,port_code:filename.slice(15,21),document_date}),code:filename.slice(21,25) as DocumentCode,document_at:`${document_date}T${filename.slice(33,35)}:${filename.slice(35,37)}:00`,filename};
}
export function filename_prefix(identity: BillIdentity, code: DocumentCode): string {
 if (!(code in PAGES)) throw new Error('Unsupported document code');
 if (!identity.sb_date) return identity.sb_no;
 let prefix=identity.sb_no+compact(identity.sb_date);
 if (!identity.port_code) return prefix;
 prefix+=identity.port_code+code;
 return prefix+(identity.document_date ? compact(identity.document_date) : '');
}
export function batch_name(identity: BillIdentity): string { return identity.sb_no+(identity.sb_date?compact(identity.sb_date):'unknown')+(identity.port_code||'unknown'); }
export function storage_year(identity: BillIdentity): string { return identity.sb_date ? parse_date(identity.sb_date).slice(0,4) : 'unknown'; }
export function gmail_identity_terms(identity: BillIdentity): string[] { return identity.sb_date ? [identity.sb_no,`No${identity.sb_no}Dt${compact(identity.sb_date)}`] : []; }
export function matches_identity(expected: BillIdentity, actual: BillIdentity): boolean { return (['sb_no','sb_date','port_code','document_date'] as const).every(k=> expected[k]==null || expected[k]===actual[k]); }
export function document_codes(fields: Set<FieldName>): Set<DocumentCode> { return new Set([...fields].map(f=>FIELD_DOCUMENTS[f])); }
export function discovery_codes(fields: Set<FieldName>): Set<DocumentCode> { return fields.size===1 && fields.has('leo_date') ? document_codes(fields) : new Set(Object.values(FIELD_DOCUMENTS)); }
export function label_matches(field: string,text: string): boolean { return LABELS[field]?.test(text.trim()) ?? false; }
export function secondary_identity_label(text:string,role:'anchor'|'companion'): boolean { return SECONDARY_IDENTITY[role].test(text.trim()); }
export function value_token(field:string,input:string): string|null {
 const text=input.trim(); if (EXCEL_BLANK_VALUES.includes(text.toUpperCase())) return null;
 if (field==='leo_date'||field==='sb_date') return parse_date(text);
 const checks:Record<string,RegExp>={sb_no:/^[0-9]{7}$/,port_code:/^[A-Z0-9]{6}$/,container_no:/^[A-Z]{3}[UJZ][0-9]{7}$/,container_size:/^(?:20|40|45)(?:\s*(?:FT|HC|GP|HQ))?$/i,customs_seal:/^[A-Za-z0-9][A-Za-z0-9/-]*$/};
 if (checks[field]&&!checks[field].test(text)) throw new Error(`Invalid ${field}`);
 if (field==='shipper_name'&&(!EXPORTER_NAME.test(text)||ADDRESS_ONLY.test(text))) throw new Error('Exporter cell contains address-only or unsupported company evidence');
 if (field==='inv_no') {
  const parts=text.split(/\s+/), first=parts[0]!;
  try { parse_date(first); } catch { /* not a date */ return invoice_token(first,parts); }
  throw new Error('Invoice cell contains date without primary invoice token');
 }
 return text;
}
function invoice_token(first:string,parts:string[]):string { if (!/^[A-Za-z0-9][A-Za-z0-9/_-]*$/.test(first)) throw new Error('Invalid primary invoice token'); if(parts.length>1) parse_date(parts[1]!); return first; }

/** Success is terminal (skipped on later runs), so it requires every field accepted from one consistent source set. */
export function result_is_success(result: BillResult): boolean {
  if (result.errors.length || result.issues.length) return false;
  if (DATA_FIELDS.some(name => result.fields[name]?.state !== 'accepted')) return false;
  const sources = new Map(result.selected_sources.map(source => [source.code, source]));
  if (sources.size !== result.selected_sources.length || new Set(Object.values(FIELD_DOCUMENTS)).size !== sources.size) return false;
  if (!result.containers.length || result.containers.some(record => Object.values(CONTAINER_ATTRS).some(attr => !record[attr]))) return false;
  const expected = {...result.identity, document_date: null};
  return DATA_FIELDS.every(name => {
    const code = FIELD_DOCUMENTS[name], source = sources.get(code), field = result.fields[name]!, digest = result.selected_hashes[code], pages = PAGES[code];
    if (!source || !digest || !matches_identity(expected, source.identity)) return false;
    if (!CONTAINER_FIELDS.includes(name) && !field.value) return false;
    return (field.evidence ?? []).every(e => e.filename === source.filename && e.content_hash === digest && (pages === null || pages.includes(e.page_number)));
  });
}
