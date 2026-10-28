#!/usr/bin/env python3
"""elf-loaded: digest the loaded sections of an ELF64, so a refactor's "changed no code"
claim is a comparison of what the loader runs rather than of the whole file.

Usage: elf-loaded.py ELF
       elf-loaded.py --compare A B
       elf-loaded.py --self-test

What it does
    A loaded section is one with SHF_ALLOC set: the bytes the loader maps or
    zero-fills. Each is recorded as its name, address, size and a sha-256
    digest of its bytes (a SHT_NOBITS section has no bytes, so its size and
    address are its whole record). Everything else is left out of the loaded
    digest: the debug sections, the symbol and string tables and the section
    header table itself. DWARF 5 line tables carry the MD5 of every source
    file (DW_LNCT_MD5), so any edit to a compiled source changes `.debug_line`
    and the whole-file hash, whatever code the compiler emits. The whole-file
    digest is printed beside the loaded one so the difference is visible, and
    judged by nothing.

    ELF is a path; there is no default. Only ELF64 little-endian images are
    read (the section header table is parsed from the bytes; no other tool is
    needed, so it behaves the same on macOS, Linux and Windows).

--compare A B
    Prints which loaded sections differ by name (a section present in one
    image only, or at another address or size, counts as differing), whether
    the whole files differ, and exits 0 when every loaded section is equal,
    1 when any differs.

Exit codes
    0  record printed; --compare found equal loaded sections; --self-test passed
    1  --compare found a differing loaded section; --self-test failed
    2  a usage error, or an ELF could not be read (missing, not ELF64
       little-endian, a section header table or section that runs past the end
       of the file)

It reads files only. It never edits, creates or deletes one.

--self-test
    Builds ELF64 images in memory (no compiler needed), runs the built-in
    cases, prints one PASS or FAIL line per case and a verdict line, and exits
    non-zero on any failure.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

PROG = "elf-loaded"
SHF_ALLOC = 0x2
SHT_NOBITS = 8


class ElfError(Exception):
    """The file is not an ELF64 little-endian image this tool can read."""


class Section(NamedTuple):
    name: str
    addr: int
    size: int
    digest: str  # "nobits" for SHT_NOBITS, else the first 16 hex of sha-256


class Record(NamedTuple):
    whole: str
    sections: List[Section]

    def loaded(self) -> str:
        blob = "".join(f"{s.name} {s.addr:#x} {s.size} {s.digest}\n" for s in self.sections)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def read_record(path: str) -> Record:
    """The record of the ELF64 at path; ElfError when it cannot be read."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ElfError(f"{path}: {exc.strerror or exc}") from exc
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ElfError(f"{path}: not an ELF file")
    if data[4] != 2 or data[5] != 1:
        raise ElfError(f"{path}: not ELF64 little-endian")
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    if shentsize < 64 or shnum == 0 or shoff + shnum * shentsize > len(data):
        raise ElfError(f"{path}: section header table runs past the end of the file")

    def header(i: int) -> Tuple[int, int, int, int, int, int]:
        f = struct.unpack_from("<IIQQQQ", data, shoff + i * shentsize)
        return f[0], f[1], f[2], f[3], f[4], f[5]  # name, type, flags, addr, offset, size

    if shstrndx >= shnum:
        raise ElfError(f"{path}: section name table index {shstrndx} out of range")
    _, _, _, _, stroff, strsize = header(shstrndx)
    if stroff + strsize > len(data):
        raise ElfError(f"{path}: section name table runs past the end of the file")
    names = data[stroff:stroff + strsize]
    out: List[Section] = []
    for i in range(1, shnum):
        n, kind, flags, addr, off, size = header(i)
        if not flags & SHF_ALLOC:
            continue
        end = names.find(b"\0", n)
        if end < 0:  # also true when the name starts past the end of the table
            raise ElfError(f"{path}: section {i} has an unterminated name")
        name = names[n:end].decode("ascii", "replace")
        if kind == SHT_NOBITS:
            digest = "nobits"
        else:
            if off + size > len(data):
                raise ElfError(f"{path}: section {name} runs past the end of the file")
            digest = hashlib.sha256(data[off:off + size]).hexdigest()[:16]
        out.append(Section(name, addr, size, digest))
    return Record(hashlib.sha256(data).hexdigest()[:16], out)


