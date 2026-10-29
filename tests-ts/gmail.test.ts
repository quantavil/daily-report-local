import { describe, expect, test } from 'bun:test';
import { GmailClient, GmailError, type GwsRunner } from '../src/ts/gmail';
import type { BillIdentity } from '../src/ts/models';

const identity: BillIdentity = { sb_no: '7546404', sb_date: '2026-10-07', port_code: 'INMBD6' };
const filename = '754640407102026INMBD6SB22081020260915.pdf';

function fixture(sender = 'ICEGATE <noreply@icegate.gov.in>') {
  return { labelIds: [], payload: { headers: [{ name: 'From', value: sender }], parts: [
    { partId: '1.0', filename, body: { attachmentId: 'blob' } },
    { partId: '1.1', filename, body: { data: 'JVBERi1pbmxpbmU' } },
  ] } };
}

function runner(respond: (command: string, params: Record<string, unknown>) => unknown): GwsRunner {
  return async (args) => {
    const at = args.indexOf('--params');
    const payload = respond(args.slice(1, at).join(' '), JSON.parse(args[at + 1]!));
    return { exitCode: 0, stdout: JSON.stringify(payload), stderr: '' };
  };
}

describe('gws Gmail adapter', () => {
  test('paginates, verifies sender and identity, and reads nested inline and remote attachments', async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const run = runner((command, params) => {
      calls.push([command, params]);
      if (command === 'gmail users messages list') return params.pageToken ? { messages: [{ id: 'two' }] } : { messages: [{ id: 'one' }], nextPageToken: 'next' };
      if (command === 'gmail users messages get') return fixture();
      if (command === 'gmail users messages attachments get') return { data: 'JVBERi1yZW1vdGU' };
      throw new Error(command);
    });
    const client = new GmailClient({ executable: 'gws', runner: run });
    const found = await client.discover(identity, new Set(['SB22']));
    expect(found).toHaveLength(4);
    expect(calls[0]![1].q).toBe('from:noreply@icegate.gov.in has:attachment -in:spam -in:trash {"7546404" "No7546404Dt07102026"}');
    expect(calls.find(([, params]) => params.pageToken === 'next')).toBeDefined();
    expect(Buffer.from(await client.download(found.find(a => a.inline_data)!)).toString()).toBe('%PDF-inline');
    expect(Buffer.from(await client.download(found.find(a => a.attachment_id)!)).toString()).toBe('%PDF-remote');
  });

  test('rejects sender spoofing, forwarded messages and spam', async () => {
    const values = [fixture('noreply@icegate.gov.in.evil.test'), fixture('noreply@icegate.gov.in, attacker@example.com'), fixture()];
    values[2]!.payload.headers.push({ name: 'X-Forwarded-For', value: 'other@example.com' });
    const client = new GmailClient({ executable: 'gws', runner: runner((command, params) => {
      if (command.endsWith('list')) return { messages: values.map((_, id) => ({ id: String(id) })) };
      return values[Number(params.id)];
    }) });
    expect(await client.discover(identity, new Set(['SB22']))).toEqual([]);
  });

  test('does not silently report failed Gmail access as no messages', async () => {
    const client = new GmailClient({ executable: 'gws', runner: async () => ({ exitCode: 1, stdout: '', stderr: '{"error":{"code":401,"message":"invalid_grant"}}' }) });
    await expect(client.authenticate()).rejects.toThrow('gws auth login');
    await expect(client.discover(identity, new Set(['SB22']))).rejects.toBeInstanceOf(GmailError);
  });

  test('without SB date searches approved sender and detects repeated pagination', async () => {
    const client = new GmailClient({ executable: 'gws', runner: runner((command, params) => {
      expect(command).toBe('gmail users messages list');
      expect(params.q).toBe('from:noreply@icegate.gov.in has:attachment -in:spam -in:trash');
      return { nextPageToken: 'same' };
    }) });
    await expect(client.discover({ sb_no: '7546404' }, new Set(['SB22']))).rejects.toThrow('pagination');
  });

  test('missing gws executable is reported without asking to log in', async () => {
    const client = new GmailClient({ executable: '/nonexistent/gws' });
    await expect(client.authenticate()).rejects.toThrow('not found');
  });

  test('accepts only the requested document date and rejects malformed attachment bytes', async () => {
    const client = new GmailClient({ executable: 'gws', runner: runner(command => command.endsWith('list') ? { messages: [{ id: 'one' }] } : fixture()) });
    expect(await client.discover({ ...identity, document_date: '2026-10-09' }, new Set(['SB22']))).toEqual([]);
    const [attachment] = await client.discover(identity, new Set(['SB22']));
    await expect(client.download({ ...attachment!, inline_data: 'invalid$' })).rejects.toThrow('malformed');
  });
});
