/** Shared business records and provenance. Calendar dates use ISO YYYY-MM-DD strings. */
export type DocumentCode = 'SB21' | 'SB22' | 'SB35';
export type FieldName = 'shipper_name' | 'inv_no' | 'leo_date' | 'container_no' | 'container_size' | 'customs_seal';
export type FieldState = 'accepted' | 'blank' | 'rejected' | 'missing' | 'error';
export interface BillIdentity { sb_no: string; sb_date?: string | null; port_code?: string | null; document_date?: string | null }
export interface DocumentMetadata { identity: BillIdentity; code: DocumentCode; document_at: string; filename: string }
export interface Attachment { metadata: DocumentMetadata; message_id: string; attachment_id?: string | null; inline_data?: string | null; part_id?: string | null }
export function attachment_key(a: Attachment): string { return [a.message_id, a.part_id || a.attachment_id || '', a.metadata.filename].join('|'); }
export interface Evidence { filename: string; content_hash: string; page_number: number; label: string; quote: string; bbox: [number, number, number, number] }
export interface FieldResult { state: FieldState; value?: string | null; value_type?: 'text' | 'date' | 'records'; evidence?: Evidence[]; reason?: string | null }
export interface ContainerRecord { number?: string | null; size?: string | null; seal?: string | null; evidence: Evidence[] }
export interface LayoutSpan { text: string; bbox: [number, number, number, number] }
export interface LayoutPage { number: number; width: number; height: number; spans: LayoutSpan[] }
export interface DocumentLayout { document_code?: DocumentCode | null; pages: LayoutPage[] }
export interface ParsedDocument { metadata: DocumentMetadata; content_hash: string; fields: Partial<Record<FieldName,FieldResult>>; containers: ContainerRecord[] }
export interface BillResult { identity: BillIdentity; fields: Partial<Record<FieldName,FieldResult>>; containers: ContainerRecord[]; selected_sources: DocumentMetadata[]; selected_hashes: Partial<Record<DocumentCode,string>>; issues: string[]; errors: string[] }
export interface WorkbookRow { row_idx: number; identity: BillIdentity; values: Partial<Record<FieldName,string | null>>; cancelled: boolean; status?: string | null; error?: string | null }
export type RowStatus = 'Success' | 'Partial' | 'Cancelled' | 'Not found' | 'Error';
export interface RowUpdate { row_idx: number; values: Partial<Record<FieldName,string | null>>; containers?: ContainerRecord[] | null; container_fields: Set<FieldName>; status: RowStatus; detail: string }
