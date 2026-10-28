# comment-only

Proves that an edit changed only comments. It strips the comments and blank lines from each file at two
points (a git revision, or the working tree), collapses spacing, and compares what is left. Identical output
means no code changed. It reports only and never changes a file.

Python 3.8 or newer, standard library only, plus `git` on the path. macOS, Linux, Windows.

## Usage

```
python3 comment-only.py [--rev REV] [--to REV] [--c-ext EXT] [--asm-ext EXT] [PATH ...]
python3 comment-only.py --self-test
```

Run it from inside the repository; paths are relative to the current directory.

| Option | Meaning |
|--------|---------|
| `PATH ...` | The files to check. With none, the files `git diff --name-only` lists between the two points. |
| `--rev REV` | The old side: a git revision. Default: `HEAD`. |
| `--to REV` | The new side: a git revision. Default: the working tree. |
| `--c-ext EXT` | Read this extension as C/C++. May repeat. |
| `--asm-ext EXT` | Read this extension as NASM assembly. May repeat. A flag moves an extension that a default already claims. |
| `--self-test` | Run the built-in cases, print `PASS`, `FAIL` or `SKIP` for each, then a verdict line. Exits non-zero on any failure. |

## Supported languages

| Language | Extensions (default) | Comments | Literals kept as code |
|----------|----------------------|----------|-----------------------|
| C and C++ | `.c .cc .cpp .h .hpp .inc .ld` | `//` and `/* */` | strings, raw strings (`R"x(...)x"`), character literals |
| NASM assembly | `.asm` | `;` | strings in `''`, `""` and backticks |

A digit separator (`0x9E37'79B9`) is part of its number, not a character literal. Whitespace runs inside code
collapse to one space and each line is trimmed, so re-indenting reads as identical; whitespace inside a literal
is kept exactly. A literal left open at the end of a line (an apostrophe in an `#error` message) ends there, and
a comment marker after it on that line is kept as code, which can only make the check fail, never pass wrongly.
Line endings (`\n` or `\r\n`) are not code.

With no paths, a changed file whose extension has no grammar is named in the verdict line as "not checked"; it
is never counted as passing. A path named on the command line with an unsupported extension is an error.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Every file's code is identical at the two points. |
| 1 | A file's code differs (the first differing line is printed), or a file is missing on one side. |
| 2 | A usage error, a named file with an unsupported extension, or git could not answer (not a repository, unknown revision, no git). |

## Example

```
$ python3 comment-only/comment-only.py src/main.cpp include/util.h
comment-only: PASS -- 2 file(s), code identical

$ python3 comment-only/comment-only.py --rev HEAD~1 --to HEAD
  src/main.cpp: code line 14 differs: -'int n = 1;' +'int n = 2;'
comment-only: FAIL -- 1 of 3 file(s) changed code; 1 not checked: notes.txt
```

## Tests

`python3 comment-only.py --self-test` builds its own temporary git repositories, so there are no fixtures to
carry. Most cases are red arms, which prove a guard works: a code edit inside a comment-looking string, a raw
string, a character literal or a digit separator must be reported as a change; a code edit, an added line, a
deleted file, an unsupported extension, an unknown revision and a directory that is not a repository must each
fail by name with the right exit code. The cases that need git are skipped, and counted in the verdict line,
when git is not found.

## Behaviour changes from the source

- The tool is named `comment-only` (the source's was `check-comment-only`); every line it prints starts
  with `comment-only:`.
- New flags `--c-ext` and `--asm-ext` add or move an extension. The source's table was fixed.
- With no `--to`, the new side is the working tree, as before; the source marked it with a sentinel word, which
  is gone (so a branch of that name is an ordinary revision).
- An unknown revision, a directory that is not a repository, or a missing `git` now exits 2 with one line,
  where the source printed a traceback or read every file as missing.
- Git is asked for paths relative to the current directory (`--relative`, NUL-separated), so the tool also
  works from a subdirectory and with file names that need quoting.
- Files are read as bytes and decoded as UTF-8 with undecodable bytes preserved, on both sides, so a stray byte
  never aborts a run and `\r\n` is treated the same on every platform.
- The self-test prints one `PASS`/`FAIL`/`SKIP` line per case and the shed's verdict line, and has new cases
  for the command line (the source's arms covered only the stripping).
