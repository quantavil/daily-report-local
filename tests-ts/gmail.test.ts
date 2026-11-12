import { describe, expect, test } from 'bun:test';
import { gmail_gatepasses } from '../src/ts/gmail';

describe('Gmail gatepass search', () => {
  test('searches the financial year then later mail, walks MIME parts and fetches attachments', async () => {
    const calls: {method:string; params:any}[] = [];
    const run = async (method:string, params:any) => {
      calls.push({method,params});
      if (method === 'list') return calls.filter(c => c.method === 'list').length === 1 ? {messages:[{id:'m1'}]} : {messages:[{id:'m2'}]};
      if (method === 'get') return {payload:{parts:[{filename:'gatepass.pdf', mimeType:'application/pdf',body:{attachmentId:params.id}}]}};
      return {data: Buffer.from('%PDF-example').toString('base64url')};
    };
    const result = [];
    for await (const item of gmail_gatepasses({sb_no:'1234567',sb_date:'2023-03-15',port_code:'INMBD6'},run)) result.push(item);
    expect(result).toHaveLength(2);
    expect(calls[0]!.params.q).toContain('after:2022/03/31');
    expect(calls[0]!.params.q).toContain('before:2023/04/01');
    expect(calls.findLast(c=>c.method==='list')!.params.q).toContain('after:2023/03/31');
    expect(calls.filter(c=>c.method==='attachment')).toHaveLength(2);
  });
});
