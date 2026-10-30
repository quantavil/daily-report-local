/** Read-only Gmail search for candidate PDF attachments. No message contents are logged. */
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import type { BillKey } from './store/db';

type Runner = (method: 'list' | 'get' | 'attachment', params: Record<string, unknown>) => Promise<any>;
type Part = { filename?: string; mimeType?: string; body?: { attachmentId?: string; data?: string; size?: number }; parts?: Part[] };
export class GmailError extends Error {}
const MAX_PDF = 20 * 1024 * 1024;

export function gmail_runner(): Runner {
  const local = join(process.env.HOME ?? '', '.bun/bin/gws');
  const executable = Bun.which('gws') ?? (existsSync(local) ? local : 'gws');
  return async (method, params) => {
    const commands = method === 'attachment' ? ['users', 'messages', 'attachments', 'get'] : ['users', 'messages', method];
    const proc = (() => {
      try { return Bun.spawn([executable, 'gmail', ...commands, '--params', JSON.stringify({userId:'me', ...params})], {stdout:'pipe', stderr:'pipe'}); }
      catch { throw new GmailError('Gmail lookup failed; gws is not available'); }
    })();
    const [out, , code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
    if (code !== 0) throw new GmailError(`Gmail lookup failed (gws exit ${code}); check Gmail authorization`);
    try { return JSON.parse(out); } catch { throw new GmailError('Gmail lookup returned invalid JSON'); }
  };
}

function* pdf_parts(part: Part | undefined): Generator<Part> {
  if (!part) return;
  if (part.filename && (part.mimeType === 'application/pdf' || /\.pdf$/i.test(part.filename))) yield part;
  for (const child of part.parts ?? []) yield* pdf_parts(child);
}

export async function* gmail_gatepasses(bill: BillKey, run: Runner = gmail_runner()): AsyncGenerator<Uint8Array> {
  const year = Number(bill.sb_date.slice(0,4)) - (Number(bill.sb_date.slice(5,7)) < 4 ? 1 : 0);
  const base = `"${bill.sb_no}" has:attachment filename:pdf`;
  const queries = [`${base} after:${year}/03/31 before:${year+1}/04/01`, `${base} after:${year+1}/03/31`];
  for (const q of queries) {
    let pageToken: string | undefined;
    do {
      const list = await run('list', {q, maxResults:100, ...(pageToken ? {pageToken} : {})});
      for (const m of list.messages ?? []) {
        if (typeof m.id !== 'string') continue;
        const message = await run('get', {id:m.id, format:'full'});
        for (const part of pdf_parts(message.payload)) {
          if ((part.body?.size ?? 0) > MAX_PDF) continue;
          const data = part.body?.attachmentId ? (await run('attachment', {messageId:m.id, id:part.body.attachmentId})).data : part.body?.data;
          if (typeof data !== 'string' || data.length > MAX_PDF * 1.4) continue;
          const bytes = Buffer.from(data, 'base64url');
          if (bytes.length <= MAX_PDF && bytes.subarray(0,5).toString() === '%PDF-') yield bytes;
        }
      }
      pageToken = typeof list.nextPageToken === 'string' ? list.nextPageToken : undefined;
    } while (pageToken);
  }
}
