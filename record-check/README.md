# record-check

Checks the decisions tables in a meeting record: that each one has a place for where the decision came up, that
every row has a status of agreed, proposed or open, and that every row cites where it came up. Given the talk's
numbered transcript, it also checks that each citation is true: the lines exist, the quotes are word for word, and an
agreed row has a yes. With `--shape minutes` it checks minutes instead (below). It reports only and never changes a
file.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 record-check.py FILE [FILE ...]
python3 record-check.py FILE [FILE ...] --transcript TRANSCRIPT [--quote-fillers drop] [--words WORDS]
python3 record-check.py FILE [FILE ...] --shape minutes [--transcript TRANSCRIPT ...]
python3 record-check.py --self-test
```

| Option | Meaning |
|--------|---------|
| `FILE` | A record to check; any number. |
| `--transcript TRANSCRIPT` | The talk's transcript, numbered `T1: …`, one line per turn, as [`transcript-number`](../transcript-number/README.md) writes it. Turns on the transcript checks below. |
| `--quote-fillers drop` | A quote may leave out fillers, each place marked `[…]` or `[...]`. Default `keep`: a quote keeps every word. |
| `--words WORDS` | The project's words file (below). |
| `--shape minutes` | Check minutes (below) in place of decisions tables. Default `decisions`. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## What it checks

A record is a Markdown file written by hand: a talk-through record, a townhall record, or any record kept in the same
shape. Only its decisions tables are checked; the prose around them is the writer's.

A decisions table is a Markdown table whose header row starts with a `decision` cell and has a `status` cell. Every
record holds at least one. Each table must:

- have a `where` column (any header that starts with `where`, such as `where it came up`);
- hold at least one row;
- give every row the header's number of cells;
- mark every row's status `agreed`, `proposed` or `open` (bold, italics or backticks around the word are fine; any
  other word is a finding);
- cite every row: its `where` cell is not empty and not a placeholder (`-`, `?`, `n/a`, `tbd`, `todo`, `none`).

A `Decisions` heading with no decisions table under it is a finding. Tables inside fenced code blocks are examples
and are skipped. A pipe inside a cell is written `\|`. The script does not check that a citation is true, only that
one is there.

## The transcript checks

With `--transcript`, besides the shape checks:

- every T-number the record names (`T12`, or a range `T12-T15`, `T12–T15` or `T12-15`) exists in the transcript,
  anywhere in the record outside fenced code. A tier's name (`tier T2`, and `T0` anywhere) is not a citation;
- every decisions row's `where` cell holds at least one T-number;
- every quote, in straight (`"…"`) or curly (`“…”`) double quotes, is found word for word in one line of the
  transcript (a quote is one person's words, so it never runs across two lines). Case, punctuation and spacing do
  not count; words do. `[sic]` is ignored. With `--quote-fillers drop`, a `[…]` may stand for one or more fillers
  (`uh`, `um`, `uhm`, `er`, `erm`, `ah`, `hmm`, `you know`) and nothing else, at the start, the end or in between;
  without it, a `[…]` in a quote is a finding;
- every `agreed` row has an assent word (a word only: it does not read "I do not agree" as a no, which the model's
  yes/no pass over the cited lines is for) in the lines it cites or the three lines after each: `yes`, `yeah`, `yep`,
  `agreed`, `agree`, `sounds good`, `no objection`, `+1`, `I will do it`, `I'll do it`, `I can do that`,
  `let's do that`, `works for me`, plus the words file's Assent items.

The transcript itself is never cleaned: it is the record's source, kept exactly as it came.

## Minutes (`--shape minutes`)

Minutes are for a reader who wants one pass: what was reported, who does what, what is still open. Each thing is
said once. They have four sections, in this order, and each item is one `- ` bullet (or a numbered one):

```markdown
Present: Sam, Alex.

## Reports
- **Call day.** Sam finds Thursdays work better for most of us (T3). Agreed: move the call to Thursdays (T2-T4).

## Action items
- Alex: write the release notes (T6).

## Pending
- The budget, next time (T7).
```

The script checks that:

- all four sections are there, in that order (Present may be a heading or a `Present:` line);
- there is no `Decisions`, `Corrections`, `Key points`, `Open questions` or `Topics` section: agreed items fold into
  Reports, and corrections go in a check list kept beside the minutes;
- every item in Reports, Action items and Pending cites a T-number (a lone `- None.` holds an empty section);
- every action item is written `who: what` (the who may be bold);
- no item says again what another says: two sentences sharing three quarters of their words (small words such as
  "the" and "to" aside, and a plural or "-ing" counted as its root) are a finding, at the later one;
