#!/usr/bin/env python3
"""adr-index: write INDEX.md, one table row per ADR, from the ADRs themselves.

Usage: adr-index.py [--root DIR] [--dir PATH] [--check] [--self-test]

What it does
    Reads every ADR in the decisions folder and writes the folder's INDEX.md. Every cell comes from the ADR file,
    so the table cannot drift from the records:
      ADR       the file, linked
      Decision  the title, without its "ADR-001 —" prefix
      Kind      architecture, design or process, from the YAML header ("—" for formats without one)
      Status    the `status` word of the YAML header, one of the seven (or MADR's or Nygard's status, mapped to them)
      Built     landed, partial, not started or n/a, from the `built` field ("—" for formats without one)

What it understands
    An ADR is a Markdown file named like ``ADR-001-some-slug.md`` or ``0001-some-slug.md``. Four formats are read:
    an ADR+ YAML header, the older bold ``**Status:**`` lines, MADR, and Nygard's ``## Status`` section.

What it promises
    - Exit 0: the index was written, or (with --check) it is current.
    - Exit 1: with --check, INDEX.md is missing or stale. Nothing is written.
    - Exit 2: no decisions folder was found. Nothing is written.
    - It writes only ``INDEX.md`` in the decisions folder, always with ``\\n`` line ends and UTF-8.
    - It looks only below --root; it never searches the folders above it.

--root DIR
    The repository root. Default: the current directory.

--dir PATH
    The decisions folder, relative to the root. Default: the first of docs/decisions, docs/adr, docs/adrs,
    doc/adr, doc/decisions, adr, decisions that exists.

--check
    Write nothing; exit 1 if INDEX.md is missing or differs from what would be written.

--self-test
    Builds its own ADRs in temporary directories, prints one PASS or FAIL line per case and a verdict line, and
    exits non-zero on any failure.

Python 3.8 or newer, standard library only. macOS, Linux, Windows.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, cast

PROG = "adr-index"

# --- shed:shared adrplus begin ---
# adr-plus 0.1.0, tools/adrplus.py and schema/adr-plus.schema.json
# The reader for an ADR's YAML header and the validator for the part of JSON Schema that the adr-plus schema
# uses. Kept as close to the release's code as a single file allows: what neither tool calls is left out, the
# schema is a string constant, and the decisions-folder lookup is named configured_decisions_dir.

DECISION_DIRS: Tuple[str, ...] = ("docs/decisions", "docs/adr", "docs/adrs", "doc/adr", "doc/decisions",
                                  "adr", "decisions")

FIELD_ORDER: Tuple[str, ...] = (
    "status", "status-note", "ratified-by", "ratified-on", "owner", "kind",
    "built", "proof", "owed", "supersedes", "amends", "consulted",
    "confirmed-by", "revisit-when",
)
# The seven status words the schema allows, and the older verdicts that map onto them.
STATUS_WORDS: Tuple[str, ...] = (
    "proposed", "provisional", "accepted", "partially accepted", "superseded",
    "withdrawn", "rejected")
VERDICT_MAP: Dict[str, str] = {
    "ratified": "accepted", "adopted": "accepted", "ruled": "accepted", "landed": "accepted",
    "deprecated": "withdrawn", "retired": "withdrawn", "closed": "withdrawn",
    "nixed": "rejected",
    "draft": "proposed", "awaiting": "proposed",
}
# A setting is a row of the decisions README's settings table: `| Owner | the maintainers |`. The
# value runs to the end of the cell or to a ` — ` that starts an explanation.
SETTING_ROW = re.compile(r"^\|\s*([A-Za-z][A-Za-z ]*?)\s*\|\s*(.*?)\s*(?:\s—\s.*)?\|?\s*$")


class YamlError(Exception):
    """A header line the flat parser will not read."""


class HeaderError(Exception):
    """A header that cannot be read: a malformed YAML block. The message names the file."""


# ------------------------------------------------------------ YAML scalars

_BOOL = re.compile(r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$")
_NULL = re.compile(r"^(?:~|null|Null|NULL|)$")
_INT = re.compile(
    r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+"
    r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$")
_FLOAT = re.compile(
    r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
    r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_STAMP = re.compile(
    r"^[0-9]{4}-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9]"
    r"(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?$")


def resolve_plain(text: str) -> object:
    """The value YAML 1.1 gives an unquoted scalar: what PyYAML would load."""
    if _BOOL.match(text):
        return text.lower() in ("yes", "true", "on")
    if _NULL.match(text):
        return None
    if _DATE.match(text):
        try:
            return datetime.date(int(text[:4]), int(text[5:7]), int(text[8:10]))
        except ValueError:
            return text
    if _STAMP.match(text):
        return datetime.datetime(1970, 1, 1)
    if _INT.match(text):
        return 0
    if _FLOAT.match(text):
        return 0.0
    if text in ("<<", "="):
        return text
    return text


def type_name(value: object) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if value is None:
        return "null"
    if isinstance(value, datetime.datetime):
        return "a timestamp"
    if isinstance(value, datetime.date):
        return "a date"
    if isinstance(value, int):
        return "a number"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, dict):
        return "a mapping"
    return "a list"


_START_UNSAFE = set("-?:,[]{}#&*!|>'\"%@`~=<")


def needs_quotes(value: str) -> bool:
    """Whether `value` cannot be written as a plain YAML scalar and still load
    as the same string."""
    if value == "":
        return True
    if value != value.strip():
        return True
    if value[0] in _START_UNSAFE:
        return True
    if ": " in value or " #" in value or value.endswith(":"):
        return True
    for ch in value:
        if not ch.isprintable():
            return True
    if value in ("---", "..."):
        return True
    return not isinstance(resolve_plain(value), str)


def quote(value: str) -> str:
    out: List[str] = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch.isprintable():
            out.append(ch)
        elif ord(ch) <= 0xFF:
            out.append(f"\\x{ord(ch):02x}")
        elif ord(ch) <= 0xFFFF:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(f"\\U{ord(ch):08x}")
    out.append('"')
    return "".join(out)


def scalar(value: str) -> str:
    return quote(value) if needs_quotes(value) else value


def render_header(fields: Dict[str, str]) -> str:
    """`---`, the fields in schema order, `---`, one line each."""
    rows = ["---"]
    for name in FIELD_ORDER:
        if name in fields:
            rows.append(f"{name}: {scalar(fields[name])}")
    rows.append("---")
    return "\n".join(rows) + "\n"


_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
            "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
            "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028",
            "P": "\u2029"}
_KEY_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?: (.*))?$")


def _parse_double(text: str) -> str:
    out: List[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if text[i + 1:].strip() != "":
                raise YamlError(f"text after the closing quote: {text[i + 1:]!r}")
            return "".join(out)
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(text):
            break
        esc = text[i]
        if esc in _ESCAPES:
            out.append(_ESCAPES[esc])
            i += 1
        elif esc in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[esc]
            digits = text[i + 1:i + 1 + width]
            if len(digits) != width or not re.match(r"^[0-9a-fA-F]+$", digits):
                raise YamlError(f"bad \\{esc} escape")
            out.append(chr(int(digits, 16)))
            i += 1 + width
        else:
            raise YamlError(f"unknown escape \\{esc}")
    raise YamlError("unterminated double-quoted value")


def _parse_single(text: str) -> str:
    out: List[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if text[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            if text[i + 1:].strip() != "":
                raise YamlError(f"text after the closing quote: {text[i + 1:]!r}")
            return "".join(out)
        out.append(ch)
        i += 1
    raise YamlError("unterminated single-quoted value")


@dataclass
class Entry:
    key: str
    text: str
    value: object


def parse_flat(lines: Sequence[str]) -> List[Entry]:
    """The flat subset the converter writes: `key: value`, plain or quoted, one
    line each. A plain value takes the type YAML 1.1 would give it. Anything
    else (a nested block, a list, a flow collection, a duplicate key) is a
    YamlError."""
    entries: List[Entry] = []
    seen = set()
    for raw in lines:
        if raw.strip() == "":
            continue
        m = _KEY_LINE.match(raw)
        if m is None:
            raise YamlError(f"not a `key: value` line: {raw!r}")
        key, rest = m.group(1), m.group(2)
        if key in seen:
            raise YamlError(f"duplicate key {key!r}")
        seen.add(key)
        if rest is None or rest.strip() == "":
            entries.append(Entry(key, "", None))
            continue
        if rest[0] == '"':
            text = _parse_double(rest)
            entries.append(Entry(key, text, text))
        elif rest[0] == "'":
            text = _parse_single(rest)
            entries.append(Entry(key, text, text))
        else:
            if rest[0] in "[]{}|>&*!%@`,#?:" or rest.startswith("- "):
                raise YamlError(f"{key}: a value starting with {rest[0]!r} is outside the flat subset")
            text = rest
            cut = text.find(" #")
            if cut != -1:
                text = text[:cut]
            text = text.strip()
            if ": " in text or text.endswith(":"):
                raise YamlError(f"{key}: a plain value cannot hold ': '")
            entries.append(Entry(key, text, resolve_plain(text)))
    return entries


def split_header(text: str) -> Optional[Tuple[List[str], int]]:
    """(the lines between the leading `---` and the next `---`, the index of the
    line after the closing fence), or None when the file has no such block."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None
    for j in range(1, len(lines)):
        if lines[j] == "---":
            return lines[1:j], j + 1
    return None


