import { describe, expect, test } from 'bun:test';
import { chmod, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { command_client, IcegateError, NotReady } from '../../src/ts/icegate/client';

/** A fake `icegate`: prints the given stdout, or fails with stderr, and records its arguments next to itself. */
async function fake(body: string) {
  const dir = await mkdtemp(join(tmpdir(), 'fake-icegate-'));
  const bin = join(dir, 'icegate');
  await writeFile(bin, `#!/bin/sh\necho "$@" > "${dir}/args"\n${body}\n`);
  await chmod(bin, 0o700);
  return { client: command_client({ ICEGATE_BIN: bin, ICEGATE_DIR: dir }), args: async () => (await Bun.file(join(dir, 'args')).text()).trim() };
}

describe('icegate client', () => {
  test('job status rows are validated and returned', async () => {
    const { client, args } = await fake(`echo '[{"SB No":"6598990","IEC":"2915003131"}]'`);
    expect(await client.jobStatus()).toEqual([{ 'SB No': '6598990', IEC: '2915003131' }]);
    expect(await args()).toBe('report job-status --json --no-journal');
  });
  test('enquiry passes the bill identity and tolerates absent tabs', async () => {
    const { client, args } = await fake(`echo '{"currentStatusModel":[{"leoDate":"07 SEP 2026 00:09"}]}'`);
    const data = await client.enquiry({ location: 'INMBD6', sb_no: '6598990', sb_date: '2026-09-05' });
    expect(data.currentStatusModel[0]!.leoDate).toBe('07 SEP 2026 00:09');
    expect(data.egmStatusModel).toEqual([]);
    expect(await args()).toBe('enquiry sb --location INMBD6 --sb-no 6598990 --sb-date 2026-09-05 --json --no-journal');
  });
  test('gatepass search is the SB date alone and keeps only this bill', async () => {
    const { client, args } = await fake(`echo '[{"jobNumber":"743","docNumber":"7322475","location":"INMBD6","irn":"a"},{"jobNumber":"9","docNumber":"7322475","location":"INDER6","irn":"b"},{"docNumber":"1","location":"INMBD6","irn":"c"}]'`);
    const found = await client.gatepassSearch({ location: 'INMBD6', sb_no: '7322475', sb_date: '2026-09-30' });
    expect(found.map(r => r.irn)).toEqual(['a']);
    expect(await args()).toBe('ecopy search GATEPASS --from 2026-09-30 --to 2026-09-30 --json --no-journal');
  });
  test('a planned batch searches whole months once; sparse months stay per day', async () => {
    const { client, args } = await fake(`echo '[{"docNumber":"1","location":"INMBD6","irn":"a"}]'`);
    client.plan(['2026-03-02', '2026-03-03', '2026-03-04', '2026-03-05', '2026-03-06', '2026-04-01']);
    expect((await client.gatepassSearch({ location: 'INMBD6', sb_no: '1', sb_date: '2026-03-05' })).length).toBe(1);
    expect(await args()).toBe('ecopy search GATEPASS --from 2026-03-01 --to 2026-03-31 --json --no-journal');
    await client.gatepassSearch({ location: 'INMBD6', sb_no: '1', sb_date: '2026-04-01' });
    expect(await args()).toBe('ecopy search GATEPASS --from 2026-04-01 --to 2026-04-01 --json --no-journal');
  });
  test('a gatepass window returns every record of the range, other ports included', async () => {
    const { client, args } = await fake(`echo '[{"docNumber":"1","location":"INMBD6"},{"docNumber":"2","location":"INHPI6"}]'`);
    expect((await client.gatepassWindow('2026-09-01', '2026-09-30')).map(r => r.docNumber)).toEqual(['1', '2']);
    expect(await args()).toBe('ecopy search GATEPASS --from 2026-09-01 --to 2026-09-30 --json --no-journal');
  });
  test('an unexpected shape is an error, not a guess', async () => {
    const { client } = await fake(`echo '{"currentStatusModel":"oops"}'`);
    await expect(client.enquiry({ location: 'INMBD6', sb_no: '6598990', sb_date: '2026-09-05' })).rejects.toThrow('unexpected shape');
  });
  test('non-JSON output and failures are IcegateErrors with the last stderr line', async () => {
    await expect((await fake('echo hello')).client.jobStatus()).rejects.toThrow('printed no JSON');
    await expect((await fake('echo "boom" >&2; exit 2')).client.jobStatus()).rejects.toThrow(IcegateError);
  });
  test('a 409 retry message becomes NotReady', async () => {
    const { client } = await fake('echo "error: e-copy document is being retrieved by the portal; retry in about 60 minutes" >&2; exit 1');
    await expect(client.gatepassDownload({ irn: 'x' }, '/tmp/x.pdf')).rejects.toBeInstanceOf(NotReady);
  });
  test('download needs an IRN and sends the user id when present', async () => {
    const { client, args } = await fake('true');
    await expect(client.gatepassDownload({}, '/tmp/x.pdf')).rejects.toThrow('no IRN');
    await client.gatepassDownload({ irn: 'abc', userId: 'u1' }, '/tmp/x.pdf');
    expect(await args()).toBe('ecopy download --irn abc --user-id u1 -o /tmp/x.pdf --no-journal');
  });
});
