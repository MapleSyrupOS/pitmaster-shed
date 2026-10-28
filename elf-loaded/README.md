# elf-loaded

Digests the loaded sections of an ELF64, so a refactor's "changed no code" claim is a comparison of what the
loader runs rather than of the whole file. It reads files only and never changes one.

Python 3.8 or newer, standard library only. macOS, Linux, Windows: it parses the ELF section header table from
the bytes, so it needs no other tool and behaves the same on every platform. It reads ELF64 little-endian
images only (a 32-bit, big-endian or non-ELF file is refused with exit 2).

## Usage

```
python3 elf-loaded.py ELF
python3 elf-loaded.py --compare A B
python3 elf-loaded.py --self-test
```

| Option | Meaning |
|--------|---------|
| `ELF` | The file to print the record of. There is no default; naming no file is a usage error. |
| `--compare A B` | Compare the loaded sections of two ELFs: print which differ, and whether the whole files differ. |
| `--self-test` | Build ELF64 images in memory, run the built-in cases, print `PASS` or `FAIL` for each, then a verdict line. Exits non-zero on any failure. |

## What a record is

A loaded section is one with `SHF_ALLOC` set: the bytes the loader maps or zero-fills. Each is recorded as its
name, address, size and the first 16 hex digits of a sha-256 of its bytes (a `SHT_NOBITS` section such as
`.bss` has no bytes, so its address and size are its whole record). Left out of the loaded digest: the debug
sections, the symbol and string tables and the section header table itself.

DWARF 5 line tables carry the MD5 of every source file, so any edit to a compiled source changes
`.debug_line` and the whole-file hash, whatever code the compiler emits. That is why the whole-file digest is
printed beside the loaded one: the difference is visible, and judged by nothing.

With `--compare`, a section present in one image only, or at another address or size, counts as differing.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Record printed; or `--compare` found every loaded section equal; or the self-test passed. |
| 1 | `--compare` found a differing loaded section; or the self-test failed. |
| 2 | A usage error, or an ELF could not be read (missing, not ELF64 little-endian, a section header table or section that runs past the end of the file). The reason is printed on stderr. |

## Example

```
$ python3 elf-loaded/elf-loaded.py build/app.elf
elf-loaded: whole file 3f9a1c0b7d2e4a58
elf-loaded: loaded 91c04be2a7f3d610 (4 sections)
elf-loaded:   .text                0x0000100000         7 5b2e9f01aa7c3d44
elf-loaded:   .rodata              0x0000200000         6 8a1f0c93e2b7d561
elf-loaded:   .data                0x0000300000         2 c4e7a21b09d8f356
elf-loaded:   .bss                 0x0000301000        64 nobits

$ python3 elf-loaded/elf-loaded.py --compare before.elf after.elf
elf-loaded: whole file DIFFER (3f9a1c0b7d2e4a58 e01b5c7a92d4f386)
elf-loaded: loaded sections EQUAL (91c04be2a7f3d610, 4 sections)
$ echo $?
0
```

## Tests

`python3 elf-loaded.py --self-test` builds its ELF64 images in memory, so there is no compiler and there are no
fixtures to carry. Several cases are red arms, which prove a guard works: a changed instruction, constant,
initialiser, `.bss` size, section address, or a section present in one image only must each make `--compare`
exit 1 and name the section; a non-ELF, 32-bit, big-endian, truncated or malformed file (section header table,
section, name table or section name running out of bounds) must each be refused with exit 2.

## Behaviour changes from the source

- There is no default ELF. The source defaulted to a build path of its own project; with no argument the tool
  now exits 2 with a usage message. (A neutral default was not possible: any path would be a guess.)
- The command line is parsed by `argparse`, so `--help` works and a malformed command line exits 2 with a
  usage line rather than printing the module text. `--compare` takes exactly two files; naming an ELF as well
  is a usage error.
- The wording that named the original host's artefact is generic ("what the loader runs").
- The self-test prints one `PASS`/`FAIL` line per case and the shed's verdict line. New cases: a file shorter
  than a header, no section headers, a name-table index and a name-table size out of range, a section name
  past the name table, an unterminated section name, the reverse direction of a one-sided section, and the
  command line (record output, `--compare` exit codes, exit 2 for an unreadable file or a missing argument,
  and that no file is changed).