# --------------------------------------------------------- JSON Schema subset

_ANNOTATIONS = frozenset(("$schema", "$id", "$comment", "title", "description",
                          "default", "examples"))
_KEYWORDS = frozenset(("type", "properties", "required", "additionalProperties",
                       "enum", "const", "pattern", "minLength", "allOf", "if",
                       "then", "else", "not"))


def _is_type(value: object, name: str) -> bool:
    if name == "string":
        return isinstance(value, str)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise ValueError(f"unknown JSON Schema type {name!r}")


def as_dict(o: object) -> Dict[str, object]:
    if not isinstance(o, dict):
        raise ValueError("schema node is not an object")
    return cast(Dict[str, object], o)


def _check(inst: object, sch: object, path: str, errs: List[str]) -> None:
    """Validate `inst` against `sch`, appending `path: message` lines. Knows only
    the keywords in _KEYWORDS and refuses any other, so the schema cannot start
    using one this validator would silently skip."""
    if sch is True:
        return
    if sch is False:
        errs.append(f"{path}: not allowed here")
        return
    node = as_dict(sch)
    for key in node:
        if key not in _KEYWORDS and key not in _ANNOTATIONS:
            raise ValueError(f"schema keyword {key!r} is not implemented by adrplus.py")
    where = path or "(header)"
    if "type" in node:
        want = str(node["type"])
        if not _is_type(inst, want):
            hint = ""
            if want == "string" and not isinstance(inst, (dict, list)):
                hint = "; write the value in double quotes"
            errs.append(f"{where}: expected {want}, found {type_name(inst)}{hint}")
            return
    if "const" in node and inst != node["const"]:
        errs.append(f"{where}: must be {node['const']!r}")
    if "enum" in node:
        options = cast(List[object], node["enum"])
        if inst not in options:
            errs.append(f"{where}: {inst!r} is not one of {', '.join(str(o) for o in options)}")
    if "pattern" in node and isinstance(inst, str):
        if re.search(str(node["pattern"]), inst) is None:
            errs.append(f"{where}: {inst!r} does not match {node['pattern']}")
    if "minLength" in node and isinstance(inst, str):
        if len(inst) < int(cast(int, node["minLength"])):
            errs.append(f"{where}: must not be empty")
    if isinstance(inst, dict):
        data = cast(Dict[str, object], inst)
        for req in cast(List[str], node.get("required", [])):
            if req not in data:
                errs.append(f"{req}: required field is missing")
        props = as_dict(node["properties"]) if "properties" in node else {}
        for name, sub in props.items():
            if name in data:
                _check(data[name], sub, name, errs)
        if node.get("additionalProperties") is False:
            for name in data:
                if name not in props:
                    errs.append(f"{name}: unknown field")
    for sub in cast(List[object], node.get("allOf", [])):
        _check(inst, sub, path, errs)
    if "not" in node:
        inner: List[str] = []
        _check(inst, node["not"], path, inner)
        if not inner:
            errs.append(f"{where}: matches a pattern that is not allowed")
    if "if" in node:
        cond: List[str] = []
        _check(inst, node["if"], path, cond)
        branch = "then" if not cond else "else"
        if branch in node:
            sub_errs: List[str] = []
            _check(inst, node[branch], path, sub_errs)
            why = str(node.get("description", "")).strip()
            for e in sub_errs:
                errs.append(f"{e} ({why})" if why else e)


