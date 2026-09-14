// Is the COMMIT free of mutations? Not the working tree — the commit.
//
// WHY THIS EXISTS, AND IT IS NOT A HYPOTHETICAL
//   A mutation run was in flight in the background. A hook reported uncommitted
//   changes, I committed, and the commit captured `src/oauth.js` with the OAuth
//   state nonce deliberately removed — a CSRF guard, stripped, in a commit whose
//   message described a lock fix. Every check I had run said green, because every
//   check I had ran against the WORKING TREE, and by then the harness had already
//   restored it. The artifact I was about to ship was the one thing nothing looked at.
//
//   The harness's own guards could not have caught this. Its lock stops a second
//   HARNESS; it says nothing about a person or a hook committing underneath it.
//
// So this checks the object that actually travels: the tree at HEAD.
//
// Run: npm run verify:committed   (before any push)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { lockPathFor, livingHolder } from './lockfile.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REPO = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: ROOT, encoding: 'utf8' }).trim();

let bad = 0;
const fail = (msg) => { console.error(msg); bad += 1; };

// 1. Is a harness running RIGHT NOW? Then no commit taken in this window can be
//    trusted, whatever it contains, because the next moment it may differ.
{
  const lock = lockPathFor(ROOT);
  if (fs.existsSync(lock)) {
    const holder = livingHolder(lock);
    if (holder > 0) {
      fail(`REFUSING — a mutation harness (pid ${holder}) is running against this checkout.\n`
        + '  Anything committed now may capture a mutation. Wait for it to finish.');
    }
  }
}

// 2. Every mutation anchor must be present in the file AS COMMITTED.
//    A missing anchor means the committed source is not the fixed source.
const src = fs.readFileSync(path.join(ROOT, 'test/mutate.mjs'), 'utf8');
const arr = src.slice(src.indexOf('const MUTATIONS = [') + 'const MUTATIONS = '.length,
  src.indexOf('\n];', src.indexOf('const MUTATIONS = [')) + 2);
// eslint-disable-next-line no-eval
const MUTATIONS = (0, eval)(arr);

const rel = path.relative(REPO, ROOT).split(path.sep).join('/');
const committed = new Map();
for (const m of MUTATIONS) {
  const gitPath = rel ? `${rel}/${m.file}` : m.file;
  if (!committed.has(gitPath)) {
    try {
      committed.set(gitPath, execFileSync('git', ['show', `HEAD:${gitPath}`], { cwd: REPO, encoding: 'utf8' }));
    } catch {
      fail(`REFUSING — ${gitPath} is not in HEAD at all.`);
      committed.set(gitPath, '');
    }
  }
  const body = committed.get(gitPath);
  // Tolerate CRLF, exactly as the harness does.
  const loose = new RegExp(m.from.split('\n')
    .map((l) => l.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\r?\\n'));
  if (!body.includes(m.from) && !loose.test(body)) {
    fail(`REFUSING — the fix for [b${m.blocker}] is NOT in the commit: ${m.name}\n  ${gitPath}`);
  }
}

if (bad) {
  console.error(`\n${bad} problem(s). The commit does not carry the source you validated.`);
  console.error('Restore the file(s), amend the commit, and run this again BEFORE pushing.\n');
  process.exit(1);
}
console.log(`committed tree is clean — ${MUTATIONS.length} anchors verified at HEAD`);
