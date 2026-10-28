# neighbour-check

Reads each neighbour on the neighbours doc's allow list for what limits it (a usage meter, a quota, its reset and how
fast it is being spent, where one can be read) and whether it is busy, and writes a `## Neighbours` block. It reads
only, except `record`, which appends one Claude account's usage reading to a file the other accounts' runs read.

Python 3.8 or newer, standard library only. Runs on macOS, Linux, Windows.

## Usage

```
python3 neighbour-check.py [--root DIR] [--doc PATH] [--only NAMES] [--config FILE] [--burn show|hide] [--json]
python3 neighbour-check.py record --weekly PCT --weekly-reset ISO [--five PCT --five-reset ISO] [--session TEXT]
                           [--root DIR] [--doc PATH] [--config FILE]
python3 neighbour-check.py --self-test
```

| Option | Meaning |
|--------|---------|
| `--root DIR` | The project root holding `pitmaster.md`. Default: the current directory. |
| `--doc PATH` | The neighbours doc to read. Wins over `pitmaster.md` and the default. |
| `--only NAMES` | Comma-separated names to narrow the list to. It never adds a name: one the doc does not list is a finding. |
| `--config FILE` | A settings file, or a `SKILL.md` whose `## Config` block holds the settings. |
| `--burn show\|hide` | Show or hide the burn numbers. Wins over every setting. `hide` still uses burn for the verdicts. |
| `--json` | Print the same readings as JSON. |
| `record` | Append a Claude account's reading (see below). Takes `--weekly`, `--weekly-reset`, and optionally `--five`, `--five-reset`, `--session`. |
| `--self-test` | Run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## Where the list comes from

The neighbours doc, found as `neighbours` finds it: `--doc`; the `neighbours:` line of `pitmaster.md` in `--root` when
that line names a path; then `~/.pitmaster/neighbours.md`. A `neighbours: a, b` line that names neighbours instead
narrows the list, then `--only` narrows it further. The doc's findings (a name listed twice, a row with no way to
reach it, and the rest) are printed on a `Findings:` line and make the exit code 1. This tool and `neighbours` carry
the same reader as a shared block, checked by `scripts/check-twins.py`.

## Settings

`key: value` lines; a trailing ` # comment` on a value is dropped. Later wins:

1. the built-in defaults;
2. `--config FILE`: the key lines of the first fenced block under its `## Config` heading (a skill's `SKILL.md` carries
   its defaults this way); a file with no such heading is read whole, outside fences, after any leading `---` front
   matter;
3. the neighbours doc's own `## neighbour-check` section, from that heading to the next heading of the same or a
   higher level (fences and HTML comments skipped). The doc is machine-wide, so this is the machine-wide place;
4. `<root>/pitmaster.md`, key lines outside fences. Keys this tool does not know are ignored there, since other tools
   share the file;
5. the flags (`--burn`).

An unknown key in 2 or 3 is a note in the output, never a failure.

| Key | Default | Meaning |
|-----|---------|---------|
| `name` | `the user` | Who the Ask line names: `name: Alex` gives `Ask Alex:`. |
| `burn` | `show` | `show` or `hide`. |
| `probes` | `codex, agy, go, claude, ollama, openai, lanes, guard` | The kinds that run. A kind not named is off. |
| `kind <neighbour>` | (guessed) | One or more kinds for that neighbour, replacing the guess. `none` checks nothing for it. |
| `guard <neighbour>` | | `<ssh destination> <command…>`: a read-only busy check, run as `ssh -o BatchMode=yes -o ConnectTimeout=8 <destination> <command>` (25 s limit, stdin closed). Gives the neighbour the `guard` kind. |
| `meters` | `~/.claude/meters.tsv` (env `METERS_TSV`) | The readings file. |
| `now` | `~/.claude/now.tsv` (env `NOW_TSV`) | The machine-wide signal of open sessions; columns are start, end, project, branch, account, session. |
| `codex_home` | `~/.codex` (env `CODEX_HOME`) | Codex's folder; rollouts are `<codex_home>/sessions/**/rollout-*.jsonl`. |
| `codex_trees` | (off) | A folder of dispatch job folders; one is open while it holds a `started` file and no `finished` file. |
| `agy_trees` | (off) | A folder of agy dispatch clones; one touched in the last 30 minutes counts as at work. |
| `opencode` | (`opencode` on PATH, else `~/.opencode/bin/opencode`) | The OpenCode binary. |
| `go_limits` | `12, 30, 60` | OpenCode Go's dollars per 5 h, per week, per month. Its week resets Monday 00:00 UTC. |
| `lane_status` | `lanes/queue/STATUS.json` | The lane queue's status file, relative to the project's main checkout (the parent of `git rev-parse --git-common-dir`, else `--root`). |
| `lane_holds` | (off) | An extra folder of machine-wide `HOLD*` files; a relative path is read from the main checkout. `<main checkout>/lanes/HOLD*` are always read when a lanes probe runs. |
| `lane_switch` | (off) | A file whose first line starting `LANES:` is the lanes switch (a relative path is read from the main checkout); a value ending `OFF` makes every lanes neighbour busy. |