def validate(inst: object, schema: Dict[str, object]) -> List[str]:
    errs: List[str] = []
    _check(inst, schema, "", errs)
    return errs


# ------------------------------------------------------------ settings


@dataclass
class Settings:
    """What a project fills in (SPEC.md 4.2) for these tools. `decisions` and
    `archive` are relative to `root`; `schema` is a path, or None for the embedded one."""
    root: pathlib.Path
    decisions: pathlib.Path
    archive: Optional[pathlib.Path]
    owner: Optional[str]
    schema: Optional[pathlib.Path]


_OVERRIDES: Dict[str, str] = {}


def configure(decisions: Optional[str] = None, archive: Optional[str] = None,
              owner: Optional[str] = None, schema: Optional[str] = None) -> None:
    """Set the values that win over the decisions README, as the command line
    gives them. Calling it with no arguments clears them. `archive` of `none`
    means the project has no archive folder."""
    _OVERRIDES.clear()
    for key, value in (("decisions", decisions), ("archive", archive), ("owner", owner), ("schema", schema)):
        if value is not None:
            _OVERRIDES[key] = value


def configured_decisions_dir(root: pathlib.Path) -> pathlib.Path:
    """The decisions folder relative to `root`: the configured one, else the first
    of DECISION_DIRS that exists, else the first of them."""
    if "decisions" in _OVERRIDES:
        return pathlib.Path(_OVERRIDES["decisions"])
    for rel in DECISION_DIRS:
        if (root / rel).is_dir():
            return pathlib.Path(rel)
    return pathlib.Path(DECISION_DIRS[0])


def readme_settings(folder: pathlib.Path) -> Dict[str, str]:
    """The rows of the settings table in `folder`/README.md, name (lowercase) to
    value. A missing README holds none."""
    path = folder / "README.md"
    if not path.exists():
        return {}
    rows: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        m = SETTING_ROW.match(line)
        if m is not None and m.group(2) != "" and set(m.group(1)) != {"-"}:
            rows.setdefault(m.group(1).strip().lower(), m.group(2))
    return rows


