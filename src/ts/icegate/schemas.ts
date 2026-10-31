/** Shapes of `icegate ... --json` output and the field catalog that drives the database columns and the export picker. */
import * as v from 'valibot';

const text = v.string();
const rows = v.optional(v.array(v.record(text, text)), []);

export const JobStatusRows = v.array(v.record(text, text));
export const MyIecRows = v.array(v.object({ locationCode: text, iecName: text, iecCode: text }));
export const Enquiry = v.object({
  sbDetailsModel: rows, currentStatusModel: rows, roslStatusModel: rows, gatewayEgmStatusModel: rows,
  egmStatusModel: rows, igstInvoiceDetailsModel: rows,
});
export const EcopyRecords = v.array(v.record(text, v.nullish(text)));
export type EnquiryData = v.InferOutput<typeof Enquiry>;

export type FieldKind = 'text' | 'date';
/** `from` is the portal source: a Job Status column or an enquiry model; `key` is the name inside it. */
export interface BillField { col: string; label: string; kind: FieldKind; from: 'job' | EnquiryModel; key: string }
export type EnquiryModel = 'sbDetailsModel' | 'currentStatusModel' | 'roslStatusModel' | 'gatewayEgmStatusModel';

const f = (from: BillField['from'], key: string, col: string, label: string, kind: FieldKind = 'text'): BillField => ({ col, label, kind, from, key });
export const BILL_FIELDS: BillField[] = [
  f('job', 'IEC', 'iec', 'IEC'), f('job', 'Status', 'job_status', 'Job status'), f('job', 'Job No', 'job_no', 'Job no'),
  f('job', 'Job Date', 'job_date', 'Job date', 'date'), f('job', 'Control Number', 'control_number', 'Control number'),
  f('sbDetailsModel', 'jobNo', 'job_no', 'Job no'), f('sbDetailsModel', 'jobDate', 'job_date', 'Job date', 'date'),
  f('sbDetailsModel', 'chaNo', 'cha_no', 'CHA number'), f('sbDetailsModel', 'portOfDischarge', 'port_of_discharge', 'Port of discharge'),
  f('sbDetailsModel', 'totalPackage', 'total_package', 'Total packages'), f('sbDetailsModel', 'grossWeight', 'gross_weight', 'Gross weight'),
  f('sbDetailsModel', 'fob', 'fob', 'FOB (INR)'), f('sbDetailsModel', 'totalCess', 'total_cess', 'Total cess'),
  f('sbDetailsModel', 'drawback', 'drawback', 'Drawback'), f('sbDetailsModel', 'str', 'str', 'STR'), f('sbDetailsModel', 'total', 'dbk_str_total', 'DBK + STR'),
  f('sbDetailsModel', 'cinNo', 'cin_no', 'CIN no'), f('sbDetailsModel', 'cinDate', 'cin_date', 'CIN date', 'date'), f('sbDetailsModel', 'rewardFlag', 'reward_flag', 'Reward flag'),
  f('currentStatusModel', 'currQueue', 'curr_queue', 'Current queue'), f('currentStatusModel', 'currStatus', 'curr_status', 'Current status'),
  f('currentStatusModel', 'leoDate', 'leo_date', 'LEO date', 'date'), f('currentStatusModel', 'epCopy', 'ep_copy', 'EP copy printed'),
  f('currentStatusModel', 'custScrollNo', 'scroll_no', 'DBK scroll no'),
  f('currentStatusModel', 'scrollDate', 'scroll_date', 'DBK scroll date', 'date'), f('currentStatusModel', 'markDate', 'mark_date', 'Mark date', 'date'),
  f('currentStatusModel', 'examDate', 'exam_date', 'Exam date', 'date'), f('currentStatusModel', 'appraisalDate', 'appraisal_date', 'Appraisal date', 'date'),
  f('currentStatusModel', 'readyToPr', 'ready_to_pr', 'Ready to process'), f('currentStatusModel', 'sampleDrawn', 'sample_drawn', 'Sample drawn'),
  f('currentStatusModel', 'testReport', 'test_report', 'Test report'), f('currentStatusModel', 'wareNm', 'ware_name', 'Warehouse'),
  f('roslStatusModel', 'roslAvailed', 'rosl_availed', 'ROSL availed'), f('roslStatusModel', 'roslAmt', 'rosl_amount', 'ROSL amount'),
  f('roslStatusModel', 'roslScrollNo', 'rosl_scroll_no', 'ROSL scroll no'), f('roslStatusModel', 'roslScrollDate', 'rosl_scroll_date', 'ROSL scroll date', 'date'),
  f('gatewayEgmStatusModel', 'awbNo', 'awb_no', 'AWB no'), f('gatewayEgmStatusModel', 'custGatewayPort', 'gateway_port', 'Gateway port'),
  f('gatewayEgmStatusModel', 'gatewaySiteId', 'gateway_site_id', 'Gateway site'),
];
/** Columns that are not fetched field-by-field: set by the workflow itself. */
export const OWN_FIELDS: { col: string; label: string; kind: FieldKind }[] = [
  { col: 'exporter', label: 'Exporter', kind: 'text' },
];
/** Related-record fields, joined with newlines when exported. Table `invoices` or `containers`. */
export const LIST_FIELDS: { id: string; label: string; kind: FieldKind }[] = [
  { id: 'invoices.invoice_no', label: 'Invoice no', kind: 'text' }, { id: 'invoices.invoice_date', label: 'Invoice date', kind: 'date' }, { id: 'invoices.igst_value', label: 'IGST paid', kind: 'text' },
  { id: 'containers.container_no', label: 'Container no', kind: 'text' }, { id: 'containers.size', label: 'Container size', kind: 'text' }, { id: 'containers.seal', label: 'Customs seal', kind: 'text' },
  { id: 'containers.egm_no', label: 'EGM no', kind: 'text' }, { id: 'containers.egm_date', label: 'EGM date', kind: 'date' },
];
/** Columns of the bill row that identify it; exportable but never fetched field-by-field. */
const IDENTITY_FIELDS: { id: string; label: string; kind: FieldKind }[] = [
  { id: 'bills.sb_no', label: 'Shipping bill no', kind: 'text' }, { id: 'bills.sb_date', label: 'Shipping bill date', kind: 'date' }, { id: 'bills.port_code', label: 'Port code', kind: 'text' },
];
/** Computed at export time from what the database holds, never stored. */
const DERIVED_FIELDS: { id: string; label: string; kind: FieldKind }[] = [
  { id: 'derived.status', label: 'Status', kind: 'text' }, { id: 'derived.type_shipment', label: 'Type shipment (derived; LCL only)', kind: 'text' },
];
/** Every exportable field id (`bills.<col>` or a list field) with its label and kind. */
export const CATALOG: { id: string; label: string; kind: FieldKind }[] = [
  ...new Map([...BILL_FIELDS, ...OWN_FIELDS].map(b => [b.col, b] as const)).values().map(b => ({ id: `bills.${b.col}`, label: b.label, kind: b.kind })),
  ...IDENTITY_FIELDS, ...LIST_FIELDS, ...DERIVED_FIELDS,
];
