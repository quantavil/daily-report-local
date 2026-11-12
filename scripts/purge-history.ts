// Squash every commit older than --days (default 30) on the current branch into one baseline
// commit, then force-push with lease and garbage-collect. Dry run unless --apply. No backup is kept.
// Safety: the branch must equal origin's, the squashed tree must equal the old tree, and a failed
// check or push puts the old history back before anything is pruned.
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
await git('fetch', 'origin', branch);
const head = await git('rev-parse', 'HEAD');
if (head !== await git('rev-parse', `origin/${branch}`)) throw new Error(`${branch} differs from origin/${branch}; push or pull first`);

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
const tree = await git('rev-parse', 'HEAD^{tree}');
const base = await git('commit-tree', `${lastOld}^{tree}`, '-m', `History up to ${date} squashed`);
const restore = async () => { await git('rebase', '--abort').catch(() => {}); await git('reset', '--hard', head); };
try {
  await git('rebase', '--onto', base, lastOld, branch);
  if (await git('rev-parse', 'HEAD^{tree}') !== tree) throw new Error('squashed tree differs from the original; nothing pushed');
  await $`git push --force-with-lease=${branch}:${head} origin ${branch}`.quiet();
} catch (error) { await restore(); throw error; }
await $`git reflog expire --expire=now --all`.quiet();
await $`git gc --prune=now --quiet`.quiet();
console.log('Done.');
