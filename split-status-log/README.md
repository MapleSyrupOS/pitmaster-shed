# split-status-log

Keeps a newest-first, dated log file small by splitting it by month, verbatim. The current month stays in the
log; every older month moves to its own file. Nothing is reworded and nothing is reordered: the log (with its
pointer block stripped) followed by the month files, newest first, is the original file byte for byte.

Python 3.8 or newer, standard library only. macOS, Linux, Windows.

## Usage

```
python3 split-status-log.py [--log FILE] [--archive-dir DIR] [--date YYYY-MM-DD] [--apply | --verify]
python3 split-status-log.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--log FILE` | The log to split. Default: `docs/STATUS-log.md`, relative to the current directory. |
| `--archive-dir DIR` | Where the `YYYY-MM.md` files go. Default: the log's path with `.md` removed, beside it (`docs/STATUS-log.md` gives `docs/STATUS-log/`). Required when the log has no extension. |
| `--date YYYY-MM-DD` | The date written into the pointer block. Default: today, from the system clock. |
| `--apply` | Write the split. Without it the run is a dry run that prints the plan and writes nothing. |
| `--verify` | Check the split on disk and write nothing. Cannot be combined with `--apply`. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## The log it understands

A log sectioned by headings that start `## YYYY-MM-DD`, newest first, with each month's headings in one
contiguous run. Anything before the first dated heading is the preamble and stays at the top of the log. A
line inside a fenced code block that looks like a dated heading is taken for a heading.

After `--apply`, the log holds a pointer block (between `<!-- STATUS-LOG-POINTER:BEGIN -->` and
`<!-- STATUS-LOG-POINTER:END -->`) that names every archived month, then the preamble, then the current month.
Each older month is in `<archive-dir>/YYYY-MM.md`: its sections only, in their existing order. Only files named
`YYYY-MM.md` count as archived months; any other file in the archive directory is ignored.

Re-running is safe. On a split log with new entries at the top, the pointer block is stripped and written
again and the content does not change. When the first heading names a new month, the month just finished is
archived and the new one becomes current. Entries for an already-archived month found in the log below the
current month are put in front of that month's file (unless already a prefix of it), so a second run changes
nothing. A run that finds nothing to change writes no file. Month files are written first and the log last, so
a run that dies half way is finished by running it again.

Files are read and written as bytes (UTF-8, with undecodable bytes preserved), and lines end where `\n` ends
them, so CRLF logs and logs with stray bytes round-trip exactly. The pointer block itself uses `\n`.

## Refusals

The tool refuses, with exit 1 and nothing written, when:

- the log has no `## YYYY-MM-DD` heading;
- a month's headings are not one contiguous run;
- the months are not newest-first (an oldest-first log would archive the wrong months);
- a pointer block has a begin marker with no end marker, or the reverse;
- the archive directory path is an existing file.

`--verify` fails (exit 1) when the concatenation breaks the contiguity or the order, when a month file has text
before its first heading or holds another month's headings, or when a month is both in the log and archived.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | A clean plan, apply or verify; or the self-test passed. |
| 1 | A refusal or a failed verify; or the self-test failed. |
| 2 | A usage error, or a harness error (the log cannot be read, a file cannot be written). |

## Example

```
$ python3 split-status-log/split-status-log.py
split-status-log: log            docs/STATUS-log.md
split-status-log: archive dir    docs/STATUS-log
split-status-log: current month kept in place; 2 older month(s): 2026-08, 2026-07
split-status-log: new log        412 line(s)  (write)
split-status-log:   docs/STATUS-log/2026-08.md  1830 line(s)  (write)
split-status-log:   docs/STATUS-log/2026-07.md  2204 line(s)  (write)
split-status-log: DRY RUN -- nothing written. Re-run with --apply.
$ python3 split-status-log/split-status-log.py --apply
split-status-log: LANDED -- current month kept in docs/STATUS-log.md, 2 older month(s) archived under docs/STATUS-log, what was written re-read and checked.
$ python3 split-status-log/split-status-log.py --verify
split-status-log: VERIFY OK -- concatenation is 4446 line(s), months contiguous and newest-first, every month file holds only its own month.
```

## Tests

`python3 split-status-log.py --self-test` builds its own logs in a temporary directory, so there are no fixtures
to carry. Many cases are red arms, which prove a guard works: each refusal must exit 1 and leave every file
untouched; `--verify` must fail on a foreign month, a month in two places, misordered months and stray text;
a crashed earlier run must be finished without duplicating a month; and the log must be written after the
month files.

## Behaviour changes from the source

- The paths are flags. The source worked only on `docs/STATUS-log.md` and `docs/STATUS-log/` under the
  repository the script sat in. Now `--log` defaults to the same `docs/STATUS-log.md`, but relative to the
  current directory rather than to the script's location, and `--archive-dir` defaults to the log's path
  without `.md`, which gives the source's `docs/STATUS-log/`. Both can be changed with a flag.
- The pointer block's text no longer names the original project's script, rules or file layout; it says where
  the month files are, relative to the log. The markers are unchanged, so a log split by the source is still
  recognised.
- An oldest-first log is refused. The source took the first heading's month as the current one without checking.
- A pointer block with only one of its two markers is refused. The source left it in the text.
- Only `YYYY-MM.md` files in the archive directory count as months. The source counted every `*.md` file there.
- Lines are split on `\n` only (the source used `splitlines`, which also splits on form feeds and similar
  characters), and files are read and written as bytes instead of decoded with replacement, so no byte of the
  log is altered; CRLF logs keep their line endings.
- The plan is checked before anything is written, and a refusal writes nothing. The source checked the
  reconstruction after writing. After writing, the files are read back and compared with the plan.
- Month files are written first and the log last, files already holding the right text are not rewritten,
  and writes go through a temporary file and a rename.
- `--verify` also checks that each month file holds only its own month, starts at a dated heading, and that no
  month is in both the log and the archive, and that the months are newest-first. It reports every problem, not only the first.
- `--date` is checked for the form `YYYY-MM-DD`. A missing log, or a file that cannot be written, exits 2 in
  every mode (the source's `--verify` printed a traceback).
- `--apply` and `--verify` together are a usage error.
- The source's self-test swapped module globals to point at a fixture; this one passes the paths in, and has
  new cases for the command line, every refusal, CRLF and non-UTF-8 input, the stray-file rule, and the flags.
