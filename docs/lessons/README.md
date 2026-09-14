# Lessons

Things this fleet learned the expensive way, written where every agent can read
them.

## Why this directory exists

We had three places to put knowledge and none of them worked for this.

- **Commit messages** are rich and completely unqueryable. Nobody greps `git log`
  to find out why `scp` is failing.
- **An agent's private memory** is private. Codex and vm-cli cannot read a word of
  claude-code-cli's, and vice versa.
- **The board** is 2,200+ append-only rows. It is a log, not a knowledge base.

That was tested rather than assumed. Three questions were put to the commit
corpus and to a compiled index, on 2026-09-12:

| Question | `grep` over 19 commit messages |
|---|---|
| Why is scp failing with `posix_spawn`? | 1 precise hit |
| Does Zoom hide its overlays from viewers? | 2 hits |
| How do I know the Zoom client is muted? | **0 hits** |

The first two were already findable. **Our problem is not retrieval, it is
capture.** The most expensive lesson of that day — that Zoom can join muted while
every audio instrument reports success — existed in no durable store at all: not
the repo, not the board, not any memory file. It was learned live and would have
been lost.

So this directory is for the lesson, at the moment it is learned, in git, where
Codex and vm-cli get it for free.

## What belongs here

A lesson, not a changelog. Write it when something cost real time, and write
what would have saved that time:

- **What was believed** and what was actually true.
- **The evidence**, with numbers. `muted = 108 red pixels, unmuted = 0` beats
  "there is a red slash".
- **How to tell**, concretely enough to act on at 2am.

What does not belong: status, plans, or anything a commit message already says
well. If it is only about one change, it belongs in that change's commit.

## The recurring shape

Nearly everything here is one failure wearing different clothes:
**the instrument measured whether the machine acted, and the failure lived one
step past where it stopped looking.** See
[checks-that-measure-the-machine.md](checks-that-measure-the-machine.md) first;
the rest are instances of it.

## Contents

| file | what it covers |
|---|---|
| [checks-that-measure-the-machine.md](checks-that-measure-the-machine.md) | The core failure mode, with four instances from one day. |
| [zoom-headless-presenter.md](zoom-headless-presenter.md) | Joining, sharing, overlays, and the muted client. |
| [audio-path.md](audio-path.md) | Virtual mic and speaker, neural voice, hearing, CPU starvation. |
| [windows-toolchain.md](windows-toolchain.md) | ssh/scp pairing, MSYS path rewriting, file modes, strict mode. |
| [x-server-and-capture.md](x-server-and-capture.md) | Screenshots, X grabs, and how one command wedged a display for 14 minutes. |
