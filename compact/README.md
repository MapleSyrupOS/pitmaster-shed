# compact

Moves a block of text out of one file into an append-only archive, verbatim, and proves it moved
unchanged. It does the mechanics of a compaction (the move, the proof, the ordinal reading) and nothing
else: what to move and when stays your judgment. With `--verify` it also proves, after the fact and from
git, that a compaction changed nothing but the moved lines.

Python 3.8 or newer, standard library only. macOS, Linux, Windows. `--verify` (and the self-test cases
that use it) need `git` on the path; without git the rest works and those cases are skipped by name.

## Usage

```
python3 compact.py --source FILE --start LINE --end LINE --divider HEADING
                   [--archive FILE] [--root DIR] [--apply] [--expect-purity HEX]
                   [--bullet [--with FILE] [--drain]]
                   [--ordinal-pattern REGEX] [--ordinal-marker WORD]
python3 compact.py --ordinal [--archive FILE] [--root DIR] [--ordinal-pattern REGEX]
python3 compact.py --verify [BASE] --source FILE [--archive FILE] [--root DIR]
python3 compact.py --self-test
```

Without `--apply` nothing is written. The dry run prints the exact block it would move, its purity
digest, and the ordinal reading. Pass the digest back as `--expect-purity` on the real run for a second
guard.

| Option | Meaning |
|--------|---------|
| `--source FILE` | The file the block is moved out of. |
| `--archive FILE` | The append-only archive. Default: `docs/STATUS-archive.md`. It must already exist (it may be empty). |
| `--root DIR` | Relative `--source`, `--archive` and `--with` paths resolve against this directory. Default: the current directory. |
| `--start LINE`, `--end LINE` | The exact text of the first and the last line to move. Both are inclusive. A line must match exactly one line of the source. |
| `--divider HEADING` | The heading line written above the block in the archive. One non-blank line. |
| `--apply` | Write the move. |
| `--expect-purity HEX` | The digest you computed for the block (from a dry run); refuse if the selected block hashes differently. |
| `--bullet` | Move one top-level bullet (`- ` or `* ` at column 0) instead of a blank-line-bounded block. Reaches bullets that sit next to each other. |
| `--with FILE` | With `--bullet`: the text in FILE replaces the bullet in the source. A file, never a command-line string. |
| `--drain` | With `--bullet --with`: the replacement (the stub) is guaranteed to end with `` body under `<divider>`. ``, the divider quoted byte for byte; appended if your text did not already end with it. |
| `--ordinal` | Print the archive's highest ordinal and the next one. Never edits anything. |
| `--ordinal-pattern REGEX` | The heading pattern, with exactly one group: the ordinal word or number. Default: `^#+ [⚑⬇] ([A-Z-]+) COMPACTION`, with spelled-out ordinals (`FIRST` ... `NINETY-NINTH`); a group of digits counts as a number. |
| `--ordinal-marker WORD` | A divider that contains this word must match the pattern (wall 5). Default `COMPACTION`; an empty string turns the wall off. |
| `--verify [BASE]` | Prove from git that the changes since BASE only moved text into the archive. See below. Needs `--source`. |
| `--self-test` | Run the built-in cases, print `PASS`, `FAIL` or `SKIP` for each, then a verdict line. Exits non-zero on any failure. |

## What it guarantees

- The archive is append-only and verbatim. No option deletes from it or rewrites a block in it.
- The archive is written and proven before the source is touched. The block's digest (SHA-256 of its
  text, first 16 hex digits) is taken from the source and again from the archive as re-read from disk;
  only if they agree is the source edited. On a mismatch the archive is put back to the bytes it had and the
  source is left as it was.
- A refusal changes nothing.
- Each file keeps its own line endings, LF or CRLF. A file that mixes the two is refused.

## The walls

Each is a refusal that names itself. There is no override flag for any of them.

| Wall | What it stops |
|------|---------------|
| 1 | An anchor that matches no line or more than one. Anchors are content, never line numbers. |
| 2 | A block that is not blank-line bounded: a cut one line off leaves a bullet sliced mid-sentence. |
| 3 | An archive copy that does not hash like the source block. The archive is rolled back. |
| 4 | Guessing the ordinal. It is read by the pattern and printed; an ordinal word the table does not carry is reported, never invented. |
| 5 | A divider that carries the marker word but that the ordinal pattern cannot read. |
| 6 | An `--end` that opens a new entry: `--end` is inclusive, so the block would be one entry too long. A one-line entry (`--start` equal to `--end`) is exempt. |
| 7 | `--source` and `--archive` being the same file (by path, symlink or inode). |
| 8 | With `--bullet`: a block that is not exactly one top-level bullet, or that cuts a bullet off from its nested items. |
| 9 | With `--drain`: a stub that does not end with the quoted citation, or a missing `--bullet` or `--with`. |

## `--verify`

