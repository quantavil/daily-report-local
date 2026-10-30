/** Runs the `icegate` CLI (icegate-client) and validates its JSON. The only code that talks to ICEGATE; it never sees credentials. */
import * as v from 'valibot';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { EcopyRecords, Enquiry, JobStatusRows, MyIecRows, type EnquiryData } from './schemas';

export class IcegateError extends Error {}
/** The portal is still preparing the document; the field stays blank and a later run retries. */
export class NotReady extends IcegateError {}

export interface Bill { location: string; sb_no: string; sb_date: string }
export interface EcopyRecord { irn?: string | null; userId?: string | null; docNumber?: string | null; [key: string]: string | null | undefined }

export interface IcegateClient {
  jobStatus(): Promise<Record<string, string>[]>;
  myIec(): Promise<{ locationCode: string; iecName: string; iecCode: string }[]>;
  enquiry(bill: Bill): Promise<EnquiryData>;
  /** Batch hint: SB dates whose gatepasses will be asked for. A month with enough dates is searched in one call; the rest stay per day. Per-row runs never call this. */
  plan(sb_dates: string[]): void;
  /** Gatepasses of one bill. A gatepass record is dated like its shipping bill (checked on 464 bills), and its job number is a per-day counter unrelated to the enquiry job number, so this searches the SB date alone (or its planned month window), once per window, and matches the SB number and port. */
  gatepassSearch(bill: Bill): Promise<EcopyRecord[]>;
  /** Every gatepass record dated in `from`..`to` (ISO), whichever bill it belongs to: this is how bills are discovered. */
  gatepassWindow(from: string, to: string): Promise<EcopyRecord[]>;
  gatepassDownload(record: EcopyRecord, output: string): Promise<void>;
}

/** Last day of a `yyyy-mm` month, never after today in India (the portal rejects later dates). */
export function month_end(month: string): string {
  const [y, m] = month.split('-').map(Number) as [number, number];
  const last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10), today = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  return last < today ? last : today;
}

/** `ICEGATE_BIN` replaces the binary (tests); `ICEGATE_DIR` is where the client's `.env` lives. */
export function command_client(env = process.env): IcegateClient {
  const bin = env.ICEGATE_BIN ?? 'icegate', cwd = env.ICEGATE_DIR ?? join(homedir(), 'Documents/icegate-client');
  async function run(args: string[]): Promise<string> {
    const proc = Bun.spawn([bin, ...args, '--no-journal'], { cwd, stdout: 'pipe', stderr: 'pipe', stdin: 'ignore' });
    const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
    if (code !== 0) {
      const message = (err.trim() || out.trim() || `icegate exited ${code}`).split('\n').at(-1)!.slice(0, 300);
      throw new (/retry in about/.test(message) ? NotReady : IcegateError)(message);
    }
    return out;
  }
  async function json<T extends v.GenericSchema>(schema: T, args: string[]): Promise<v.InferOutput<T>> {
    const out = await run([...args, '--json']);
    let data: unknown;
    try { data = JSON.parse(out); } catch { throw new IcegateError(`icegate ${args.slice(0, 2).join(' ')} printed no JSON`); }
    const parsed = v.safeParse(schema, data);
    if (!parsed.success) throw new IcegateError(`icegate ${args.slice(0, 2).join(' ')} returned an unexpected shape: ${parsed.issues[0]!.message}`);
    return parsed.output;
  }
  const MONTH_AT = 5; // distinct dates in a month from which one month search beats per-day calls (~3 s fixed cost each)
  const windows = new Map<string, Promise<EcopyRecord[]>>(), month_of = new Set<string>();
  function window(from: string, to: string): Promise<EcopyRecord[]> {
    const id = `${from}|${to}`;
    let found = windows.get(id);
    if (!found) windows.set(id, found = json(EcopyRecords, ['ecopy', 'search', 'GATEPASS', '--from', from, '--to', to]).catch(e => { windows.delete(id); throw e; }));
    return found;
  }
  return {
    plan(sb_dates) {
      const per = new Map<string, Set<string>>();
      for (const d of sb_dates) per.set(d.slice(0, 7), (per.get(d.slice(0, 7)) ?? new Set()).add(d));
      for (const [month, set] of per) if (set.size >= MONTH_AT) month_of.add(month);
    },
    jobStatus: () => json(JobStatusRows, ['report', 'job-status']),
    myIec: () => json(MyIecRows, ['my-iec']),
    enquiry: b => json(Enquiry, ['enquiry', 'sb', '--location', b.location, '--sb-no', b.sb_no, '--sb-date', b.sb_date]),
    gatepassWindow: window,
    async gatepassSearch(b) {
      const month = b.sb_date.slice(0, 7), whole = month_of.has(month);
      return (await window(whole ? `${month}-01` : b.sb_date, whole ? month_end(month) : b.sb_date)).filter(r => r.docNumber === b.sb_no && r.location === b.location);
    },
    async gatepassDownload(record, output) {
      if (!record.irn) throw new IcegateError('gatepass record has no IRN');
      await run(['ecopy', 'download', '--irn', record.irn, ...(record.userId ? ['--user-id', record.userId] : []), '-o', output]);
    },
  };
}