def settings(root: pathlib.Path) -> Settings:
    decisions = configured_decisions_dir(root)
    rows = readme_settings(root / decisions)
    archive_text = _OVERRIDES.get("archive", rows.get("archive folder", "none"))
    archive = None if archive_text.strip().lower() in ("none", "") else pathlib.Path(archive_text.strip())
    owner = _OVERRIDES.get("owner", rows.get("owner"))
    if "schema" in _OVERRIDES:
        schema = pathlib.Path(_OVERRIDES["schema"])
    elif (root / decisions / "adr-plus.schema.json").exists():
        schema = root / decisions / "adr-plus.schema.json"
    else:
        schema = None
    return Settings(root, decisions, archive, owner, schema)


def load_schema(root: pathlib.Path) -> Dict[str, object]:
    """The schema to check headers against: the one named on the command line, else the project's own
    `adr-plus.schema.json` in its decisions folder, else the one embedded here."""
    path = settings(root).schema
    text = EMBEDDED_SCHEMA if path is None else path.read_text(encoding="utf-8")
    return as_dict(json.loads(text))


def s2_status(word: str) -> Optional[str]:
    """The status word `word` stands for under the seven-word rule: one of the
    seven itself, or an older verdict mapped onto one; None for any other word."""
    low = word.strip().lower()
    if low in STATUS_WORDS:
        return low
    return VERDICT_MAP.get(low)


def is_yaml(text: str) -> bool:
    """Whether the file opens with a `---` line, the start of a YAML header."""
    return text.split("\n", 1)[0] == "---"


def front_matter(text: str, name: str = "") -> Tuple[Optional[Dict[str, object]], int]:
    """(the parsed YAML block, the index of the first line after its closing
    `---`), or (None, 0) when the file does not open with `---`. Values keep the
    type YAML 1.1 would give a plain scalar, so a retyped value is visible to the
    schema. A block that never closes, or that the flat parser refuses, raises
    HeaderError naming `name`."""
    if not is_yaml(text):
        return None, 0
    where = name or "(text)"
    block = split_header(text)
    if block is None:
        raise HeaderError(f"{where}: the file opens with --- but the YAML block never closes")
    try:
        entries = parse_flat(block[0])
    except YamlError as exc:
        raise HeaderError(f"{where}: {exc}") from exc
    return {e.key: e.value for e in entries}, block[1]


# The schema adr-plus 0.1.0 ships, used when the project has no adr-plus.schema.json of its own.
EMBEDDED_SCHEMA = r"""{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ADR+ header",
  "description": "The YAML block at the top of an ADR. It checks structure only: which fields exist, which are required, and the words a few of them may hold. Whether a proof names something real, or an ADR number exists, is checked by the project's own checkers (SPEC.md 5.1). Every value is a string; a value YAML would turn into a date, a number or a yes/no is written in double quotes.",
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "owner", "kind", "built", "supersedes", "amends"],
  "properties": {
    "status": {
      "description": "What was ruled, in one of seven lowercase words: proposed (drafted, waiting for a ruling), provisional (an agent had to land it first), accepted (ruled yes), partially accepted (ruled yes for part of it), superseded (replaced by a newer ADR), withdrawn (no longer in force, and nothing replaced it), rejected (proposed, ruled no). Older verdicts map onto these: ratified, adopted, ruled and landed become accepted; deprecated, retired and closed become withdrawn; nixed becomes rejected; draft and awaiting become proposed.",
      "type": "string",
      "enum": [
        "proposed", "provisional", "accepted", "partially accepted",
        "superseded", "withdrawn", "rejected"
      ]
    },
    "status-note": {
      "description": "The rest of the old Status wording, word for word: who ruled, when, and anything else the line said. A glyph or bold mark that stood before the verdict is kept at the start of this note. The separator ' — ' after the verdict is not written; any other separator is kept in front of the text (a note that begins with a space is the text exactly as it followed the verdict). When the old verdict was mapped to one of the seven words, the note holds the whole old wording from that verdict on, exactly as written, for example 'RATIFIED — by Alex 2026-09-10' beside status accepted. When an unmapped verdict is followed by ' — ' and then by one of those older words, the note starts with the dash and a space, for example '— RATIFIED BY ALEX 2026-09-10' beside status accepted.",
      "type": "string",
      "minLength": 1
    },
    "ratified-by": {
      "description": "Who ratified the decision, as the Status wording spelled the name. Absent while the status is 'proposed'.",
      "type": "string",
      "minLength": 1
    },
    "ratified-on": {
      "description": "The day it was ratified, written YYYY-MM-DD inside double quotes so YAML keeps it a string. Absent while the status is 'proposed'.",
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    },
    "owner": {
      "description": "Who owns the decision. Always present; the project settings name the value.",
      "type": "string",
      "minLength": 1
    },
    "kind": {
      "description": "architecture (how the system is built), design (what a person sees or does) or process (how the project works).",
      "type": "string",
      "enum": ["architecture", "design", "process"]
    },
    "built": {
      "description": "Whether the decision exists in the tree: landed, partial, not started, or n/a (the decision became a rule or a document).",
      "type": "string",
      "enum": ["landed", "partial", "not started", "n/a"]
    },
    "proof": {
      "description": "What shows the built part is true: a test, a check or a file that must exist (SPEC.md 1.4). For 'not started' and 'n/a' it holds the reason or the pointer to where it is queued. Required unless the decision is not started.",
      "type": "string",
      "minLength": 1
    },
    "owed": {
      "description": "What is still missing. Required when built is 'partial'.",
      "type": "string",
      "minLength": 1
    },
    "supersedes": {
      "description": "The word 'nothing', or what this ADR replaces: normally an ADR number with its section, sometimes a section of another document.",
      "type": "string",
      "minLength": 1
    },
    "amends": {
      "description": "The word 'nothing', or what this ADR changes without replacing: normally an ADR number with its section, sometimes a section of another document.",
      "type": "string",
      "minLength": 1
    },
    "consulted": {
      "description": "Who was asked before the ruling.",
      "type": "string",
      "minLength": 1
    },
    "confirmed-by": {
      "description": "Who confirmed the built part, when someone other than the builder did.",
      "type": "string",
      "minLength": 1
    },
    "revisit-when": {
      "description": "The event or date that should reopen the decision.",
      "type": "string",
      "minLength": 1
    }
  },
  "allOf": [
    {
      "description": "A decision that is landed, partial or n/a names its proof.",
      "if": {
        "properties": {"built": {"enum": ["landed", "partial", "n/a"]}},
        "required": ["built"]
      },
      "then": {"required": ["proof"]}
    },
    {
      "description": "A partial decision says what is still owed.",
      "if": {
        "properties": {"built": {"const": "partial"}},
        "required": ["built"]
      },
      "then": {"required": ["owed"]}
    },
    {
      "description": "A proposed decision has not been ratified, so it carries no ratifier and no ratification date.",
      "if": {
        "properties": {"status": {"const": "proposed"}},
        "required": ["status"]
      },
      "then": {
        "properties": {"ratified-by": false, "ratified-on": false}
      }
    }
  ]
}
"""
# --- shed:shared adrplus end ---