def print_record(rec: Record) -> None:
    print(f"{PROG}: whole file {rec.whole}")
    print(f"{PROG}: loaded {rec.loaded()} ({len(rec.sections)} sections)")
    for s in rec.sections:
        print(f"{PROG}:   {s.name:<24} {s.addr:#012x} {s.size:>9} {s.digest}")


def differing(a: Record, b: Record) -> List[str]:
    """Names of the loaded sections that differ, present in one image only, or moved."""
    am: Dict[str, Section] = {s.name: s for s in a.sections}
    bm: Dict[str, Section] = {s.name: s for s in b.sections}
    names = [s.name for s in a.sections] + [s.name for s in b.sections if s.name not in am]
    return [n for n in names if am.get(n) != bm.get(n)]


def compare(pa: str, pb: str) -> int:
    a, b = read_record(pa), read_record(pb)
    diff = differing(a, b)
    print(f"{PROG}: whole file {'EQUAL' if a.whole == b.whole else 'DIFFER'} ({a.whole} {b.whole})")
    if diff:
        print(f"{PROG}: loaded sections DIFFER: {' '.join(diff)}")
        return 1
    print(f"{PROG}: loaded sections EQUAL ({a.loaded()}, {len(a.sections)} sections)")
    return 0


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(
        prog=PROG, description="Digest the loaded sections of an ELF64, or compare two.")
    ap.add_argument("elf", nargs="?", metavar="ELF", help="the ELF64 file to print the record of")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="compare the loaded sections of two ELFs")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases and exit")
    args = ap.parse_args(argv)
    if args.self_test:
        return _run_self_test()
    if (args.elf is None) == (args.compare is None):
        ap.error("give one ELF to print, or --compare A B")
    try:
        if args.compare is not None:
            return compare(args.compare[0], args.compare[1])
        print_record(read_record(args.elf))
        return 0
    except ElfError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Self-test: ELF64 images built in memory, so the cases need no toolchain.
# ---------------------------------------------------------------------------

Spec = Tuple[str, int, int, int, bytes]  # name, type, flags, addr, bytes (size for NOBITS)


def build_elf(specs: List[Spec]) -> bytes:
    """A minimal ELF64 LE image holding the given sections, then a name table."""
    names = b"\0"
    name_at: Dict[str, int] = {}
    for name, *_ in specs + [(".shstrtab", 3, 0, 0, b"")]:
        name_at[name] = len(names)
        names += name.encode() + b"\0"
    body = b""
    placed: List[Tuple[Spec, int, int]] = []
    off = 64
    for spec in specs:
        raw = b"" if spec[1] == SHT_NOBITS else spec[4]
        placed.append((spec, off, len(spec[4]) if spec[1] == SHT_NOBITS else len(raw)))
        body += raw
        off += len(raw)
    stroff = off
    body += names
    shoff = stroff + len(names)
    shdrs = struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    for (name, kind, flags, addr, _), o, sz in placed:
        shdrs += struct.pack("<IIQQQQIIQQ", name_at[name], kind, flags, addr, o, sz, 0, 0, 1, 0)
    shdrs += struct.pack("<IIQQQQIIQQ", name_at[".shstrtab"], 3, 0, 0, stroff, len(names), 0, 0, 1, 0)
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    ehdr = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64,
                               len(specs) + 2, len(specs) + 1)
    return ehdr + body + shdrs


def baseline() -> List[Spec]:
    return [
        (".text", 1, 0x6, 0x100000, b"\x55\x48\x89\xe5\x90\x5d\xc3"),
        (".rodata", 1, 0x2, 0x200000, b"hello\0"),
        (".data", 1, 0x3, 0x300000, b"\x01\x02"),
        (".bss", SHT_NOBITS, 0x3, 0x301000, b"\0" * 64),
        (".debug_line", 1, 0, 0, b"md5-of-sources-A"),
        (".comment", 1, 0, 0, b"clang"),
    ]


def _edit(name: str, data: Optional[bytes] = None, size: Optional[int] = None) -> List[Spec]:
    specs = baseline()
    for i, (n, k, f, a, d) in enumerate(specs):
        if n == name:
            nd = data if data is not None else (b"\0" * size if size is not None else d)
            specs[i] = (n, k, f, a, nd)
    return specs


