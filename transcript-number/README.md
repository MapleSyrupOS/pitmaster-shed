# transcript-number

Numbers a talk's transcript T1, T2…, one line per turn, so a record of the talk can cite the line each decision came
from. It also checks a numbered transcript, and writes a quote-only record: the lines that carry a decision word, each
quoted and cited, every status open, for when no model is at hand to write a real record. It never changes its input.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 transcript-number.py FILE [-o OUT]
python3 transcript-number.py FILE --quote-only [--numbered] [--source TEXT] [--by NAME] [-o OUT]
python3 transcript-number.py --check FILE
python3 transcript-number.py --self-test
```

| Option | Meaning |
|--------|---------|
| `FILE` | A plain-text transcript, UTF-8: one turn per line, or `Speaker: text` lines, as a meeting tool exports it or as pasted. `-` reads standard input. |
| `-o OUT` | Write to `OUT` instead of the screen. It refuses to write over `FILE`. |
| `--quote-only` | Write the quote-only record instead of the numbered transcript. |
| `--numbered` | With `--quote-only`: `FILE` is a transcript this tool already numbered. |
| `--source TEXT` | With `--quote-only`: the source the header names (the original file, or a link), instead of `FILE`'s name. |
| `--by NAME` | With `--quote-only`: who ran it, for the header. |
| `--check` | Check a numbered transcript: T1, T2… with none skipped, none doubled, none out of order, and no unnumbered line between them. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## What it writes

Every line that is not blank becomes `T<n>: <the line>`, its text kept exactly; only the spaces at its end are
dropped. Blank lines are dropped and never numbered. The numbered lines are written a blank line apart, so they read
as separate lines in Markdown too. A transcript that is already numbered is refused: check it with `--check`.

Lines end only at a line break (`\n`, `\r\n` or `\r`); a Unicode line separator or a form feed stays inside its line.

The quote-only record (pitmaster ADR-032's tier T0) has a header saying no model wrote it, with the date, the source
and who ran it, then one decisions table:
every line carrying a decision word (`decide`, `decision`, `agree`, `vote`, `motion`, `action`, `will`, `todo`,
`follow up`, `propose`, and their forms) is a row quoting the line, with status `open` and its T-number. With no such
line it writes one open row citing the whole transcript. The record passes
[`record-check`](../record-check/README.md), with or without `--transcript`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Green: written, or `--check` found nothing. |
| 1 | `--check` findings (or a self-test case failed). |
| 2 | Invalid usage or unreadable input: a missing or empty file, one that is not UTF-8, an already numbered transcript given to number again, or `-o` naming the input. |

## Example

```
$ python3 transcript-number/transcript-number.py huddle.txt -o huddle-transcript.md
$ python3 transcript-number/transcript-number.py --check huddle-transcript.md
transcript-number: huddle-transcript.md, 0 finding(s)
$ python3 transcript-number/transcript-number.py huddle-transcript.md --quote-only --numbered -o huddle.md
$ python3 record-check/record-check.py huddle.md --transcript huddle-transcript.md
record-check: 1 file(s), 1 decisions table(s), 4 row(s), 0 finding(s)
```

## Using it with a local model

A chat app that runs a model on your own machine (LM Studio, for example) cannot run this tool. Number the transcript
here, paste the numbered text into the chat with the record-writing prompt (paste it rather than attach it: an
attached file may be read only in parts), save the reply as the record, and check it with `record-check
--transcript`.

## Tests

`python3 transcript-number.py --self-test` writes its own throwaway files in a temporary directory. Green cases:
numbering with text kept and trailing spaces dropped, a byte-order mark and CRLF line endings, `-o` leaving the input
alone, `--check` passing the tool's own output, the quote-only record (decision-word lines only, open, cited, a pipe
escaped), `--quote-only --numbered`, a transcript with no decision word, the header's date, source and runner,
and a Unicode line separator kept inside its line, plus working from a lone copy of its
folder. Red cases, each of which must be a finding: a skipped T-number, a doubled one, and an unnumbered line between
numbered ones. Usage cases: a missing file, an already numbered transcript, an empty one, and `-o` onto the input.