# --- shed:shared adr_common begin ---
# Shared parsing for adr-index.py and adr-check.py.
#
# An ADR is a Markdown file named like `ADR-001-some-slug.md` or `0001-some-slug.md` in the decisions folder.
# Four formats are understood:
#
#   adr-plus       a YAML header at the top of the file (ADR+ spec 1.3), read by the adrplus block above
#   adr-plus-bold  `**Status:**` and `**Built:**` bold lines under the title: the ADR+ header before it moved to
#                  YAML. adr-check.py fails it and names the converter.
#   madr           `status:` in YAML front matter with MADR's keys, or a `## Status` section plus MADR's option
#                  headings
#   nygard         a `## Status` section followed by its text

BUILT_VALUES = ("not started", "partial", "landed", "n/a")
# The status words of the older formats, longest first so "partially accepted" wins over "accepted".
VERDICT_WORDS = tuple(sorted(list(STATUS_WORDS) + list(VERDICT_MAP), key=len, reverse=True))
# The keys MADR 4.0 puts in front matter. A header with any other key is an ADR+ header.
MADR_KEYS = frozenset(("status", "date", "decision-makers", "consulted", "informed"))

ADR_NAME = re.compile(r"^(?:ADR-(\d+)|(\d{4}))-[^/]+\.md$", re.IGNORECASE)
TITLE = re.compile(r"^#\s+(.*)$")
TITLE_PREFIX = re.compile(r"^(?:ADR-?\d+|\d+)\s*(?:—|–|:|-|\.)\s*", re.IGNORECASE)
STATUS_LINE = re.compile(r"^\*\*Status:\*\*\s*(.*)$")
BUILT_LINE = re.compile(r"^\*\*Built:\*\*\s*(.*)$")
FRONT_STATUS = re.compile(r"^status:\s*[\"']?([^\"'\n]+)", re.IGNORECASE)
SECTION = re.compile(r"^##\s+(.*?)\s*$")


