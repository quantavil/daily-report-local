import { describe, expect, test } from 'bun:test';
import { extract_layout, parse_document, render_markdown } from '../src/ts/documents';
import { parse_filename } from '../src/ts/rules';
import type { DocumentLayout, LayoutSpan } from '../src/ts/models';

const span = (text: string, x: number, y: number, w: number): LayoutSpan => ({text, bbox: [x,y,x+w,y+10]});
const identity = (): LayoutSpan[] => [span('SB No',20,20,50),span('7546404',80,20,70),span('SB Date',200,20,60),span('07/10/2026',270,20,90),span('Port Code',390,20,70),span('INMBD6',470,20,65)];
const layout = (...pages: LayoutSpan[][]): DocumentLayout => ({pages: pages.map((spans,i)=>({number:i+1,width:600,height:800,spans}))});
const meta = (code: string) => parse_filename(`754640407102026INMBD6${code}081020261432.pdf`);

describe('native PDF parsing', () => {
 test('extracts exporter and invoice from anchored cells without CHA or address', () => {
  const result = parse_document(layout([...identity(), span("1. EXPORTER'S NAME & ADDRESS",20,80,250),span('SYNTHETIC EXPORTS LTD',20,100,230),span('123 ADDRESS ROAD',20,115,180),span('4. CHA NAME',320,80,180),span('WRONG CHA LTD',320,100,180),span('2. INVOICE No. & Dt.',20,200,180),span('INV-42 07/10/2026 USD 1000',20,220,260)]),meta('SB21'),'hash');
  expect(result.fields.shipper_name?.value).toBe('SYNTHETIC EXPORTS LTD');
  expect(result.fields.inv_no?.value).toBe('INV-42');
 });
 test('rejects conflicting identity on a non-owned page', () => {
  const other=identity(); other[1]=span('1111111',80,20,70);
  const result=parse_document(layout([...identity(),span('6. LEO Date',20,100,180),span('09/10/2026',20,120,180)],other),meta('SB22'),'hash');
  expect(result.fields.leo_date?.state).toBe('rejected');
 });
 test('preserves ordered container records and missing seal position across continuation', () => {
  const headers=[span('11. Container',20,100,150),span('12. Seal',200,100,120),span('14. Size',400,100,80)];
  const first=[...identity(),...headers,span('ABCU1234567',20,130,120),span('SEAL1',200,130,90),span('40',400,130,40),span('DEFU7654321',20,150,120),span('20',400,150,40)];
  const second=[span('GHIU1234567',20,40,120),span('SEAL3',200,40,90),span('40',400,40,40)];
  const result=parse_document(layout(first,second),meta('SB35'),'hash');
  expect(result.containers.map(c=>[c.number,c.size,c.seal])).toEqual([['ABCU1234567','40','SEAL1'],['DEFU7654321','20',null],['GHIU1234567','40','SEAL3']]);
 });
 test('unreadable PDF is an explicit failure', () => {
  expect(()=>extract_layout(new TextEncoder().encode('not a pdf'),meta('SB22'))).toThrow();
 });
 test('uses reconstructed heading to prove an empty LEO cell', () => {
  const result=parse_document(layout([...identity(),span('6.',20,100,15),span('LEO Date',40,100,80),span('7. NEXT FIELD',20,150,180)]),meta('SB22'),'hash');
  expect(result.fields.leo_date?.state).toBe('blank');
 });
 test('extracts stacked movement fields on original page four', () => {
  const p4=[span('11,Container',144,183,46),span('ABCU1234567',263,183,54),span('12.Seal',72,197,27),span('SEAL1',120,197,56),span('13.Seal Type',184,197,48),span('BOLT',266,197,20),span('14.Size',315,197,27),span('40',366,197,9)];
  const result=parse_document(layout(identity(),[],[],p4),meta('SB35'),'hash');
  expect(result.containers.map(c=>[c.number,c.size,c.seal])).toEqual([['ABCU1234567','40','SEAL1']]);
  expect(result.containers[0]?.evidence[0]?.page_number).toBe(4);
 });
 test('rejects unsupported continuation instead of silently truncating records', () => {
  const first=[...identity(),span('11. Container',20,100,150),span('12. Seal',200,100,120),span('14. Size',400,100,80),span('ABCU1234567',20,130,120),span('SEAL1',200,130,90),span('40',400,130,40)];
  const second=[span('1. UNKNOWN TABLE',20,20,180),span('DEFU7654321',20,100,120),span('SEAL2',200,100,90),span('20',400,100,40)];
  expect(parse_document(layout(first,second),meta('SB35'),'hash').fields.container_no?.state).toBe('rejected');
 });
 test.each(['07/10/2026','07-10-2026','07-Oct-2026','2026-10-07','07.10.2026','07102026','07-Oct-26'])('rejects date-only invoice %s', date => {
  const result=parse_document(layout([...identity(),span('2. INVOICE No. & Dt.',20,100,180),span(date,20,120,180)]),meta('SB21'),'hash');
  expect(result.fields.inv_no?.state).toBe('rejected');
 });
 test('rejects address-only exporter and duplicate LEO labels',()=>{
  const exporter=parse_document(layout([...identity(),span("1. EXPORTER'S NAME & ADDRESS",20,80,250),span('123 ADDRESS ROAD',20,100,180)]),meta('SB21'),'hash');
  expect(exporter.fields.shipper_name?.state).toBe('rejected');
  const leo=parse_document(layout([...identity(),span('6. LEO Date',20,100,180),span('09/10/2026',20,120,180),span('6. LEO Date',300,100,180),span('10/10/2026',300,120,180)]),meta('SB22'),'hash');
  expect(leo.fields.leo_date?.state).toBe('rejected');
 });
 test('continues a table after the next page bill header',()=>{
  const first=[...identity(),span('11. Container',20,100,150),span('12. Seal',200,100,120),span('14. Size',400,100,80),span('ABCU1234567',20,130,120),span('SEAL1',200,130,90),span('40',400,130,40)];
  const next=identity(); next[0]=span('1. SB No',20,20,55);
  next.push(span('DEFU7654321',20,100,120),span('SEAL2',200,100,90),span('20',400,100,40));
  expect(parse_document(layout(first,next),meta('SB35'),'hash').containers.map(c=>c.number)).toEqual(['ABCU1234567','DEFU7654321']);
 });
 test('secondary gatepass header must match filename',()=>{
  const page=[...identity(),span('1.SB NO',72,107,32),span('7546404',124,107,31),span('2.BL NO',166,107,33),span('3.BL DT',315,107,30),span('4.SB DT',72,120.5,30),span('07-Oct-26',117,120.5,40),span('5.HBL No',166,120.5,34),span('11,Container',144,183,46),span('ABCU1234567',263,183,54),span('12.Seal',72,197,27),span('SEAL1',120,197,56),span('13.Seal Type',184,197,48),span('BOLT',266,197,20),span('14.Size',315,197,27),span('40',366,197,9)];
  expect(parse_document(layout(page),meta('SB35'),'hash').fields.container_no?.state).toBe('accepted');
  page[11]=span('08-Oct-26',117,120.5,40);
  expect(parse_document(layout(page),meta('SB35'),'hash').fields.container_no?.state).toBe('rejected');
 });
 test('only uses LEO on page one and never substitutes order date',()=>{
  const p=[...identity(),span('Order Date',20,50,180),span('08/10/2026',20,65,180),span('6. LEO Date',20,100,180),span('09/10/2026',20,120,180)];
  let result=parse_document(layout(p,[span('6. LEO Date',20,20,180),span('10/10/2026',20,40,180)]),meta('SB22'),'hash');
  expect(result.fields.leo_date?.value).toBe('2026-10-09');
  result=parse_document(layout([...identity(),span('Order Date',20,50,180),span('08/10/2026',20,65,180)]),meta('SB22'),'hash');
  expect(result.fields.leo_date?.state).toBe('rejected');
 });
 test('missing identity rejects evidence and wrong identity is rejected',()=>{
  expect(parse_document(layout([span('6. LEO Date',20,20,180),span('09/10/2026',20,40,180)]),meta('SB22'),'hash').fields.leo_date?.state).toBe('rejected');
  const p=identity();p[1]=span('1111111',80,20,70);p.push(span('6. LEO Date',20,100,180),span('09/10/2026',20,120,180));
  expect(parse_document(layout(p),meta('SB22'),'hash').fields.leo_date?.state).toBe('rejected');
 });
 test('handles overlapping header geometry and far right inline LEO',()=>{
  const p=[span('Port Code',310,30,38),span('INMBD6',322,39,31),span('SB No',395,30,24),span('7546404',391,39,31),span('SB Date',459,30,31),span('07-Oct-26',453,39,40),span('6.LEO Date.',315,543,45),span('09-Oct-26',473,543,40)];
  expect(parse_document(layout(p),meta('SB22'),'hash').fields.leo_date?.value).toBe('2026-10-09');
 });
 test('chooses first page exporter over repeated page-two party heading',()=>{
  const p=[...identity(),span("1.EXPORTER'S NAME & ADDRESS",63,192,133),span('SYNTHETIC EXPORTS',63,201,130),span('123 ADDRESS ROAD',63,210,130)];
  const p2=[span("1.EXPORTER'S NAME & ADDRESS",122,160,133),span('WRONG PARTY',122,180,130)];
  expect(parse_document(layout(p,p2),meta('SB21'),'hash').fields.shipper_name?.value).toBe('SYNTHETIC EXPORTS');
 });
 test('invalid seal does not reject validated container or size',()=>{
  const p=[...identity(),span('11. Container',20,100,150),span('12. Seal',200,100,120),span('14. Size',400,100,80),span('ABCU1234567',20,130,120),span('BAD SEAL!',200,130,100),span('40',400,130,40)];
  const result=parse_document(layout(p),meta('SB35'),'hash');
  expect(result.fields.customs_seal?.state).toBe('rejected');expect(result.fields.container_no?.state).toBe('accepted');expect(result.fields.container_size?.state).toBe('accepted');
 });
 test('split adjacent CHA heading bounds exporter evidence',()=>{
  const p=[...identity(),span("1. EXPORTER'S NAME & ADDRESS",20,80,250),span('4.',320,80,15),span('CHA NAME',340,80,100),span('SYNTHETIC EXPORTS LTD',20,100,230),span('WRONG CHA LTD',320,100,180)];
  expect(parse_document(layout(p),meta('SB21'),'hash').fields.shipper_name?.value).toBe('SYNTHETIC EXPORTS LTD');
 });
 test('ignores an unrelated port table below the header',()=>{
  const p=[...identity(),span('6. LEO Date',20,100,180),span('09/10/2026',20,120,180),span('3.PORT CODE',180,278,100),span('OTHER TABLE DETAIL',250,300,150)];
  expect(parse_document(layout(p),meta('SB22'),'hash').fields.leo_date?.value).toBe('2026-10-09');
 });
 test('rejects a second conflicting primary bill header on the same page',()=>{
  const second=identity().map(s=>({...s,bbox:[s.bbox[0],s.bbox[1]+200,s.bbox[2],s.bbox[3]+200] as LayoutSpan['bbox']}));
  second[1]=span('1111111',80,220,70);
  expect(parse_document(layout([...identity(),span('6. LEO Date',20,100,180),span('09/10/2026',20,120,180),...second]),meta('SB22'),'hash').fields.leo_date?.state).toBe('rejected');
 });
 test('rejects headerless stacked continuation rather than truncating records',()=>{
  const first=[...identity(),span('11,Container',144,183,46),span('ABCU1234567',263,183,54),span('12.Seal',72,197,27),span('SEAL1',120,197,56),span('13.Seal Type',184,197,48),span('BOLT',266,197,20),span('14.Size',315,197,27),span('40',366,197,9)];
  const continuation=[span('DEFU7654321',263,40,54),span('SEAL2',120,54,56),span('20',366,54,9)];
  expect(parse_document(layout(first,continuation),meta('SB35'),'hash').fields.container_no?.state).toBe('rejected');
 });
 test.each([['SB21',[1,2]],['SB22',[1]],['SB35',[1,2,4]]] as const)('markdown respects owned pages for %s',(code,expected)=>{
  const native:DocumentLayout={pages:[1,2,4].map(number=>({number,width:600,height:800,spans:[span(`Page ${number} label`,20,20,80),span('fragment',104,20,50),span(`Page ${number} value`,300,20,100)]}))};
  const markdown=render_markdown(native,meta(code));
  for(const n of [1,2,4]) { if((expected as readonly number[]).includes(n)) { expect(markdown).toContain(`SOURCE PAGE ${n}`); expect(markdown).toContain(`Page ${n} label fragment`); } else expect(markdown).not.toContain(`SOURCE PAGE ${n}`); }
 });
});

