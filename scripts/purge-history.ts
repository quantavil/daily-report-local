// Squash every commit older than --days (default 30) on the current branch into one baseline
// commit, then force-push with lease and garbage-collect. Dry run unless --apply. No backup is kept.
import { $ } from 'bun';

const args = process.argv.slice(2);
const apply = args.includes('--apply');
const i = args.indexOf('--days');
const days = i >= 0 ? Number(args[i + 1]) : 30;
if (!Number.isInteger(days) || days < 1) throw new Error('--days needs a positive integer');

const git = async (...a: string[]) => (await $`git ${a}`.text()).trim();
const branch = await git('rev-parse', '--abbrev-ref', 'HEAD');
if (branch === 'HEAD') throw new Error('detached HEAD');
if (await git('status', '--porcelain')) throw new Error('working tree not clean');

const cutoff = Math.floor(Date.now() / 1000) - days * 86400;
const lines = (await git('rev-list', '--first-parent', '--format=%ct', '--reverse', branch)).split('\n');
const commits: { sha: string; t: number }[] = [];
for (let k = 0; k + 1 < lines.length; k += 2) commits.push({ sha: lines[k]!.replace('commit ', ''), t: Number(lines[k + 1]) });

const keepFrom = commits.findIndex((c) => c.t >= cutoff);
if (keepFrom <= 1) { console.log(`Nothing to purge: ${keepFrom < 0 ? 'no' : 'at most one'} commit older than ${days} days.`); process.exit(0); }
const lastOld = commits[keepFrom - 1]!.sha;
console.log(`${keepFrom} commits older than ${days} days -> 1 baseline; ${commits.length - keepFrom} kept.`);
if (!apply) { console.log('Dry run. Re-run with --apply.'); process.exit(0); }

const date = new Date(commits[keepFrom - 1]!.t * 1000).toISOString().slice(0, 10);
const base = await git('commit-tree', `${lastOld}^{tree}`, '-m', `History up to ${date} squashed`);
await git('rebase', '--onto', base, lastOld, branch);
await $`git push --force-with-lease origin ${branch}`;
await $`git reflog expire --expire=now --all`.quiet();
await $`git gc --prune=now --quiet`.quiet();
console.log('Done.');