def _write(td: Path, label: str, image: bytes) -> str:
    p = td / f"{label}.elf"
    p.write_bytes(image)
    return str(p)


def _compare_quiet(pa: str, pb: str) -> Tuple[int, Record, Record]:
    """compare() without its two printed lines (they would sit among the PASS/FAIL lines)."""
    a, b = read_record(pa), read_record(pb)
    return (1 if differing(a, b) else 0), a, b


def _sh_field(image: bytes, index: int, field_off: int, fmt: str, value: int) -> bytes:
    """image with one field of section header INDEX overwritten."""
    buf = bytearray(image)
    shoff = struct.unpack_from("<Q", buf, 0x28)[0]
    struct.pack_into(fmt, buf, shoff + index * 64 + field_off, value)
    return bytes(buf)


def _refused(path: str, fragment: str) -> bool:
    try:
        read_record(path)
    except ElfError as exc:
        return fragment in str(exc)
    return False


def _case_identical(td: Path) -> bool:
    base = _write(td, "base", build_elf(baseline()))
    rc, a, b = _compare_quiet(base, base)
    return rc == 0 and a.whole == b.whole and differing(a, b) == []


def _case_debug_only_change(td: Path) -> bool:
    # The case a whole-file hash gets wrong: a comment-only edit moves the line table's
    # source digests and nothing else.
    base = _write(td, "base", build_elf(baseline()))
    cmt = _write(td, "comment", build_elf(_edit(".debug_line", data=b"md5-of-sources-B")))
    rc, a, b = _compare_quiet(base, cmt)
    return rc == 0 and a.whole != b.whole and differing(a, b) == []


def _case_text_change(td: Path) -> bool:
    # RED ARM: a changed instruction makes .text differ, by name.
    base = _write(td, "base", build_elf(baseline()))
    text = _write(td, "text", build_elf(_edit(".text", data=b"\x55\x48\x89\xe5\x90\x90\x5d\xc3")))
    rc, a, b = _compare_quiet(base, text)
    return rc == 1 and differing(a, b) == [".text"]


def _case_rodata_change(td: Path) -> bool:
    # RED ARM: a changed constant makes .rodata differ, by name.
    base = _write(td, "base", build_elf(baseline()))
    ro = _write(td, "rodata", build_elf(_edit(".rodata", data=b"hellp\0")))
    rc, a, b = _compare_quiet(base, ro)
    return rc == 1 and differing(a, b) == [".rodata"]


def _case_data_change(td: Path) -> bool:
    # RED ARM: a changed initialiser makes .data differ, by name.
    base = _write(td, "base", build_elf(baseline()))
    dat = _write(td, "data", build_elf(_edit(".data", data=b"\x01\x03")))
    rc, a, b = _compare_quiet(base, dat)
    return rc == 1 and differing(a, b) == [".data"]


def _case_bss_larger(td: Path) -> bool:
    # RED ARM: a section with no bytes is still compared, by size.
    base = _write(td, "base", build_elf(baseline()))
    bss = _write(td, "bss", build_elf(_edit(".bss", size=128)))
    rc, a, b = _compare_quiet(base, bss)
    return rc == 1 and differing(a, b) == [".bss"]


def _case_moved_section(td: Path) -> bool:
    # RED ARM: the same bytes at another address differ.
    moved = baseline()
    moved[1] = (".rodata", 1, 0x2, 0x200010, b"hello\0")
    base = _write(td, "base", build_elf(baseline()))
    rc, a, b = _compare_quiet(base, _write(td, "moved", build_elf(moved)))
    return rc == 1 and differing(a, b) == [".rodata"]


def _case_extra_section(td: Path) -> bool:
    # RED ARM: a loaded section present in one image only differs, in either direction.
    extra = baseline() + [(".eh_frame", 1, 0x2, 0x400000, b"\0\0")]
    base = _write(td, "base", build_elf(baseline()))
    ext = _write(td, "extra", build_elf(extra))
    rc1, a1, b1 = _compare_quiet(base, ext)
    rc2, a2, b2 = _compare_quiet(ext, base)
    return (rc1 == 1 and differing(a1, b1) == [".eh_frame"]
            and rc2 == 1 and differing(a2, b2) == [".eh_frame"])


