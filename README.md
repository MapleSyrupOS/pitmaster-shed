# pitmaster-shed

Small tools a person can run without an agent. Each one is a single Python 3 script that uses only the
standard library, in its own folder with a README and a self-test. They run on macOS, Linux and Windows; a tool's
README names any limit. Apache-2.0.

## Install

Copy the folder of the tool you want into your project and run it, or add this repository to your
`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: <URL of this repository>
    rev: <tag or commit>
    hooks:
      - id: ratchet
```

The hooks are `adr-check`, `adr-index`, `compact-verify`, `modeguard`, `ratchet` and `status-check`; the other
tools are run by hand. Every tool runs `--self-test` to check itself, for example `python3 ratchet/ratchet.py --self-test`.

## Tools

| Tool | What it does |
|------|--------------|
| [`adr-check`](adr-check/README.md) | Checks every ADR's YAML header, that each proof resolves, and that MADR's features appear only when two or more options were weighed. |
| [`adr-index`](adr-index/README.md) | Writes `INDEX.md`, one table row per ADR, from the ADRs themselves; `--check` fails when it is missing or stale. |
| [`comment-only`](comment-only/README.md) | Proves that an edit changed only comments, by comparing two versions of each file with comments and spacing stripped (C/C++ and NASM assembly). |
| [`compact`](compact/README.md) | Moves a block of text into an append-only archive, verbatim and proven, and verifies from git that a compaction changed nothing else. |
| [`elf-loaded`](elf-loaded/README.md) | Digests the loaded sections of an ELF64 file and compares two, so "this refactor changed no code" is checked against what the loader runs, not the whole file. |
| [`fence-mark`](fence-mark/README.md) | Runs a command in a directory and always leaves a completion marker and a diff of the change, untracked files included. |
| [`modeguard`](modeguard/README.md) | Refuses a file-mode change (an executable bit lost, 100755 to 100644) on a tracked path unless it is on an allow list; installs a pre-commit hook. |
| [`ratchet`](ratchet/README.md) | Fails when a listed file grows past its line-count limit. |
| [`split-status-log`](split-status-log/README.md) | Splits a newest-first, dated log file by month, byte for byte: the current month stays, each older month moves to its own file. |
| [`status-check`](status-check/README.md) | Checks a status file's line ceiling and that every bullet in the chosen sections cites a file that exists. |

## Siblings

- [asf-yaml-editor](https://github.com/writerblockade/asf-yaml-editor): an offline validator and builder for
  Apache `.asf.yaml` files.
