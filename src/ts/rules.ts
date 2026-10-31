/** ICEGATE rules and data validation; no network, filesystem or workbook side effects. */
export const EXCEL_DATE_NUMBER_FORMAT = 'dd\\-mmm\\-yyyy';
/** Portal placeholders that mean "no value". */
export const BLANK_VALUES = ['', '-', 'N.A.', 'N/A', 'NA', 'NONE', 'NULL'];
/** Portal statuses treated as cancelled in the report. */
export const CANCELLED_STATUS = /\bcancel|^purge after 30 days$/i;
export const DERIVED_LCL = 'LCL';
/** Containers are reused; bills of different exporters share one box (LCL) only when their SB dates are this close. */
export const LCL_WINDOW_DAYS = 14;
export const LABELS: Record<string,RegExp> = {
 exporter:/^3\s*\.\s*Exporter$/i, iec:/^IEC\s*\/\s*Br\.?$/i,
 inv_no:/^2\s*\.\s*INVOICE\s+No\.?\s*&\s*Dt\.?$/i, leo_date:/^6\s*\.\s*LEO\s+Date\.?$/i,
 container_no:/^11\s*[.,]\s*Container(?:\s+No\.?)?$/i, customs_seal:/^12\s*\.\s*Seal(?:\s+No\.?)?$/i,
 container_size:/^14\s*\.\s*Size$/i, sb_no:/^(?:\d+\s*\.\s*)?(?:SB\s*No\.?|Shipping\s+Bill\s+No\.?)$/i,
 sb_date:/^(?:\d+\s*\.\s*)?(?:SB\s*(?:Date|DT)|Shipping\s+Bill\s+Date)$/i, port_code:/^(?:\d+\s*\.\s*)?Port\s+Code$/i
};
export const NUMBERED_LABEL = /^\d+\s*\.\s*[A-Za-z]/, NUMBERED_PREFIX = /^\d+\s*[.,]$/;
export const ROW_TOLERANCE=3, COLUMN_TOLERANCE=3, MAX_CELL_DEPTH=65, MAX_INLINE_GAP=25;
export const CONTAINER_FIELDS = ['container_no','customs_seal','container_size'];
export const CONTAINER_ATTRS = {container_no:'number',container_size:'size',customs_seal:'seal'} as const;
export const MOVEMENT_STOP = /^(?:\d+\s*\.\s*[A-Za-z]|DECLARATION|NOTE\b|SIGNATURE|PAGE\s+\d)/i;
export const CELL_LEFT_MARGIN: Record<string,number> = {sb_no:12,sb_date:12,port_code:12,inv_no:40};
export const INLINE_VALUE_FIELDS = new Set(['exporter','iec','sb_no','sb_date','port_code',...CONTAINER_FIELDS]);
export const IDENTITY_FIELDS = ['sb_no','sb_date','port_code'] as const;
export const SECONDARY_IDENTITY = {anchor:/^1\s*\.\s*SB\s*NO$/i,companion:/^4\s*\.\s*SB\s*DT$/i,field:'sb_date',row_offset:13.5} as const;
const MONTHS=['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
function iso_date(y: number, m: number, d: number): string {
 if (y < 1 || y > 9999 || m < 1 || m > 12 || d < 1 || d > 31) throw new Error('Invalid date');
 const dt = new Date(0); dt.setUTCFullYear(y,m-1,d); dt.setUTCHours(0,0,0,0);
 if (dt.getUTCFullYear()!==y || dt.getUTCMonth()!==m-1 || dt.getUTCDate()!==d) throw new Error('Invalid date');
 return `${String(y).padStart(4,'0')}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
/** Indian financial year (April–March) of an ISO date, as in the sheet name: 2026-10-09 → 2026-27. */
export function fy_of(iso: string): string {
  const y = +iso.slice(0, 4) - (+iso.slice(5, 7) < 4 ? 1 : 0);
  return `${y}-${String(y + 1).slice(2)}`;
}

export function parse_date(input: string): string {
 let m: RegExpMatchArray | null;
 const text=input.trim().replace(/[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/,'');
 if ((m=text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) return iso_date(+m[1]!,+m[2]!,+m[3]!);
 if ((m=text.match(/^(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})$/))) return iso_date(+m[4]!,+m[3]!,+m[1]!);
 if ((m=text.match(/^(\d{2})(\d{2})(\d{4})$/))) return iso_date(+m[3]!,+m[2]!,+m[1]!);
 if ((m=text.match(/^(\d{1,2})[ -]([A-Za-z]{3})[ -](\d{2}|\d{4})$/))) {
  const month=MONTHS.indexOf(m[2]!.toLowerCase())+1; const yr=+m[3]!; return iso_date(m[3]!.length===2 ? (yr<=68?2000+yr:1900+yr) : yr,month,+m[1]!);
 }
 throw new Error('Invalid associated date');
}
/** The portal sometimes lists a gatepass with a placeholder SB date (e.g. 20-12-2002); a real one is dated at most this many days before its job date. */
const GATEPASS_SB_LEAD_DAYS=31;
export function plausible_sb_date(sb_date: string, job_date: string): boolean {
 const lead=(Date.parse(parse_date(job_date))-Date.parse(parse_date(sb_date)))/864e5;
 return lead>=0 && lead<=GATEPASS_SB_LEAD_DAYS;
}
/** A bill's identity from portal text; throws on anything that is not seven SB digits, a date and a six-character port. */
export function bill_identity(sb_no: string, sb_date: string, port_code: string): { sb_no: string; sb_date: string; port_code: string } {
 const no=String(sb_no).trim(), port=String(port_code).trim().toUpperCase();
 if (!/^[0-9]{7}$/.test(no)) throw new Error('SB number must contain seven ASCII digits');
 if (!/^[A-Z0-9]{6}$/.test(port)) throw new Error('Port code must contain six ASCII letters/digits');
 return {sb_no:no,sb_date:parse_date(String(sb_date)),port_code:port};
}
export function label_matches(field: string,text: string): boolean { return LABELS[field]?.test(text.trim()) ?? false; }
export function secondary_identity_label(text:string,role:'anchor'|'companion'): boolean { return SECONDARY_IDENTITY[role].test(text.trim()); }
export function value_token(field:string,input:string): string|null {
 const text=input.trim(); if (BLANK_VALUES.includes(text.toUpperCase())) return null;
 if (field==='sb_date') return parse_date(text);
 const checks:Record<string,RegExp>={sb_no:/^[0-9]{7}$/,port_code:/^[A-Z0-9]{6}$/,iec:/^[A-Z0-9]{10}$/,container_no:/^[A-Z0-9]{4,15}$/,container_size:/^(?:20|40|45)(?:\s*(?:FT|HC|GP|HQ))?$/i,customs_seal:/^[A-Za-z0-9][A-Za-z0-9/-]*$/};
 if (checks[field]&&!checks[field].test(text)) throw new Error(`Invalid ${field}`);
 return text;
}