class Adr:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()
        m = ADR_NAME.match(path.name)
        self.number = int(m.group(1) or m.group(2)) if m else -1
        self.title = self._title()
        self.header: Optional[Dict[str, object]] = None
        self.header_error: Optional[str] = None
        self.format = self._format()
        self.status_line, self.status_lineno = self._status_line()
        self.built_line, self.built_lineno = self._line(BUILT_LINE)

    def _title(self) -> str:
        for line in self.lines:
            m = TITLE.match(line)
            if m:
                return TITLE_PREFIX.sub("", m.group(1)).strip()
        return self.path.stem

    def _format(self) -> str:
        if is_yaml(self.text):
            try:
                self.header, _ = front_matter(self.text, self.path.name)
            except HeaderError as exc:
                self.header_error = str(exc)
                return "adr-plus"
            if self.header is not None and set(self.header) <= MADR_KEYS:
                return "madr"
            return "adr-plus"
        if any(STATUS_LINE.match(l) for l in self.lines[:12]):
            return "adr-plus-bold"
        lowered = self.text.lower()
        if "considered options" in lowered or "decision drivers" in lowered or "decision outcome" in lowered:
            return "madr"
        return "nygard"

    def _line(self, pattern: "re.Pattern[str]") -> Tuple[Optional[str], int]:
        for i, line in enumerate(self.lines[:12]):
            m = pattern.match(line)
            if m:
                return m.group(1).strip(), i
        return None, -1

    def _status_line(self) -> Tuple[Optional[str], int]:
        if self.format == "adr-plus":
            status = (self.header or {}).get("status")
            return (status if isinstance(status, str) else None), 1
        if self.format == "adr-plus-bold":
            return self._line(STATUS_LINE)
        if self.lines and self.lines[0].strip() == "---":
            for i, line in enumerate(self.lines[1:40], start=1):
                if line.strip() == "---":
                    break
                m = FRONT_STATUS.match(line)
                if m:
                    return m.group(1).strip(), i
        for i, line in enumerate(self.lines):
            m = SECTION.match(line)
            if m and m.group(1).strip().lower() == "status":
                for j in range(i + 1, min(i + 6, len(self.lines))):
                    if self.lines[j].strip():
                        return self.lines[j].strip(), j
        return None, -1

    @property
    def status_word(self) -> str:
        """The verdict as a lowercase word: one of the seven for an ADR+ header, the mapped or raw word for the rest."""
        if not self.status_line:
            return "—"
        if self.format == "adr-plus":
            return self.status_line
        lowered = self.status_line.lower().lstrip("*_✅⚑ ")
        for word in VERDICT_WORDS:
            if lowered.startswith(word):
                return s2_status(word) or word
        return self.status_line.split()[0].strip("*_,.;:").lower() if self.status_line.split() else "—"

    @property
    def built_word(self) -> str:
        if self.format == "adr-plus":
            built = (self.header or {}).get("built")
            return built if isinstance(built, str) else "—"
        if not self.built_line:
            return "—"
        for value in BUILT_VALUES:
            if self.built_line.lower().startswith(value):
                return value
        return "?"

    @property
    def kind(self) -> str:
        kind = (self.header or {}).get("kind")
        return kind if isinstance(kind, str) else "—"


def find_decisions_dir(root: pathlib.Path, given: Optional[str]) -> Optional[pathlib.Path]:
    if given:
        return root / given
    for rel in DECISION_DIRS:
        if (root / rel).is_dir():
            return root / rel
    return None


def load_adrs(folder: pathlib.Path) -> List[Adr]:
    adrs = [Adr(p) for p in sorted(folder.iterdir()) if p.is_file() and ADR_NAME.match(p.name)]
    adrs.sort(key=lambda a: (a.number, a.path.name))
    return adrs
# --- shed:shared adr_common end ---

HEADER = """# ADR index

Generated by `adr-index.py` from the ADRs themselves. Do not edit it by hand:
change the ADR and run the script again. An ADR's own header outranks this table.

| ADR | Decision | Kind | Status | Built |
|---|---|---|---|---|
"""


def render(folder: pathlib.Path) -> str:
    rows = []
    for adr in load_adrs(folder):
        label = f"ADR-{adr.number:03d}" if adr.number >= 0 else adr.path.stem
        title = adr.title.replace("|", "\\|")
        rows.append(f"| [{label}]({adr.path.name}) | {title} | {adr.kind} | {adr.status_word} | {adr.built_word} |\n")
    return HEADER + "".join(rows)


