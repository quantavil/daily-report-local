/** Read-only Gmail discovery through Google Workspace CLI. */
import type { Attachment, BillIdentity, DocumentCode } from './models';
import { attachment_key } from './models';
import { filename_prefix, gmail_identity_terms, matches_identity, parse_filename, SENDER } from './rules';

export class GmailError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GmailError';
  }
}

type RunResult = { exitCode: number; stdout: string; stderr: string };
export type GwsRunner = (args: string[]) => Promise<RunResult>;

type Part = {
  partId?: string;
  filename?: string;
  body?: { attachmentId?: string; data?: string };
  headers?: Array<{ name?: string; value?: string }>;
  parts?: Part[];
};
type Message = { labelIds?: string[]; payload?: Part };

async function runGws(args: string[]): Promise<RunResult> {
  const timeout = AbortSignal.timeout(60_000);
  try {
    const child = Bun.spawn(args, { stdout: 'pipe', stderr: 'pipe', signal: timeout });
    const [stdout, stderr, exitCode] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    if (timeout.aborted) throw new GmailError('gws request timed out; check your network and try again.');
    return { stdout, stderr, exitCode };
  } catch (error) {
    if (error instanceof GmailError) throw error;
    const details = error as { code?: string; name?: string };
    if (details.code === 'ENOENT') throw new GmailError('gws was not found on PATH. Install it or set GWS_BIN.');
    if (timeout.aborted || details.name === 'TimeoutError' || details.name === 'AbortError') throw new GmailError('gws request timed out; check your network and try again.');
    throw new GmailError('Could not start gws. Check the installation and GWS_BIN.');
  }
}

function parseResponse(raw: string): unknown {
  try { return JSON.parse(raw); } catch { return undefined; }
}

function authenticationFailure(result: RunResult): boolean {
  const parsed = parseResponse(result.stdout) ?? parseResponse(result.stderr);
  const text = JSON.stringify(parsed ?? '').toLowerCase();
  return /(?:invalid_grant|autherror|unauthenticated|no credentials provided|token.*(?:expired|revoked)|"code"\s*:\s*401)/.test(text);
}

function senderApproved(headers: Part['headers']): boolean {
  if (!Array.isArray(headers)) return false;
  if (headers.some(h => ['x-forwarded-for', 'x-forwarded-to', 'resent-from', 'resent-to'].includes((h.name ?? '').toLowerCase()))) return false;
  const senders = headers.filter(h => (h.name ?? '').toLowerCase() === 'from');
  if (senders.length !== 1 || typeof senders[0]!.value !== 'string') return false;
  const value = senders[0]!.value.trim();
  const address = SENDER.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // Accept a single mailbox only; a display name may be quoted or unquoted.
  return new RegExp(`^(?:${address}|(?:"(?:[^"\\\\]|\\\\.)*"|[^<>,@"]*)\\s*<${address}>)$`, 'i').test(value);
}

function* parts(part: Part): Generator<Part> {
  yield part;
  if (Array.isArray(part.parts)) for (const child of part.parts) yield* parts(child);
}

function decodeBase64(data: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*={0,2}$/.test(data) || data.replace(/=+$/, '').length % 4 === 1) {
    throw new GmailError('Gmail returned malformed attachment data.');
  }
  const bytes = Buffer.from(data, 'base64url');
  if (bytes.toString('base64url') !== data.replace(/=+$/, '')) throw new GmailError('Gmail returned malformed attachment data.');
  return bytes;
}

export class GmailClient {
  private readonly executable: string;
  private readonly runner: GwsRunner;

  constructor(options: { executable?: string; runner?: GwsRunner } = {}) {
    this.executable = options.executable ?? process.env.GWS_BIN ?? (Bun.which('gws') ?? `${process.env.HOME}/.bun/bin/gws`);
    this.runner = options.runner ?? runGws;
  }