An environment variable beats the default but not a setting. A `kind` or `guard` key naming a neighbour the list does
not hold is a note and probes nothing: the doc is the allow list. `kind x: none` wins over a `guard x` setting.

## Kinds, and how they are guessed

Unless a `kind` setting names it, the guess reads each row's name, `how to reach it` cell and family, case-insensitively.
A neighbour may get several kinds.

| Kind | Guessed when | What the probe reads |
|------|--------------|----------------------|
| `codex` | the word codex is in the name or reach | Codex rollout logs: the newest window's usage, its gain over 24 h and 3 h, the pace to its reset, `codex exec` jobs, recent sessions by project. |
| `agy` | the word agy or antigravity is in the name or reach, or the name is gemini | agy processes and clones. Its quota cannot be read here, so the verdict is unknown and the Ask line names the Antigravity app. |
| `go` | opencode is in the name or reach | `opencode stats` for raw spend against `go_limits`; `opencode run` jobs. |
| `claude` | the family is Claude or Anthropic | The other Claude accounts' readings in `meters` and open sessions in `now`. |
| `ollama` | ollama in the reach, or port 11434 | `GET /api/tags` and `/api/ps`; whether a `model X` the reach names is there. |
| `openai` | "LM Studio" or "openai" in the reach, or an address that is not Ollama's | `GET /v1/models`. |
| `lanes` | the word lanes is in the reach | The lane queue's `STATUS.json`: this lane's state, the switch, the daemon, the holds. Idle or free is free; any other state is busy, except a state the queue marks unknown (a remote box that answers for itself), which gives no verdict, so its guard decides. |
| `guard` | a `guard <name>` setting exists | The guard command's `VERDICT: X` line, or its exit code. |

An address is a URL, `port N` (this machine), or a bare `host:port` such as `box-a:1236`. LM Studio with no address is
`127.0.0.1:1234`; Ollama with no address is `127.0.0.1:11434`. A neighbour with no kind, or whose kinds are all off in
`probes`, goes on one `Not checked:` line with the reason.

A Claude neighbour whose reach cell names another account's label stands for that account only; otherwise it stands
for every other account. This account's label is `CLAUDE_ACCOUNT_LABEL`, else the basename of `CLAUDE_CONFIG_DIR`
without a leading dot, else `acct-` and the first four characters of `CLAUDE_CODE_ACCOUNT_UUID`, else `default`. An
account is named by the weekday its weekly limit resets ("the Sat account").

## What it reports

```
## Neighbours
Measured YYYY-MM-DD HH:MM UTC by neighbour-check from <doc path> (verdicts PROVISIONAL: ...). Burn: shown.
<name> (<kinds>): <VERDICT>. <what limits it>. Burn: <how fast>. Busy: <what it is doing>.
Lanes (this project): ...          only when a lanes probe ran and the queue exists; printed once
Not checked: <name> (why); ...     only when any
Notes: ...                         unknown settings and the doc's notes
Findings: ...                      the doc's findings
Ask <name>: <neighbour>: <the reading to give>; ...   (or "nothing")
```