- nothing Reports marks settled (a sentence starting `Agreed:`, `Done:` or `Decided:`) is also in Pending.

With `--transcript`, the transcript checks above run over the minutes too, and each `Agreed:` in Reports needs an
assent word in the lines its item cites or the three after each. Whether a sentence is a suggestion or an agreement,
and whose observation it is, are for the writer: the script cannot tell.

## The words file

`--words` names a Markdown file with up to three sections, one `- ` item per entry. A section's heading starts with
its name (`## Corrections (heard -> right)` is read as Corrections):

```markdown
## Corrections
- apjay.org -> apache.org
- tech, tack, tax, attack -> TAC (only in: travel or committee talk)
- ComDev

## Names
- <a name as the transcript shows it> -> <the name the person goes by>

## Assent
- aye
```

- **Corrections**: `heard -> right` (or `→`), several heard forms split by commas. An item with no arrow is a right
  spelling only, advice for whoever writes the record. A heard form still in the record is a finding, unless the
  item has an `(only in: …)` context: a tool cannot tell context, so those are advice too.
- **Names**: a name the transcript shows, and the name to use instead. A substituted name still in the record is a
  finding, and no finding prints it (a failing quote that holds one is reported without its text). The name to use
  may contain the shown one (`Pat -> Pat Example`). Keep a Names list that holds a private name (a legal name, say)
  outside the repository, or in a file git ignores.
- **Assent**: extra yes-words for this project, added to the built-in list.

Quotes are matched against the transcript as written and again after every swap the file lists, so a quote that
uses the right spelling still counts as word for word.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Green: no findings. |
| 1 | Findings (or a self-test case failed). |
| 2 | Invalid usage or unreadable input, such as a missing file or one that is not UTF-8, a transcript with no numbered lines, a Names item with no arrow, or `--words` or `--quote-fillers drop` without `--transcript`. |

## Example

```
$ python3 record-check/record-check.py record.md
FAIL record.md:6: status "maybe" is not one of agreed, proposed, open
FAIL record.md:6: decision is not cited (empty where)
record-check: 1 file(s), 1 decisions table(s), 2 row(s), 2 finding(s)
$ echo $?
1
$ python3 record-check/record-check.py record.md --transcript record-transcript.md
FAIL record.md:3: quote not found word for word in the transcript: "I think Thursdays work best"
FAIL record.md:10: agreed, but no assent word in T6-T9 (the cited lines and 3 after)
record-check: 1 file(s), 1 decisions table(s), 3 row(s), 2 finding(s)
```

## Tests

`python3 record-check.py --self-test` writes its own throwaway records in a temporary directory, so there are no
fixtures to carry. Green cases: a talk-through record, a second table in an amendment, extra columns in another
order, bold and backticked statuses, a `where it came up` header, a capitalised header, an escaped pipe, Windows line
endings (CRLF), other tables left alone, a fenced example skipped, a `Decisions` heading with the table under a
subheading, and the script working from a lone copy of its folder. Red cases, each of which must be reported as a
finding: a status that is not one of the three, a status with words after it, an empty or placeholder `where`, no
`where` column, a header with no rows, a short row, no decisions table at all, a table only inside a fence, a
`Decisions` heading with no table, and two files where the bad one is named. Transcript cases, green: a true record, a quote that keeps
its filler, a dropped filler marked `[…]` under `--quote-fillers drop`, `[sic]` and curly quotes, a words-file swap in
a quote, a heard form with an `only in` context left alone, an extra assent word, tier names that are not citations, "T6 - 9 people" read as
no range, a name whose right side holds its left side, a trailing `[…]` over a filler.
Transcript cases, red: an out-of-range T-number, an altered quote, an agreed row with no assent word, a where cell
with no T-number, a `[…]` without `--quote-fillers drop`, a `[…]` standing for a word that is no filler, a heard
spelling left in the record, a substituted name left in the record (the finding must not print it), a two-word
quote not in the transcript, a trailing `[…]` hiding words, a quote spanning two lines, an em-dash range, a heading
with words after its name, and a failing quote holding a substituted name (its text must not print). Usage
cases: a missing file, a file that is not UTF-8, an unnumbered transcript, a Names item with no arrow and a missing
transcript.
Minutes cases, green: a good set, `- None.` in an empty section, the same with its transcript and words file, Present as a heading with a bold
who and numbered items, and an action that shares a topic word with a report. Red: no Pending section, sections out
of order, a Corrections section, a Decisions section, an item with no T-number, an action not written `who: what`,
an agreed item restated as an action, the same point in Reports and Pending, a point settled in Reports and still
pending, an `Agreed:` with no assent word in its lines, and a heard spelling left in the minutes.