def _case_refuse_non_elf(td: Path) -> bool:
    p = td / "junk.elf"
    p.write_bytes(b"not an elf at all, just text" * 4)
    return _refused(str(p), "not an ELF file")


def _case_refuse_short(td: Path) -> bool:
    p = td / "short.elf"
    p.write_bytes(b"\x7fELF\x02\x01")
    return _refused(str(p), "not an ELF file")


def _case_refuse_elf32(td: Path) -> bool:
    p = td / "elf32.elf"
    p.write_bytes(b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(60))
    return _refused(str(p), "not ELF64 little-endian")


def _case_refuse_big_endian(td: Path) -> bool:
    img = build_elf(baseline())
    p = td / "bigendian.elf"
    p.write_bytes(img[:5] + b"\x02" + img[6:])  # EI_DATA = 2
    return _refused(str(p), "not ELF64 little-endian")


def _case_refuse_truncated_headers(td: Path) -> bool:
    p = td / "cut.elf"
    p.write_bytes(build_elf(baseline())[:-40])
    return _refused(str(p), "section header table runs past")


def _case_refuse_no_sections(td: Path) -> bool:
    img = bytearray(build_elf(baseline()))
    struct.pack_into("<H", img, 0x3C, 0)  # e_shnum = 0
    p = td / "nosections.elf"
    p.write_bytes(bytes(img))
    return _refused(str(p), "section header table runs past")


def _case_refuse_missing_file(td: Path) -> bool:
    return _refused(str(td / "absent.elf"), "absent.elf")


def _case_refuse_section_past_end(td: Path) -> bool:
    img = _sh_field(build_elf(baseline()), 1, 32, "<Q", 10 ** 9)  # .text sh_size
    p = td / "pastend.elf"
    p.write_bytes(img)
    return _refused(str(p), "section .text runs past the end of the file")


def _case_refuse_name_table_index(td: Path) -> bool:
    img = bytearray(build_elf(baseline()))
    struct.pack_into("<H", img, 0x3E, 200)  # e_shstrndx beyond the table
    p = td / "strndx.elf"
    p.write_bytes(bytes(img))
    return _refused(str(p), "section name table index 200 out of range")


def _case_refuse_name_table_past_end(td: Path) -> bool:
    img = _sh_field(build_elf(baseline()), len(baseline()) + 1, 32, "<Q", 10 ** 9)  # .shstrtab sh_size
    p = td / "strtab.elf"
    p.write_bytes(img)
    return _refused(str(p), "section name table runs past the end of the file")


def _case_refuse_name_beyond_table(td: Path) -> bool:
    img = _sh_field(build_elf(baseline()), 1, 0, "<I", 10 ** 6)  # .text sh_name
    p = td / "badname.elf"
    p.write_bytes(img)
    return _refused(str(p), "section 1 has an unterminated name")


def _case_refuse_unterminated_name(td: Path) -> bool:
    # cut the name table inside the name of the last loaded section (.bss), so it has no NUL
    names = b"\0"
    for name in [s[0] for s in baseline()]:
        if name == ".bss":
            cut = len(names) + 3
        names += name.encode() + b"\0"
    img = _sh_field(build_elf(baseline()), len(baseline()) + 1, 32, "<Q", cut)  # .shstrtab sh_size
    p = td / "unterminated.elf"
    p.write_bytes(img)
    return _refused(str(p), "has an unterminated name")


def _cli(*args: str) -> Tuple[str, str, int]:
    script = str(Path(__file__).resolve())
    r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout, r.stderr, r.returncode


def _case_cli_print_record(td: Path) -> bool:
    base = _write(td, "base", build_elf(baseline()))
    out, _, rc = _cli(base)
    return (rc == 0 and f"{PROG}: whole file " in out and "(4 sections)" in out
            and ".text" in out and ".bss" in out and ".debug_line" not in out
            and ".bss" in out.split(".text")[1] and out.rstrip().endswith("nobits"))


