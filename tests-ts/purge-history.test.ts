import { describe, expect, test } from 'bun:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const SCRIPT = join(import.meta.dir, '..', 'scripts', 'purge-history.ts');
const DAY = 86400;

const sh = (cwd: string, ...a: string[]) => {
  const r = Bun.spawnSync(['git', ...a], { cwd, env: { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null' } });
  if (r.exitCode !== 0) throw new Error(`git ${a.join(' ')}: ${r.stderr.toString()}`);
  return r.stdout.toString().trim();
};

// A clone with one commit per entry of `ages` (days old, oldest first), pushed to a bare remote.
function repo(ages: number[]) {
  const root = mkdtempSync(join(tmpdir(), 'purge-'));
  const remote = join(root, 'remote.git'), work = join(root, 'work');
  sh(root, 'init', '-q', '--bare', '-b', 'main', remote);
  sh(root, 'clone', '-q', remote, work);
  sh(work, 'config', 'user.email', 't@example.com');
  sh(work, 'config', 'user.name', 't');
  ages.forEach((age, i) => {
    writeFileSync(join(work, 'f.txt'), `v${i}`);
    sh(work, 'add', '.');
    const date = `${Math.floor(Date.now() / 1000) - age * DAY} +0000`;
    Bun.spawnSync(['git', 'commit', '-q', '-m', `c${i}`, '--date', date], { cwd: work, env: { ...process.env, GIT_COMMITTER_DATE: date, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null' } });
  });
  sh(work, 'push', '-q', 'origin', 'main');
  return { work, remote };
}

const purge = (cwd: string, ...a: string[]) => {
  const r = Bun.spawnSync(['bun', SCRIPT, ...a], { cwd });
  return { code: r.exitCode, out: r.stdout.toString() + r.stderr.toString() };
};
const count = (cwd: string, ref = 'HEAD') => Number(sh(cwd, 'rev-list', '--count', ref));

describe('purge-history', () => {
  test('squashes old commits into one, keeps the files and the recent commits, and pushes', () => {
    const { work, remote } = repo([40, 35, 20, 3, 1]);
    const tree = sh(work, 'rev-parse', 'HEAD^{tree}');
    const r = purge(work, '--days', '7', '--apply');
    expect(r.code).toBe(0);
    expect(count(work)).toBe(3);
    expect(sh(work, 'rev-parse', 'HEAD^{tree}')).toBe(tree);
    expect(sh(remote, 'rev-parse', 'main')).toBe(sh(work, 'rev-parse', 'HEAD'));
    const subjects = sh(work, 'log', '--format=%s').split('\n');
    expect(subjects.slice(0, 2)).toEqual(['c4', 'c3']);
    expect(subjects[2]).toMatch(/squashed$/);
  });

  test('a dry run changes nothing', () => {
    const { work, remote } = repo([40, 35, 20, 3]);
    const before = sh(work, 'rev-parse', 'HEAD');
    const r = purge(work, '--days', '7');
    expect(r.code).toBe(0);
    expect(r.out).toContain('Dry run');
    expect(sh(work, 'rev-parse', 'HEAD')).toBe(before);
    expect(sh(remote, 'rev-parse', 'main')).toBe(before);
  });

  test('nothing is purged when at most one commit is old', () => {
    const { work, remote } = repo([40, 3, 1]);
    const before = sh(work, 'rev-parse', 'HEAD');
    expect(purge(work, '--days', '7', '--apply').out).toContain('Nothing to purge');
    expect(sh(remote, 'rev-parse', 'main')).toBe(before);
  });

  test('refuses when the branch is not the same as origin, and leaves both alone', () => {
    const { work, remote } = repo([40, 35, 20, 3]);
    writeFileSync(join(work, 'f.txt'), 'unpushed');
    sh(work, 'commit', '-qam', 'local only');
    const local = sh(work, 'rev-parse', 'HEAD'), pushed = sh(remote, 'rev-parse', 'main');
    const r = purge(work, '--days', '7', '--apply');
    expect(r.code).not.toBe(0);
    expect(r.out).toContain('differs from origin');
    expect(sh(work, 'rev-parse', 'HEAD')).toBe(local);
    expect(sh(remote, 'rev-parse', 'main')).toBe(pushed);
  });

  test('refuses on a dirty working tree', () => {
    const { work } = repo([40, 35, 20, 3]);
    writeFileSync(join(work, 'f.txt'), 'edited');
    expect(purge(work, '--days', '7', '--apply').out).toContain('not clean');
  });

  test('a rejected push puts the old history back', () => {
    const { work, remote } = repo([40, 35, 20, 3]);
    writeFileSync(join(remote, 'hooks', 'pre-receive'), '#!/bin/sh\nexit 1\n', { mode: 0o755 });
    const before = sh(work, 'rev-parse', 'HEAD');
    const r = purge(work, '--days', '7', '--apply');
    expect(r.code).not.toBe(0);
    expect(sh(work, 'rev-parse', 'HEAD')).toBe(before);
    expect(count(work)).toBe(4);
    expect(sh(remote, 'rev-parse', 'main')).toBe(before);
  });
});