function syntheticPdf(inline=false): Uint8Array {
 const instructions = [
  ['SB No',20,760],['7546404',100,760],['SB Date',200,760],['07/10/2026',280,760],['Port Code',400,760],['INMBD6',480,760],
  ['6. LEO Date',20,700],['09/10/2026',inline ? 90 : 20,inline ? 700 : 680],
 ] as const;
 const content=instructions.map(([text,x,y])=>`BT /F1 10 Tf ${x} ${y} Td (${text}) Tj ET`).join('\n');
 const objects=[
  '<< /Type /Catalog /Pages 2 0 R >>',
  '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
  '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
  '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
 ];
 let pdf='%PDF-1.4\n'; const offsets=[0];
 for(let i=0;i<objects.length;i++){offsets.push(pdf.length);pdf+=`${i+1} 0 obj\n${objects[i]}\nendobj\n`;}
 const xref=pdf.length; pdf+=`xref\n0 ${objects.length+1}\n0000000000 65535 f \n`;
 for(const offset of offsets.slice(1)) pdf+=`${String(offset).padStart(10,'0')} 00000 n \n`;
 pdf+=`trailer\n<< /Size ${objects.length+1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
 return new TextEncoder().encode(pdf);
}
test('extracts real PDF geometry and anchored LEO value end-to-end',()=>{
 const metadata=meta('SB22');
 const native=extract_layout(syntheticPdf(),metadata);
 const result=parse_document(native,metadata,'hash');
 expect(result.fields.leo_date?.value).toBe('2026-10-09');
 expect(result.fields.leo_date?.evidence?.[0]?.page_number).toBe(1);
});
test('extracts an inline LEO value beside the label in native text',()=>{
 const metadata=meta('SB22');
 const result=parse_document(extract_layout(syntheticPdf(true),metadata),metadata,'hash');
 expect(result.fields.leo_date?.value).toBe('2026-10-09');
});