def _case_cli_compare_exit_codes(td: Path) -> bool:
    # RED ARM: --compare exits 0 on equal loaded sections and 1, naming the section, on a difference.
    base = _write(td, "base", build_elf(baseline()))
    cmt = _write(td, "comment", build_elf(_edit(".debug_line", data=b"md5-of-sources-B")))
    text = _write(td, "text", build_elf(_edit(".text", data=b"\x90")))
    ok_out, _, ok_rc = _cli("--compare", base, cmt)
    bad_out, _, bad_rc = _cli("--compare", base, text)
    return (ok_rc == 0 and "whole file DIFFER" in ok_out and "loaded sections EQUAL" in ok_out
            and bad_rc == 1 and "loaded sections DIFFER: .text" in bad_out)


def _case_cli_unreadable_exits_2(td: Path) -> bool:
    # RED ARM: an unreadable ELF exits 2 with a message on stderr, in both modes.
    base = _write(td, "base", build_elf(baseline()))
    junk = td / "junk.elf"
    junk.write_bytes(b"x" * 100)
    _, err1, rc1 = _cli(str(junk))
    _, err2, rc2 = _cli("--compare", base, str(td / "absent.elf"))
    return rc1 == 2 and "not an ELF file" in err1 and rc2 == 2 and "absent.elf" in err2


def _case_cli_needs_an_elf(td: Path) -> bool:
    # There is no default ELF: no argument is a usage error, as is naming both modes.
    base = _write(td, "base", build_elf(baseline()))
    _, _, rc1 = _cli()
    _, _, rc2 = _cli(base, "--compare", base, base)
    return rc1 == 2 and rc2 == 2


def _case_never_edits(td: Path) -> bool:
    base = _write(td, "base", build_elf(baseline()))
    other = _write(td, "other", build_elf(_edit(".data", data=b"\x09")))
    before = {p.name: p.read_bytes() for p in td.iterdir()}
    _cli(base)
    _cli("--compare", base, other)
    return {p.name: p.read_bytes() for p in td.iterdir()} == before


# (name, function)
_CASES: List[Tuple[str, Callable[[Path], bool]]] = [
    ("identical-files-are-equal-everywhere", _case_identical),
    ("changed-debug-section-loaded-equal-whole-different", _case_debug_only_change),
    ("changed-instruction-text-differs", _case_text_change),
    ("changed-constant-rodata-differs", _case_rodata_change),
    ("changed-initialiser-data-differs", _case_data_change),
    ("larger-bss-differs", _case_bss_larger),
    ("section-at-another-address-differs", _case_moved_section),
    ("section-in-one-image-only-differs", _case_extra_section),
    ("non-elf-file-refused", _case_refuse_non_elf),
    ("file-shorter-than-a-header-refused", _case_refuse_short),
    ("elf32-file-refused", _case_refuse_elf32),
    ("big-endian-elf64-refused", _case_refuse_big_endian),
    ("truncated-section-header-table-refused", _case_refuse_truncated_headers),
    ("no-section-headers-refused", _case_refuse_no_sections),
    ("missing-file-refused", _case_refuse_missing_file),
    ("section-past-end-of-file-refused", _case_refuse_section_past_end),
    ("name-table-index-out-of-range-refused", _case_refuse_name_table_index),
    ("name-table-past-end-of-file-refused", _case_refuse_name_table_past_end),
    ("section-name-beyond-name-table-refused", _case_refuse_name_beyond_table),
    ("unterminated-section-name-refused", _case_refuse_unterminated_name),
    ("cli-prints-the-record-of-loaded-sections", _case_cli_print_record),
    ("cli-compare-exit-codes", _case_cli_compare_exit_codes),
    ("cli-unreadable-elf-exits-2", _case_cli_unreadable_exits_2),
    ("cli-needs-an-elf-argument", _case_cli_needs_an_elf),
    ("never-edits", _case_never_edits),
]


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS/FAIL per case."""
    n_pass = n_fail = 0
    for name, fn in _CASES:
        with tempfile.TemporaryDirectory() as td:
            try:
                ok = fn(Path(td))
            except Exception as exc:  # a crashing case is a failing case
                print(f"FAIL {name} ({type(exc).__name__}: {exc})")
                n_fail += 1
                continue
        if ok:
            print(f"PASS {name}")
            n_pass += 1
        else:
            print(f"FAIL {name}")
            n_fail += 1
    ran = n_pass + n_fail
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