A neighbour's verdict is the worst of its facts', worst first: `closed`, `tight`, `busy`, `down`, `unknown`, `on pace`,
`free`, `up`, `spare`. A fact with no verdict (a lanes neighbour when the project has no lane queue) does not change
it. For a usage meter, 95 % used is `closed`, a pace that reaches 100 % by the window's reset is `tight`, and under
80 % projected is `spare`; these lines are provisional. A probe that raises reports its neighbour `down` with the
exception named; it is never skipped. Probes run in parallel and print in the doc's order. Where `ps` is missing or
fails (Windows), the busy clause says processes are not listed here. Hold files name a pid; on Windows the pid is not
checked, because signalling a pid there is not a no-op.

## The readings file and `record`

`record` appends one line to the `meters` file, creating it with the header
`utc, label, account, weekly_pct, weekly_reset, five_pct, five_reset, session` (tab-separated) when it is missing or
empty. It never edits a line; the newest line per label is the reading. The label is this account's label, the account
is named by the weekly reset's weekday. It prints `neighbour-check: recorded in <path>: <line>`.

```
python3 neighbour-check.py record --weekly 83 --weekly-reset 2026-10-27T14:00:00Z --five 12 --five-reset 2026-10-27T07:10:00Z
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | The block was written and the doc is sound. |
| 1 | No doc, or findings in it (the block is still printed, with a `Findings:` line), or a self-test case failed. |
| 2 | Invalid usage or unreadable input: a bad ISO time, `--weekly` outside 0 to 100, an unknown `--burn`, an unreadable `--config`, a `--root` that is not a folder. |

## Example

```
$ python3 neighbour-check/neighbour-check.py --doc neighbours.md
## Neighbours
Measured 2026-03-10 14:05 UTC by neighbour-check from neighbours.md (verdicts PROVISIONAL: closed, tight, on pace, spare, unknown; free, busy, up, down). Burn: shown.
codex (codex): ON PACE. 62 % of its 7.0 d window (plan-a) as of its last call at 13:58 UTC, resets Fri 13 Mar 09:00 UTC (in 2.8 d). Burn: +14 points in 24 h, +2 points in 3 h; this window's session tokens, cache reads included: tool-a 41.0M. Busy: 1 codex exec job running; 3 sessions written in 15 min (tool-a 3).
lane-a (openai, lanes): FREE. serves: model-a; no meter (local model). Busy: lane lane-a IDLE (loaded, 0 jobs).
gemma (ollama): UP. no meter (local); available: model-b, model-c. Busy: loaded: none (Ollama shows no in-flight requests).
Lanes (this project): daemon up (heartbeat 4 s ago); owner alex; queue 0 waiting, 0 running; holds: none.
Not checked: scribe (no probe matches its row; name one with `kind scribe: <kind>`).
Ask the user: nothing.
```

## Tests

`python3 neighbour-check.py --self-test` builds its own throwaway folders and runs no process, network or ssh call:
every probe takes its processes, HTTP, ssh, clock and environment from the test. Green cases: a Codex rollout read
and its pace, with the 24 h gain counting the newest window only; the verdict lines; `codex exec` and `opencode run`
told from `app-server`, `exec-server` and a service; project labels; the guard words FREE, IN-USE and UNREACHABLE;
open sessions in `now.tsv`; the account label and weekday names; the readings file's header and newest row per label;
the Claude, Go, Ollama, OpenAI-wire, agy, guard and lanes probes; the kind guess on a sample doc table, `kind`
overrides and `probes` turning a kind off; settings precedence, a SKILL.md `## Config` block, unknown-key notes and
`kind`/`guard` for a name not on the list; the lanes line printed once, the switch and a stale daemon, hold files, and
no pid signalled on Windows; burn shown and hidden with the verdict kept; the doc read through the shared reader (no
doc, `--only`, `pitmaster.md`); `record` and exit 2; and the script run by path from a lone copy of its folder with an
empty `HOME`.

The red arm comes last. `NEIGHBOUR_CHECK_BREAK=1` plants a defect in the real pace function (an earlier window's
readings leak into the pace); the self-test turns it on, must see the window rule fail (it prints that `FAIL` line),
and passes only because the failure was caught. Set from outside, `NEIGHBOUR_CHECK_BREAK=1 python3 neighbour-check.py
--self-test` exits 1.