```
python3 compact.py --verify [BASE] --source STATUS.md [--archive docs/STATUS-archive.md]
```

Run it after a compaction, after the commit or before, in any repository. It compares `--source` and
`--archive` as they are now with BASE and checks that:

1. the archive is its base text plus an appended tail, and nothing earlier in the archive changed;
2. the lines the source lost are exactly the blocks of that tail, verbatim and in order (in the order they
   had in the source, so move blocks top to bottom or verify after each run);
3. every stub the source gained that cites a divider (`` body under `<divider>`. ``, the shape `--drain`
   writes) cites a divider line that the archive tail holds, byte for byte;
4. putting the lost lines back over the gained ones rebuilds the base source byte for byte.

It also refuses a hunk that removed no block text at all: a line added or a blank line deleted by hand.
It checks any compact run, not only drains; the stub check applies wherever a stub cites a divider.

BASE is a git ref, used as given. Without it, BASE is the merge base of HEAD with `origin/main`, else with
`main`, and the verdict line names the ref it used. With neither available it exits 2. Both files must be
tracked at the base. Text is compared with line endings normalised, so a checkout that adds CRs does not
fail a verification.

It catches the one thing compact cannot guarantee for itself: a hand edit made after compact ran. Its own
rebuild check (4) can only fail on something git's diff cannot see, such as a final newline removed by hand.

One limit: it reads the source's changes from git's line diff. If the stub you wrote repeats a line of the
moved block exactly, git treats that line as unchanged and `--verify` reports it as missing from the
archive. Reword the stub's first or last line.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Proven, and with `--apply` landed. With `--verify`, every check passed. With `--ordinal`, an ordinal was read. |
| 1 | Refused: a wall fired and nothing was left changed. With `--verify`, a check failed. With `--ordinal`, no ordinal found or one the table does not carry. |
| 2 | Harness error: bad arguments, an unreadable file, an invalid `--ordinal-pattern`, git missing, or (`--verify`) no base to compare against. |

## Example

```
$ python3 compact.py --source STATUS.md --start "- **alpha** first entry" --end "  over two lines." \
      --divider "## ⬇ SECOND COMPACTION -- alpha" --bullet --with stub.txt --drain --apply
compact: --drain          stub ends with: `body under `## ⬇ SECOND COMPACTION -- alpha`.`
compact: block           rows 3-4, 2 lines
compact: purity BEFORE   767a50745b3a9fcc
compact: ordinal highest FIRST  ->  next SECOND
compact: purity AFTER    767a50745b3a9fcc  -- re-taken from the archive's own bytes, IDENTICAL
compact: LANDED -- 2 lines moved verbatim, source now 8 lines
$ git commit -am "Move alpha to the archive"
$ python3 compact.py --verify HEAD~1 --source STATUS.md
compact: verify PASS -- base HEAD~1 (a7514cc): 2 line(s) in 1 block(s) moved to the archive verbatim and in order, 1 stub(s) cite their divider, the base rebuilds byte for byte
```

## Tests

`python3 compact.py --self-test` builds every fixture in a temporary directory, so there is nothing to
carry. Most cases are red arms, which prove a guard works: each wall's refusal, a corrupted archive byte,
and for `--verify` a moved line changed by one byte, a line dropped, lines out of order, a stub whose
divider is paraphrased, an archive rewritten, and a hand edit of the source. Cases that need git build a
throwaway repository and are skipped by name, and counted in the verdict line, when git is absent.

## Behaviour changes from the source

- `--drain` guarantees the stub **ends** with `` body under `<divider>`. ``. The earlier shape appended it
  when it was absent anywhere; now a citation that sits mid-prose is refused, since the stub's last words
  are the citation's.
- New `--verify [BASE]` folds in a separate drain-proof script: its generic checks only (lost lines equal
  the archive's appended tail, verbatim and in order; each stub cites its divider; the base rebuilds byte
  for byte). Its checks tied to one project's dividers and index rows are not ported.
- `--archive` defaults to `docs/STATUS-archive.md`, and relative paths resolve against the new `--root`.
- The ordinal heading pattern and the wall 5 marker word are flags (`--ordinal-pattern`,
  `--ordinal-marker`); the defaults are the earlier fixed values, except that the pattern now carries one
  group and may capture a number.
- Line endings: a CRLF file is read, matched and written back as CRLF; a file that mixes CRLF and LF is
  refused. The earlier script translated endings by platform.
- An empty archive gets the first block with no leading blank lines.
- A harness failure (unreadable file, bad option) is reported as exit 2 through the normal path, and a
  divider that spans lines or is blank is refused.
- Output that a console cannot encode is escaped instead of crashing the run.
- The self-test prints one `PASS`, `FAIL` or `SKIP` line per case and a verdict line, in the shed's shape.
  Every arm of the earlier self-test is a case, several split so each fails by name; project names,
  dates and run identifiers are gone from messages and comments.