  private async request(command: string[], params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const response = await this.runner([this.executable, 'gmail', ...command, '--params', JSON.stringify(params)]);
    if (response.exitCode !== 0) {
      if (authenticationFailure(response)) throw new GmailError('Gmail login expired or unavailable. Run gws auth login, then retry.');
      throw new GmailError('Gmail request failed. Check your network, Google permissions, and gws configuration.');
    }
    const data = parseResponse(response.stdout);
    if (!data || typeof data !== 'object' || Array.isArray(data) || 'error' in data) throw new GmailError('gws returned an invalid Gmail response.');
    return data as Record<string, unknown>;
  }

  async authenticate(): Promise<void> {
    await this.request(['users', 'messages', 'list'], { userId: 'me', maxResults: 1 });
  }

  async discover(identity: BillIdentity, codes: Set<DocumentCode>): Promise<Attachment[]> {
    if (!codes.size) return [];
    const prefixes = [...codes].map(code => filename_prefix(identity, code));
    const terms = gmail_identity_terms(identity);
    const query = `from:${SENDER} has:attachment -in:spam -in:trash` + (terms.length ? ` {${terms.map(t => `"${t}"`).join(' ')}}` : '');
    const found = new Map<string, Attachment>();
    const seenMessages = new Set<string>();
    const seenTokens = new Set<string>();
    let pageToken: string | undefined;
    do {
      const params: Record<string, unknown> = { userId: 'me', q: query, includeSpamTrash: false, maxResults: 100 };
      if (pageToken) params.pageToken = pageToken;
      const response = await this.request(['users', 'messages', 'list'], params);
      if (response.messages !== undefined && !Array.isArray(response.messages)) throw new GmailError('Gmail returned an invalid message list.');
      for (const stub of (response.messages ?? []) as Array<{id?: string}>) {
        if (!stub || typeof stub.id !== 'string' || !stub.id) throw new GmailError('Gmail returned a message without an ID.');
        if (seenMessages.has(stub.id)) continue;
        seenMessages.add(stub.id);
        const message = await this.request(['users', 'messages', 'get'], { userId: 'me', id: stub.id, format: 'full' }) as Message;
        if (message.labelIds?.some(label => label === 'SPAM' || label === 'TRASH') || !senderApproved(message.payload?.headers)) continue;
        let ordinal = 0;
        for (const part of parts(message.payload!)) {
          const currentOrdinal = ordinal++;
          const filename = part.filename;
          if (!filename || !prefixes.some(prefix => filename.startsWith(prefix))) continue;
          let metadata;
          try { metadata = parse_filename(filename); } catch { continue; }
          if (!codes.has(metadata.code) || !matches_identity(identity, metadata.identity)) continue;
          const body = part.body;
          if (!body?.attachmentId && body?.data === undefined) continue;
          const attachment: Attachment = { metadata, message_id: stub.id, part_id: part.partId || `ordinal:${currentOrdinal}`, attachment_id: body?.attachmentId ?? null, inline_data: body?.data ?? null };
          found.set(attachment_key(attachment), attachment);
        }
      }
      if (response.nextPageToken !== undefined && typeof response.nextPageToken !== 'string') throw new GmailError('Gmail returned an invalid pagination token.');
      pageToken = response.nextPageToken as string | undefined;
      if (pageToken && seenTokens.has(pageToken)) throw new GmailError('Gmail returned a repeated pagination token.');
      if (pageToken) seenTokens.add(pageToken);
    } while (pageToken);
    return [...found.values()];
  }

  async download(attachment: Attachment): Promise<Uint8Array> {
    let encoded = attachment.inline_data;
    if (encoded == null) {
      if (!attachment.attachment_id) throw new GmailError('Attachment has neither inline data nor an attachment ID.');
      const result = await this.request(['users', 'messages', 'attachments', 'get'], { userId: 'me', messageId: attachment.message_id, id: attachment.attachment_id });
      if (typeof result.data !== 'string') throw new GmailError('Gmail returned an attachment without data.');
      encoded = result.data;
    }
    return decodeBase64(encoded);
  }
}
