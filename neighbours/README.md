# neighbours

Reads the neighbours doc, the allow list of models other than the one you are talking to that a skill may call, checks
that it is sound, and says which of them answer right now. It reports only and never changes a file.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 neighbours.py [--root DIR] [--doc PATH] [--only NAMES] [--no-probe] [--timeout S] [--json]
python3 neighbours.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project root holding `pitmaster.md`. Default: the current directory. |
| `--doc PATH` | The neighbours doc to read. Wins over `pitmaster.md` and the default. |
| `--only NAMES` | Comma-separated names to narrow the list to. It never adds a name: one the doc does not list is a finding. |
| `--no-probe` | Do not check who answers. |
| `--timeout S` | Seconds to wait for each answer. Default: 1. |
| `--json` | Print the report as JSON. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## Where the doc is found

In this order: `--doc`; the `neighbours:` line of `pitmaster.md` in `--root`, when that line names a path; then
`~/.pitmaster/neighbours.md`. A `neighbours:` line that names neighbours instead (`neighbours: s35, codex`) narrows
the list to those names. A value is a path when it starts with `~`, holds a `/` or `\`, or ends in `.md`.

## The doc

One table whose header starts with a `name` cell and has a `how to reach it` cell. The `family`, `cost`, `streams`
and `live` columns are optional, in any order. When present, `cost` is `free`, `plan` or `paid`, and `streams` and
`live` are `yes` or `no`.

A `Seats: a, b` line names the default line-up; each seat must be on the list. A `## Live` section may set
`delivery:` (`all at once`, `streamed` or `hybrid`; `hybrid` when unset) and `filibuster:` (free text; `1 sentence`
when unset). HTML comments and fenced code blocks are skipped.

## What it reports

For each neighbour: name, family, cost, streams, live, whether it holds a seat, and who answers now. Then the seats,
the live settings and a count line.

"Who answers" is a network check, made only where the `how to reach it` cell gives an address: a URL
(`http://host:port`) or `port N` (this machine). It opens a TCP connection and closes it, which proves something
listens there, not that a model is loaded. A neighbour reached only through an agent's tool (an MCP server, a lane) is
reported as not checked.

Findings: no doc found, no table, a table with the wrong header, a name listed twice, a row with no way to reach it, a
row short of a cell, a `cost`, `streams`, `live` or `delivery` value outside its allowed words, a seat not on the
list, a name in `--only` or `pitmaster.md` that the doc does not list.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | The list was read and is sound. Some neighbours may not answer. |
| 1 | No doc, or findings in it (or a self-test case failed). |
| 2 | Invalid usage or unreadable input. |

## Example

```
$ python3 neighbours/neighbours.py --doc neighbours.md
doc: neighbours.md (from --doc)
  name   family  cost  streams  live  seat  now
  s35    qwen    free  yes      yes   yes   no answer (localhost:8080)
  codex  openai  plan  no       no    yes   not checked (reached through an agent's tool)
Seats: s35, codex
Live: delivery hybrid, filibuster 1 sentence
neighbours: 2 neighbour(s), 0 answer, 1 silent, 1 not checked, 0 finding(s)
```

## Tests

`python3 neighbours.py --self-test` builds its own throwaway documents in a temporary directory, so there are no
fixtures to carry. Green cases: the table, seats and live settings are read; probes report answers, not checked and no
answer; `pitmaster.md` narrows the list or points to another doc; `--doc` wins; a fenced `neighbours:` line is
ignored; optional columns may be left out or reordered; Windows line endings (CRLF), a bold `Seats` line and an IPv6
URL read the same; and the script works from a lone copy of its folder. Red cases, each of which must be reported as a
finding: no doc, a `cost`, `streams`, `live` or `delivery` outside its allowed words, a name listed twice, a row with
no way to reach it, a short row, a seat not on the list, no table, a table only inside a fence, a table with the
wrong header, a name `pitmaster.md` or `--only` adds.