def run(root: pathlib.Path, given: Optional[str], check: bool) -> int:
    folder = find_decisions_dir(root, given)
    if folder is None or not folder.is_dir():
        print(f"{PROG}: no decisions folder found (looked for docs/decisions, docs/adr, adr, ...); "
              "pass --dir", file=sys.stderr)
        return 2
    want = render(folder)
    index = folder / "INDEX.md"
    # Read through the text layer, so a checkout that turned line ends into CRLF still compares equal.
    have = index.read_text(encoding="utf-8") if index.exists() else None
    if check:
        if have != want:
            print(f"{PROG}: {index} is {'missing' if have is None else 'stale'}; run adr-index.py",
                  file=sys.stderr)
            return 1
        print(f"{PROG}: {index} is current")
        return 0
    with open(index, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(want)
    print(f"{PROG}: wrote {index} ({want.count(chr(10)) - HEADER.count(chr(10))} ADRs)")
    return 0


# ----------------------------------------------------------------------------------------------- self-test

def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _expect(cond: object, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _adr_plus(status: str = "accepted", kind: str = "process", built: str = "n/a", title: str = "ADR-001 — First",
              proof: str = "a rule") -> str:
    return (f"---\nstatus: {status}\nowner: the maintainers\nkind: {kind}\nbuilt: {built}\nproof: {proof}\n"
            f"supersedes: nothing\namends: nothing\n---\n# {title}\n")


def _tree(td: pathlib.Path, files: Optional[Dict[str, str]] = None, folder: str = "docs/decisions") -> pathlib.Path:
    """A project root with four ADRs in four formats (and a README that is not an ADR) under `folder`."""
    base = {
        "ADR-002-second.md": ("---\nstatus: proposed\nstatus-note: drafted\nowner: the maintainers\nkind: design\n"
                              "built: not started\nproof: queued\nsupersedes: nothing\namends: nothing\n---\n"
                              "# ADR-002 — Second | piped\n"),
        "ADR-001-first.md": _adr_plus(),
        "0003-madr-one.md": "---\nstatus: accepted\n---\n# Use MADR\n\n## Considered Options\n\n* a\n",
        "ADR-004-nygard.md": "# ADR-004 — Nygard\n\n## Status\n\nRatified\n\n## Context\n",
        "README.md": "not an ADR\n",
    }
    base.update(files or {})
    for name, text in base.items():
        _write(td / folder / name, text)
    return td


def _call(root: pathlib.Path, given: Optional[str] = None, check: bool = False) -> Tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run(root, given, check)
    return rc, out.getvalue(), err.getvalue()


def _cli(args: Sequence[str], cwd: pathlib.Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8")


def _rows(root: pathlib.Path) -> List[str]:
    text = (root / "docs" / "decisions" / "INDEX.md").read_text(encoding="utf-8")
    return [l for l in text.splitlines() if l.startswith("| [")]


def _case_missing_index_fails_check(td: pathlib.Path) -> None:
    # RED ARM: a missing index must fail --check, and --check must write nothing.
    root = _tree(td)
    rc, _, err = _call(root, check=True)
    _expect(rc == 1 and "missing" in err, f"missing index: rc {rc}, stderr {err!r}")
    _expect(not (root / "docs" / "decisions" / "INDEX.md").exists(), "--check wrote an index")


def _case_writes_one_row_per_adr_in_order(td: pathlib.Path) -> None:
    root = _tree(td)
    rc, out, _ = _call(root)
    _expect(rc == 0 and "wrote" in out and "(4 ADRs)" in out, f"write: rc {rc}, stdout {out!r}")
    rows = _rows(root)
    _expect(len(rows) == 4, f"four rows expected, got {rows}")
    _expect(rows[0].startswith("| [ADR-001](ADR-001-first.md)"), rows[0])
    _expect(rows[1].startswith("| [ADR-002]"), rows[1])
    _expect(rows[2].startswith("| [ADR-003](0003-madr-one.md)"), rows[2])
    _expect(rows[3].startswith("| [ADR-004]"), rows[3])


def _case_cells_come_from_the_header(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    rows = _rows(root)
    _expect("| First | process | accepted | n/a |" in rows[0], rows[0])
    _expect("| design | proposed | not started |" in rows[1], rows[1])


def _case_pipe_in_title_is_escaped(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    _expect("Second \\| piped" in _rows(root)[1], _rows(root)[1])


def _case_older_formats_are_mapped(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    rows = _rows(root)
    _expect("| — | accepted | — |" in rows[2], "MADR front matter: " + rows[2])
    _expect("| — | accepted | — |" in rows[3], "a Nygard ADR's Ratified is mapped to accepted: " + rows[3])


def _case_readme_is_not_an_adr(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    _expect("README" not in (root / "docs" / "decisions" / "INDEX.md").read_text(encoding="utf-8"),
            "README.md was indexed")


def _case_fresh_index_passes_check(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    rc, out, _ = _call(root, check=True)
    _expect(rc == 0 and "is current" in out, f"fresh index: rc {rc}, stdout {out!r}")


def _case_edited_status_makes_index_stale(td: pathlib.Path) -> None:
    # RED ARM: an index out of date must fail --check.
    root = _tree(td)
    _call(root)
    _write(root / "docs" / "decisions" / "ADR-001-first.md", _adr_plus(status="superseded"))
    rc, _, err = _call(root, check=True)
    _expect(rc == 1 and "stale" in err, f"edited status: rc {rc}, stderr {err!r}")


def _case_changed_kind_makes_index_stale(td: pathlib.Path) -> None:
    # RED ARM: a changed Kind cell must fail --check.
    root = _tree(td)
    _call(root)
    _write(root / "docs" / "decisions" / "ADR-001-first.md", _adr_plus(kind="design"))
    rc, _, err = _call(root, check=True)
    _expect(rc == 1 and "stale" in err, f"changed kind: rc {rc}, stderr {err!r}")


def _case_changed_built_makes_index_stale(td: pathlib.Path) -> None:
    # RED ARM: a changed Built cell must fail --check.
    root = _tree(td)
    _call(root)
    _write(root / "docs" / "decisions" / "ADR-001-first.md", _adr_plus(built="landed", proof="docs"))
    rc, _, err = _call(root, check=True)
    _expect(rc == 1 and "stale" in err, f"changed built: rc {rc}, stderr {err!r}")


def _case_no_folder_exits_2(td: pathlib.Path) -> None:
    # RED ARM: no decisions folder is exit 2, with nothing written.
    rc, _, err = _call(td)
    _expect(rc == 2 and "no decisions folder" in err, f"no folder: rc {rc}, stderr {err!r}")
    _expect(list(td.iterdir()) == [], "something was written")


def _case_dir_names_the_folder(td: pathlib.Path) -> None:
    root = _tree(td, folder="records")
    rc, _, _ = _call(root, given="records")
    _expect(rc == 0 and (root / "records" / "INDEX.md").is_file(), "--dir records did not write records/INDEX.md")
    rc, _, _ = _call(root, given="nowhere")
    _expect(rc == 2, f"--dir to a missing folder: rc {rc}")


def _case_index_has_lf_line_ends(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    data = (root / "docs" / "decisions" / "INDEX.md").read_bytes()
    _expect(b"\r" not in data and data.endswith(b"|\n"), "INDEX.md is not written with LF line ends")


def _case_crlf_checkout_still_current(td: pathlib.Path) -> None:
    root = _tree(td)
    _call(root)
    index = root / "docs" / "decisions" / "INDEX.md"
    index.write_bytes(index.read_bytes().replace(b"\n", b"\r\n"))
    rc, _, _ = _call(root, check=True)
    _expect(rc == 0, "an index with CRLF line ends should still be current")


def _case_root_defaults_to_cwd(td: pathlib.Path) -> None:
    root = _tree(td)
    r = _cli([], root)
    _expect(r.returncode == 0 and (root / "docs" / "decisions" / "INDEX.md").is_file(),
            f"no --root, run in the root: rc {r.returncode}, {r.stderr!r}")
    r = _cli(["--check"], root)
    _expect(r.returncode == 0, f"--check in the root: rc {r.returncode}, {r.stderr!r}")


def _case_never_looks_in_a_parent_folder(td: pathlib.Path) -> None:
    # RED ARM: the decisions folder of an enclosing project is not this run's; no --root means no search upward.
    _tree(td)
    sub = td / "sub" / "deeper"
    sub.mkdir(parents=True)
    r = _cli([], sub)
    _expect(r.returncode == 2 and "no decisions folder" in r.stderr, f"run below the root: rc {r.returncode}")
    _expect(not (td / "docs" / "decisions" / "INDEX.md").exists(), "an index was written in a parent folder")


_CASES: List[Tuple[str, Callable[[pathlib.Path], None]]] = [
    ("missing-index-fails-check", _case_missing_index_fails_check),
    ("writes-one-row-per-adr-in-order", _case_writes_one_row_per_adr_in_order),
    ("cells-come-from-the-header", _case_cells_come_from_the_header),
    ("pipe-in-title-is-escaped", _case_pipe_in_title_is_escaped),
    ("older-formats-are-mapped", _case_older_formats_are_mapped),
    ("readme-is-not-an-adr", _case_readme_is_not_an_adr),
    ("fresh-index-passes-check", _case_fresh_index_passes_check),
    ("edited-status-makes-index-stale", _case_edited_status_makes_index_stale),
    ("changed-kind-makes-index-stale", _case_changed_kind_makes_index_stale),
    ("changed-built-makes-index-stale", _case_changed_built_makes_index_stale),
    ("no-folder-exits-2", _case_no_folder_exits_2),
    ("dir-names-the-folder", _case_dir_names_the_folder),
    ("index-has-lf-line-ends", _case_index_has_lf_line_ends),
    ("crlf-checkout-still-current", _case_crlf_checkout_still_current),
    ("root-defaults-to-cwd", _case_root_defaults_to_cwd),
    ("never-looks-in-a-parent-folder", _case_never_looks_in_a_parent_folder),
]


def _run_self_test() -> int:
    """Run every case in its own temporary directory; print PASS/FAIL per case."""
    n_pass = n_fail = 0
    for name, fn in _CASES:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(pathlib.Path(td).resolve())
            except Exception as exc:  # a crashing case is a failing case
                print(f"FAIL {name} ({type(exc).__name__}: {exc})")
                n_fail += 1
                continue
        print(f"PASS {name}")
        n_pass += 1
    ran = n_pass + n_fail
    if n_fail:
        print(f"{PROG}: self-test FAIL -- {n_fail} of {ran} cases failed")
        return 1
    print(f"{PROG}: self-test PASS -- {n_pass} of {ran} cases passed")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="adr-index.py", description="Write INDEX.md, one table row per ADR.")
    ap.add_argument("--root", default=".", help="the repository root (default: the current directory)")
    ap.add_argument("--dir", help="the decisions folder, relative to the root (default: found by name)")
    ap.add_argument("--check", action="store_true", help="exit 1 if INDEX.md is missing or stale; write nothing")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases")
    args = ap.parse_args(argv)
    if args.self_test:
        return _run_self_test()
    return run(pathlib.Path(args.root), args.dir, args.check)


if __name__ == "__main__":
    sys.exit(main())
