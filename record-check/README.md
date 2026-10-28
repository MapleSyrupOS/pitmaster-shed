# record-check

Checks the decisions tables in a meeting record: that each one has a place for where the decision came up, that
every row has a status of agreed, proposed or open, and that every row cites where it came up. It reports only and
never changes a file.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 record-check.py FILE [FILE ...]
python3 record-check.py --self-test
```

| Option | Meaning |
|--------|---------|
| `FILE` | A record to check; any number. |
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

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Green: no findings. |
| 1 | Findings (or a self-test case failed). |
| 2 | Invalid usage or unreadable input, such as a missing file or one that is not UTF-8. |

## Example

```
$ python3 record-check/record-check.py record.md
FAIL record.md:6: status "maybe" is not one of agreed, proposed, open
FAIL record.md:6: decision is not cited (empty where)
record-check: 1 file(s), 1 decisions table(s), 2 row(s), 2 finding(s)
$ echo $?
1
```

## Tests

`python3 record-check.py --self-test` writes its own throwaway records in a temporary directory, so there are no
fixtures to carry. Green cases: a talk-through record, a second table in an amendment, extra columns in another
order, bold and backticked statuses, a `where it came up` header, a capitalised header, an escaped pipe, Windows line
endings (CRLF), other tables left alone, a fenced example skipped, a `Decisions` heading with the table under a
subheading, and the script working from a lone copy of its folder. Red cases, each of which must be reported as a
finding: a status that is not one of the three, a status with words after it, an empty or placeholder `where`, no
`where` column, a header with no rows, a short row, no decisions table at all, a table only inside a fence, a
`Decisions` heading with no table, and two files where the bad one is named. Usage cases: a missing file and a file
that is not UTF-8.
