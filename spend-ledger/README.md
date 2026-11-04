# spend-ledger

Records what an agent session actually spent, read from its own transcript on this machine, as one row of a spend
ledger, and reports spend beside what landed over a window. It also logs readings of a plan's usage meter and fits
how many tokens one point of that meter is worth. Tokens are the measurement; the dollar figure and the points
estimate are derived from them and labelled as derived.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows. Keep `prices.tsv` beside the script.

## Usage

```
python3 spend-ledger.py measure [--platform claude|codex] [--project DIR | --any-project]
                                [--session ID | --latest | --all] [--since WHEN] [--until WHEN]
                                [--meter START END] [--lead TEXT] [--landed TEXT] [--note TEXT] [--header]
python3 spend-ledger.py report  [--ledger FILE] [--since DATE] [--until DATE]
python3 spend-ledger.py reading PERCENT [--meter NAME] [--at WHEN] [--readings FILE]
python3 spend-ledger.py calibrate [--meter NAME] [--since WHEN] [--models TEXT] [--readings FILE] [--write LOG]
python3 spend-ledger.py --self-test
```

| Mode | What it does |
|------|--------------|
| `measure` | Reads session transcripts and prints one ledger row per session (`--latest`, the default; one `--session`; or `--all` with activity in the window). `--meter START END` gives two readings of the usage meter, in points, when the transcript does not carry them. `--lead`, `--landed` and `--note` fill the row's own cells. |
| `report` | Reads the ledger (default `docs/spend-ledger.md`) and prints spend beside what landed, merged, opened and committed counted apart, over a window. |
| `reading` | Appends one reading of a usage meter (its percent used) to the readings file. |
| `calibrate` | Pairs consecutive readings of one meter with the tokens every Claude Code session on this machine spent between them, and fits K, tokens per point. `--write LOG` appends the pairs to a calibration log, under its `Fitted K` heading. |
| `--self-test` | Runs the built-in cases on fixture transcripts and a fixture ledger, prints `ok` or `FAIL` for each, then a verdict line. |

WHEN is a date (`YYYY-MM-DD`, the day included whole) or an ISO time (`2026-10-20T18:00:00Z`, the instant
included); a time without an offset is UTC.

## What it reads

| Platform | Transcripts |
|----------|-------------|
| `claude` | Claude Code: `~/.claude/projects/<project>/<session>.jsonl`, plus `<session>/subagents/*.jsonl` for the workers it spawned. Each assistant message id counts once, at its largest output count, so streamed repeats and record order do not matter. |
| `codex` | Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. A window's spend is the last running total in it less the last one before it. Their rate-limit records carry the meter's reading, so its start and end need no asking. |

Any other platform has no transcript this tool can read: write its row by hand.

## The derived figures

- **list $**: a list-price proxy from `prices.tsv` (dated in its header; a model with no row shows `-`). It puts
  models on one scale. It is not a bill and it is not the plan's meter. Edit `prices.tsv` when prices change.
- **est pts**: this session's share of the meter's movement, weighted by list price, when other sessions on the
  same platform overlapped its window. Alone on the meter, the movement is the session's own.

## The readings file

`$PITMASTER_HOME/usage-readings.tsv` (default `~/.pitmaster/usage-readings.tsv`), one line per reading: its UTC
time, the meter's name and the percent. It is per machine and never belongs in a repository. A reading lower than
the one before it is a reset, never a pair. A pair whose meter moved under 2 points, or whose K is under half the
median (the meter moved for spending this machine cannot see, such as a phone or the web), is flagged and left out
of the fit.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Done. |
| 1 | A self-test case failed. |
| 2 | Invalid usage, or no ledger at the path given to `report`. |

## Limits

- It reads only this machine's transcripts. Spending on another machine, a phone or the web moves the meter
  without leaving a transcript here.
- The ledger holds numbers and links only, never prompt text or file contents.
- The prices are list prices on the date `prices.tsv` names; they go stale.

## Tests

`python3 spend-ledger.py --self-test` writes its own fixture transcripts for both platforms, a fixture ledger and a
fixture readings file in a temporary directory, so there are no fixtures to carry. It covers counting each message
id once, subagent transcripts, the Codex running total, the row's columns in order, the meter share by list-price
weight, the report's window and its split of what landed, a reset in the readings, K as tokens per point, and a
move under 2 points left out of the fit. It has no `--break` switch; each case names what it checks.

## Source

The source is `skills/spend-ledger/scripts/spend-ledger.py` in the pitmaster plugin, which now carries a pinned
copy of this file and of `prices.tsv`. Its docstring still points at the skill's `SKILL.md` for platforms it
cannot read. Changes made here, and copied back byte for byte:

- `--project` compares folders after `abspath` and `normcase` on both sides, so on Windows a Codex session's
  recorded folder matches the folder asked for (the Codex self-test cases failed there before).
