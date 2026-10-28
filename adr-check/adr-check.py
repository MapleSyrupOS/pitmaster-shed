#!/usr/bin/env python3
"""adr-check: check every ADR's YAML header, that each proof resolves, and that MADR's features follow their triggers.

Usage: adr-check.py [--root DIR] [--dir PATH] [--triggers-from N] [--triggers-skip N,N] [--self-test]

What it checks
    For ADRs in the ADR+ format (a YAML header, spec 1.3):
      - the header parses and passes the adr-plus schema: only known fields, `status` one of the seven lowercase
        words, `kind` present (architecture, design or process), `built` one of landed / partial / not started /
        n/a, `proof` present unless not started, `owed` present when partial, `supersedes` and `amends` present,
        strings quoted where YAML would retype them;
      - a landed or partial decision names at least one proof, and every proof named must resolve:
          `path/to/file`              the file or folder exists
          `path/to/test.py::name`     the file exists and contains the name (a test)
          #123 or a .../pull/123 URL  a pull request, looked up with `gh` when it is available
        Backticks are optional in `proof`; without them the proof is split at commas and semicolons and only the
        pieces with no spaces count as references;
      - a commit is never proof: it shows something was saved, not that it still works. A commit hash may follow
        a real proof, for history, but alone it fails;
      - the file has a "## History" section;
      - the triggers (spec 1.7), for every ADR numbered --triggers-from or higher and not named by
        --triggers-skip, fail both ways: two or more options weighed needs a Drivers list in Context, Good and
        Bad bullets under every option and "(chosen: reason)" on the first option's heading; a Drivers list with
        fewer than two options is padding.
    An ADR still in bold `**Status:**` lines fails and names the converter, adr-plus's `adr-yaml.py --apply`.
    For MADR and Nygard ADRs it checks only that a status can be found.
    For every format: a title, and no two ADRs with the same number.
    The check only confirms that a proof resolves. It does not judge whether the proof is a good one.

What it promises
    - Exit 0: no failures (warnings are allowed). Exit 1: at least one failure. Exit 2: no decisions folder.
    - A proof it cannot look up here (no `gh`) is a warning, never a failure. A URL other than a pull-request link
      is not proof at all.
    - It reads only. It never edits, creates or deletes a file.
    - It looks only below --root; it never searches the folders above it.
    - The schema is, in order: the `adr-plus.schema.json` in the project's decisions folder, else the copy of the
      adr-plus 0.1.0 schema embedded in this script.

--root DIR
    The repository root. Default: the current directory.

--dir PATH
    The decisions folder, relative to the root. Default: the first of docs/decisions, docs/adr, docs/adrs,
    doc/adr, doc/decisions, adr, decisions that exists.

--triggers-from N
    The first ADR number the trigger check judges. Default: 1.

--triggers-skip N,N
    ADR numbers the trigger check does not judge (ADRs written before the check existed).

--self-test
    Builds its own ADRs in temporary directories, prints one PASS or FAIL line per case and a verdict line, and
    exits non-zero on any failure.

Python 3.8 or newer, standard library only. macOS, Linux, Windows. The `gh` lookups need the GitHub CLI.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, cast

PROG = "adr-check"

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

# --- shed:shared adr_triggers begin ---
# The ADR+ trigger check (ADR+ spec 1.5 and 1.7): MADR's features, each written only when its trigger is true.
# Python 3.8+, standard library only. Used by adr-check.py.
#
# The trigger is that two or more options were weighed. When it is true:
#
#   * `## Context` ends with a `Drivers:` list (a line `Drivers:` or `**Drivers:**` followed by `- ` items);
#   * every option under `## Alternatives` has a `### ` heading and both a `- Good:` and a `- Bad:` bullet (the bold
#     forms `- **Good:**` and `- **Bad:**` count);
#   * the first option is the chosen one and its heading carries the reason, as in
#     `### Feature flag, off by default (chosen: merges must not wait)`. A decision that is `rejected` or
#     `withdrawn` is exempt from this rule: nothing was chosen.
#
# When the trigger is false (no option or one), a `Drivers:` list in Context is a feature without its trigger and
# fails the other way.
#
# An option is a `### ` heading inside the `## Alternatives` section, which ends at the next `## ` heading. A heading
# inside a code fence is not a heading.

NO_CHOICE_STATUSES: Tuple[str, ...] = ("rejected", "withdrawn")

FENCE = re.compile(r"^\s*(```|~~~)")
CONTEXT_HEAD = re.compile(r"^##\s+Context\b")
ALTERNATIVES_HEAD = re.compile(r"^##\s+Alternatives\b")
OPTION_HEAD = re.compile(r"^###\s+(.*\S)\s*$")
DRIVERS_LABEL = re.compile(r"^(?:\*\*)?Drivers(?::\*\*|\*\*:|:)\s*$")
BULLET = re.compile(r"^\s*[-*+]\s")
GOOD_BULLET = re.compile(r"^\s*[-*+]\s+(?:\*\*)?Good(?::\*\*|\*\*:|:)")
BAD_BULLET = re.compile(r"^\s*[-*+]\s+(?:\*\*)?Bad(?::\*\*|\*\*:|:)")
CHOSEN = re.compile(r"\(chosen:\s*[^)\s][^)]*\)")


@dataclass
class Option:
    heading: str
    lines: List[str]


def _outside_fences(lines: Sequence[str]) -> List[bool]:
    """For each line, whether it lies outside a code fence. A fence line itself counts as inside."""
    flags: List[bool] = []
    inside = False
    for line in lines:
        if FENCE.match(line):
            flags.append(False)
            inside = not inside
        else:
            flags.append(not inside)
    return flags


def section(lines: Sequence[str], head: "re.Pattern[str]") -> Optional[List[str]]:
    """The lines under the first `## ` heading that matches `head`, up to the next `## ` heading outside a code
    fence; None when no such heading exists."""
    flags = _outside_fences(lines)
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if not flags[i]:
            continue
        if start is None:
            if head.match(line):
                start = i + 1
        elif line.startswith("## "):
            return list(lines[start:i])
    return list(lines[start:]) if start is not None else None


def options(text: str) -> List[Option]:
    """The options weighed: the `### ` headings of the Alternatives section, each with the lines up to the next
    `### ` heading."""
    body = section(text.split("\n"), ALTERNATIVES_HEAD)
    if body is None:
        return []
    flags = _outside_fences(body)
    found: List[Option] = []
    for i, line in enumerate(body):
        m = OPTION_HEAD.match(line) if flags[i] else None
        if m:
            found.append(Option(m.group(1), []))
        elif found:
            found[-1].lines.append(line)
    return found


def has_drivers(text: str) -> bool:
    """Whether `## Context` holds a Drivers label followed by a `- ` list."""
    body = section(text.split("\n"), CONTEXT_HEAD)
    if body is None:
        return False
    for i, line in enumerate(body):
        if not DRIVERS_LABEL.match(line.strip()):
            continue
        for later in body[i + 1:]:
            if not later.strip():
                continue
            return bool(BULLET.match(later))
    return False


def findings(text: str, status: str) -> List[str]:
    """Every trigger rule the ADR breaks, each as one plain sentence. `status` is the ADR's `status` word."""
    opts = options(text)
    drivers = has_drivers(text)
    out: List[str] = []
    if len(opts) < 2:
        if drivers:
            out.append(f"## Context has a Drivers list but only {len(opts)} option(s) are weighed; "
                       f"a Drivers list is written only when two or more options were weighed")
        return out
    for opt in opts:
        if not any(GOOD_BULLET.match(ln) for ln in opt.lines):
            out.append(f"option `{opt.heading}` has no `- Good:` bullet")
        if not any(BAD_BULLET.match(ln) for ln in opt.lines):
            out.append(f"option `{opt.heading}` has no `- Bad:` bullet")
    if not drivers:
        out.append(f"{len(opts)} options are weighed but ## Context has no Drivers list "
                   f"(a `Drivers:` line followed by `- ` items)")
    if status not in NO_CHOICE_STATUSES and not CHOSEN.search(opts[0].heading):
        out.append(f"the first option `{opts[0].heading}` does not carry `(chosen: <reason>)` in its heading")
    return out
# --- shed:shared adr_triggers end ---

TICKED = re.compile(r"`([^`]+)`")
PR_REF = re.compile(r"(?:^|[\s(,;])#(\d+)\b|https?://\S+/pull/(\d+)")
URL = re.compile(r"https?://\S+")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")  # looks like a commit hash; never counts as proof


def check_proof(root: pathlib.Path, token: str) -> Tuple[str, str]:
    """Return ("ok" | "warn" | "fail", message) for one backticked proof."""
    if URL.match(token):
        return "warn", f"URL `{token}` not checked (no network lookups)"
    if "::" in token:
        path, name = token.split("::", 1)
        p = root / path
        if not p.is_file():
            return "fail", f"test file `{path}` does not exist"
        if name not in p.read_text(encoding="utf-8", errors="replace"):
            return "fail", f"test `{name}` not found in `{path}`"
        return "ok", ""
    if (root / token).exists():
        return "ok", ""
    return "fail", f"path `{token}` does not exist"


def check_pr(root: pathlib.Path, number: str) -> Tuple[str, str]:
    if shutil.which("gh") is None:
        return "warn", f"PR #{number} not checked (gh not installed)"
    r = subprocess.run(["gh", "pr", "view", number, "--json", "number"], cwd=str(root), capture_output=True)
    if r.returncode == 0:
        return "ok", ""
    return "warn", f"PR #{number} not confirmed (gh could not look it up)"


def proof_tokens(proof: str) -> List[str]:
    """The references a `proof` value names: its backticked pieces, else its comma- or semicolon-separated pieces
    that hold no spaces."""
    ticked = TICKED.findall(proof)
    if ticked:
        return ticked
    return [p for p in (s.strip() for s in re.split(r"[,;]", proof)) if p and " " not in p]


def field_text(header: Dict[str, object], key: str) -> str:
    value = header.get(key)
    return value if isinstance(value, str) else ""


def check_proofs(root: pathlib.Path, proof: str, built: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    tokens = proof_tokens(proof)
    # A bare hash is a commit unless a file or folder of that name exists.
    commits = [t for t in tokens if COMMIT.match(t) and not (root / t).exists()]
    # A URL counts only as a pull-request link (looked up below), never as a file or a commit page.
    links = [t for t in tokens if URL.match(t)]
    prs_in_tokens = [t for t in tokens if PR_REF.search(t) and not URL.match(t)]
    proofs = [t for t in tokens if t not in commits and t not in links and t not in prs_in_tokens]
    for t in links:
        if not PR_REF.search(t):
            out.append(("warn", f"URL `{t}` does not count as proof; only a pull-request link does"))
    prs = [a or b for a, b in PR_REF.findall(proof)]
    if commits:
        out.append(("warn", f"commit `{commits[0]}` does not count as proof; it may follow a real proof"))
    if not proofs and not prs:
        out.append(("fail", f"{built} needs a proof: a `file`, a `test.py::name` or a PR (a commit never counts)"))
    for token in proofs:
        out.append(check_proof(root, token))
    for number in prs:
        out.append(check_pr(root, number))
    return out


def check_adr(root: pathlib.Path, adr: Adr, schema: Dict[str, object],
              triggers_from: int = 1, triggers_skip: FrozenSet[int] = frozenset()) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if not any(l.startswith("# ") for l in adr.lines):
        out.append(("fail", "no '# ' title"))
    if adr.format == "adr-plus-bold":
        out.append(("fail", "the header is bold lines; ADR+ headers are YAML (spec 1.3). Convert with "
                            "adr-plus's `adr-yaml.py --apply`"))
        return out
    if adr.format == "adr-plus":
        if adr.header_error is not None:
            out.append(("fail", f"header: {adr.header_error}"))
            return out
        assert adr.header is not None
        for err in validate(adr.header, schema):
            out.append(("fail", f"header: {err}"))
        built = field_text(adr.header, "built")
        if built in ("landed", "partial"):
            out.extend(check_proofs(root, field_text(adr.header, "proof"), built))
        if not any(re.match(r"^##\s+History\b", l) for l in adr.lines):
            out.append(("fail", "no '## History' section"))
        if adr.number >= triggers_from and adr.number not in triggers_skip:
            for msg in findings(adr.text, field_text(adr.header, "status")):
                out.append(("fail", f"trigger: {msg}"))
        return [r for r in out if r[0] != "ok"]
    if adr.status_line is None:
        out.append(("fail", "no status found"))
    return out


def run(root: pathlib.Path, given: Optional[str], quiet: bool = False, triggers_from: int = 1,
        triggers_skip: FrozenSet[int] = frozenset()) -> int:
    folder = find_decisions_dir(root, given)
    if folder is None or not folder.is_dir():
        print(f"{PROG}: no decisions folder found; pass --dir", file=sys.stderr)
        return 2
    try:
        rel: Optional[str] = folder.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:  # a --dir outside the root: the schema lookup falls back to the usual folder names
        rel = None
    configure(decisions=rel)
    try:
        schema = load_schema(root)
    finally:
        configure()
    adrs = load_adrs(folder)
    failures = 0
    seen: Dict[int, str] = {}
    for adr in adrs:
        results = check_adr(root, adr, schema, triggers_from, triggers_skip)
        if adr.number >= 0:
            if adr.number in seen:
                results.append(("fail", f"number {adr.number} is also used by {seen[adr.number]}"))
            seen[adr.number] = adr.path.name
        for level, msg in results:
            failures += level == "fail"
            if not quiet:
                print(f"{level.upper():4} {adr.path.name}: {msg}")
    if not quiet:
        print(f"{PROG}: {len(adrs)} ADRs, {failures} failure(s)")
    return 1 if failures else 0


# ----------------------------------------------------------------------------------------------- self-test

LANDED = "`tests/test_x.py::test_widget`, `docs/decisions`"
TWO_OPTIONS = ("## Context\n\nWhy.\n\nDrivers:\n- speed\n\n## Alternatives\n\n"
               "### First (chosen: it is fast)\n- Good: a\n- Bad: b\n\n### Second\n- Good: c\n- Bad: d\n")
NO_DRIVERS = TWO_OPTIONS.replace("Drivers:\n- speed\n\n", "")
ONE_OPTION = "## Context\n\nWhy.\n\n## Alternatives\n\n- Nothing else was weighed.\n"


def adr_text(number: int, body: str = "", **over: str) -> str:
    """A valid ADR+ ADR as text; `over` replaces header fields (a value of "" removes the field)."""
    fields = {"status": "accepted", "owner": "the maintainers", "kind": "process", "built": "landed",
              "proof": LANDED, "supersedes": "nothing", "amends": "nothing"}
    for key, value in over.items():
        key = key.replace("_", "-")
        if value == "":
            fields.pop(key, None)
        else:
            fields[key] = value
    return (render_header(fields) + f"# ADR-{number:03d} — Good\n\n## Decision\n\nx\n\n"
            + (body + "\n" if body else "") + "## History\n\n- made\n")


GOOD = adr_text(1)
BOLD = ("# ADR-003 — Bold\n\n**Status:** ACCEPTED — agreed\n**Built:** LANDED — "
        "`tests/test_x.py::test_widget`\n**Kind:** process\n**Supersedes:** nothing. **Amends:** nothing.\n\n"
        "## History\n\n- made\n")


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _expect(cond: object, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _tree(td: pathlib.Path, files: Optional[Dict[str, str]] = None, folder: str = "docs/decisions") -> pathlib.Path:
    """A project root: a good ADR+ ADR, a MADR one and a Nygard one under `folder`, and a test file to prove against."""
    base = {
        "ADR-001-good.md": GOOD,
        "ADR-002-madr.md": "---\nstatus: proposed\n---\n# Two\n\n## Considered Options\n",
        "0007-nygard.md": "# Seven\n\n## Status\n\nAccepted\n",
    }
    base.update(files or {})
    for name, text in base.items():
        _write(td / folder / name, text)
    _write(td / "tests" / "test_x.py", "def test_widget():\n    pass\n")
    return td


def _call(root: pathlib.Path, given: Optional[str] = None, **kw: object) -> Tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run(root, given, **kw)  # type: ignore[arg-type]
    return rc, out.getvalue() + err.getvalue()


def _cli(args: Sequence[str], cwd: pathlib.Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8")


def _arm(files: Dict[str, str], rc: int, phrase: str, **kw: object) -> Callable[[pathlib.Path], None]:
    """A case: build the base tree plus `files`, run the check, and require the exit code and a phrase in the
    output (so a red arm fails for the reason it names, not for another)."""
    def case(td: pathlib.Path) -> None:
        got, out = _call(_tree(td, files), **kw)
        _expect(got == rc, f"exit {got}, wanted {rc}; output: {out.strip()!r}")
        _expect(phrase in out, f"output lacks {phrase!r}: {out.strip()!r}")
    return case


def _real_commit(root: pathlib.Path) -> str:
    """The short hash of a real commit made in `root`, or a made-up one when git is not there."""
    if shutil.which("git"):
        g = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(g + ["init", "-q"], capture_output=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "x"], capture_output=True)
        r = subprocess.run(g + ["rev-parse", "--short=10", "HEAD"], capture_output=True, text=True)
        if r.stdout.strip():
            return r.stdout.strip()
    return "abc1234def"


def _commit_arm(make: Callable[[str], Dict[str, str]], rc: int, phrase: str) -> Callable[[pathlib.Path], None]:
    def case(td: pathlib.Path) -> None:
        root = _tree(td)
        files = make(_real_commit(root))  # the commit is real and resolves, and still must not count
        for name, text in files.items():
            _write(root / "docs" / "decisions" / name, text)
        got, out = _call(root)
        _expect(got == rc, f"exit {got}, wanted {rc}; output: {out.strip()!r}")
        _expect(phrase in out, f"output lacks {phrase!r}: {out.strip()!r}")
    return case


def _project_schema_wins(td: pathlib.Path) -> None:
    # RED ARM: a schema of the project's own, in its decisions folder, outranks the embedded one.
    root = _tree(td)
    _write(root / "docs" / "decisions" / "adr-plus.schema.json", '{"type": "object", "required": ["zzz"]}\n')
    got, out = _call(root)
    _expect(got == 1 and "zzz: required field is missing" in out, f"project schema ignored: {got} {out!r}")


def _embedded_schema_refuses_older_verdict(td: pathlib.Path) -> None:
    whole = {"status": "accepted", "owner": "o", "kind": "process", "built": "n/a", "proof": "p",
             "supersedes": "nothing", "amends": "nothing"}
    _expect(not validate(whole, load_schema(td)), "the embedded schema must accept a complete header")
    _expect(validate(dict(whole, status="ratified"), load_schema(td)), "the embedded schema must refuse an older verdict")
    _expect(validate(dict(whole, built="finished"), load_schema(td)), "the embedded schema must refuse a built word")


def _no_folder_exits_2(td: pathlib.Path) -> None:
    # RED ARM: no decisions folder is exit 2.
    got, out = _call(td)
    _expect(got == 2 and "no decisions folder" in out, f"{got} {out!r}")
    got, out = _call(_tree(td), given="nowhere")
    _expect(got == 2, f"--dir to a missing folder: {got}")


def _dir_names_the_folder(td: pathlib.Path) -> None:
    root = _tree(td, {"ADR-001-good.md": adr_text(1, proof="`tests/test_x.py::test_widget`")}, folder="records")
    got, out = _call(root, given="records")
    _expect(got == 0 and "3 ADRs, 0 failure(s)" in out, f"{got} {out!r}")


def _root_defaults_to_cwd(td: pathlib.Path) -> None:
    root = _tree(td)
    r = _cli([], root)
    _expect(r.returncode == 0 and "3 ADRs, 0 failure(s)" in r.stdout, f"no --root: rc {r.returncode} {r.stderr!r}")
    r = _cli(["--root", str(root)], td.parent)
    _expect(r.returncode == 0, f"--root named: rc {r.returncode} {r.stderr!r}")


def _never_looks_in_a_parent_folder(td: pathlib.Path) -> None:
    # RED ARM: the decisions folder of an enclosing project is not this run's.
    _tree(td)
    sub = td / "sub" / "deeper"
    sub.mkdir(parents=True)
    r = _cli([], sub)
    _expect(r.returncode == 2 and "no decisions folder" in r.stderr, f"run below the root: rc {r.returncode}")


def _cli_trigger_flags(td: pathlib.Path) -> None:
    # The two flags, end to end through the argument parser: ADR-004 breaks the trigger rule.
    root = _tree(td, {"ADR-004-two.md": adr_text(4, NO_DRIVERS)})
    r = _cli([], root)
    _expect(r.returncode == 1 and "ADR-004-two.md: trigger" in r.stdout, f"no flags: rc {r.returncode} {r.stdout!r}")
    r = _cli(["--triggers-from", "5"], root)
    _expect(r.returncode == 0, f"--triggers-from 5: rc {r.returncode} {r.stdout!r}")
    r = _cli(["--triggers-skip", "4"], root)
    _expect(r.returncode == 0, f"--triggers-skip 4: rc {r.returncode} {r.stdout!r}")
    r = _cli(["--triggers-skip", "9, 4"], root)
    _expect(r.returncode == 0, f"--triggers-skip '9, 4': rc {r.returncode} {r.stdout!r}")
    r = _cli(["--triggers-skip", "9,10"], root)
    _expect(r.returncode == 1, f"--triggers-skip 9,10 must not spare ADR-004: rc {r.returncode}")


def _bad_triggers_skip_exits_2(td: pathlib.Path) -> None:
    # RED ARM: a --triggers-skip that is not numbers is a usage error, not a pass.
    r = _cli(["--triggers-skip", "four"], _tree(td))
    _expect(r.returncode == 2 and "--triggers-skip takes ADR numbers" in r.stderr, f"rc {r.returncode} {r.stderr!r}")


def _read_only(td: pathlib.Path) -> None:
    root = _tree(td, {"ADR-003-a.md": adr_text(3, proof="`no/such/file.md`")})
    before = sorted((p.name, p.read_bytes()) for p in root.rglob("*") if p.is_file())
    _call(root)
    after = sorted((p.name, p.read_bytes()) for p in root.rglob("*") if p.is_file())
    _expect(before == after, "the check changed a file")


A3 = "ADR-003-a.md"
A4 = "ADR-004-two.md"
_CASES: List[Tuple[str, Callable[[pathlib.Path], None]]] = [
    ("a-good-set-passes", _arm({}, 0, "3 ADRs, 0 failure(s)")),
    # Proofs.
    ("proof-test-name-not-in-file", _arm({A3: adr_text(3, proof="`tests/test_x.py::test_gone`, `docs/decisions`")},
                                         1, "test `test_gone` not found")),
    ("proof-path-does-not-exist", _arm({A3: adr_text(3, proof="`no/such/file.md`")}, 1,
                                       "path `no/such/file.md` does not exist")),
    ("landed-with-no-proof", _arm({A3: adr_text(3, proof="trust me")}, 1, "landed needs a proof")),
    ("partial-without-owed", _arm({A3: adr_text(3, built="partial")}, 1, "owed: required field is missing")),
    ("partial-with-owed", _arm({A3: adr_text(3, built="partial", owed="the rest")}, 0, "0 failure(s)")),
    ("proof-without-backticks", _arm({A3: adr_text(3, proof="tests/test_x.py::test_widget, docs/decisions")},
                                     0, "0 failure(s)")),
    ("unticked-proof-that-does-not-resolve", _arm({A3: adr_text(3, proof="tests/test_x.py::test_gone")}, 1,
                                                  "test `test_gone` not found")),
    ("url-alone-is-not-proof", _arm({A3: adr_text(3, proof="`https://example.org/page`")}, 1,
                                    "landed needs a proof")),
    ("pull-request-that-cannot-be-looked-up-only-warns",
     _arm({A3: adr_text(3, proof="`docs/decisions`, #123")}, 0, "0 failure(s)")),
    ("no-history-section", _arm({A3: adr_text(3).replace("## History\n\n- made\n", "")}, 1, "no '## History' section")),
    ("duplicate-number", _arm({"ADR-001-dup.md": GOOD}, 1, "number 1 is also used by")),
    ("no-title", _arm({A3: adr_text(3).replace("# ADR-003 — Good\n", "")}, 1, "no '# ' title")),
    # Header: the schema's words and fields.
    ("missing-status", _arm({A3: adr_text(3, status="")}, 1, "status: required field is missing")),
    ("bad-built-value", _arm({A3: adr_text(3, built="finished")}, 1, "'finished' is not one of landed")),
    ("older-verdict-word", _arm({A3: adr_text(3, status="ratified")}, 1, "'ratified' is not one of")),
    ("capitalised-status", _arm({A3: adr_text(3, status="Accepted")}, 1, "'Accepted' is not one of")),
    ("missing-kind", _arm({A3: adr_text(3, kind="")}, 1, "kind: required field is missing")),
    ("kind-outside-the-three", _arm({A3: adr_text(3, kind="plan")}, 1, "'plan' is not one of architecture")),
    ("missing-supersedes", _arm({A3: adr_text(3, supersedes="")}, 1, "supersedes: required field is missing")),
    ("proposed-with-a-ratifier", _arm({A3: adr_text(3, status="proposed", ratified_by="a name")}, 1,
                                      "ratified-by: not allowed here")),
    ("unknown-header-field", _arm({A3: GOOD.replace("amends: nothing\n", "amends: nothing\ndate: \"2020-01-01\"\n")},
                                  1, "date: unknown field")),
    ("retyped-value", _arm({A3: GOOD.replace("owner: the maintainers", "owner: 2020-01-01")}, 1,
                           "expected string, found a date")),
    ("header-never-closes", _arm({A3: GOOD.replace("---\n# ADR-001", "# ADR-001")}, 1, "never closes")),
    ("bold-line-header-names-the-converter", _arm({A3: BOLD}, 1, "adr-yaml.py --apply")),
    ("nygard-adr-without-a-status", _arm({"0008-bare.md": "# Eight\n"}, 1, "no status found")),
    # A commit is never proof, even a real one that resolves.
    ("landed-with-only-a-commit", _commit_arm(lambda s: {A3: adr_text(3, proof=f"`{s}`")}, 1,
                                              "landed needs a proof")),
    ("partial-with-only-a-commit", _commit_arm(
        lambda s: {A3: adr_text(3, built="partial", owed="the rest", proof=f"`{s}`")}, 1, "partial needs a proof")),
    ("landed-with-only-an-unticked-commit", _commit_arm(lambda s: {A3: adr_text(3, proof=s)}, 1,
                                                        "landed needs a proof")),
    ("landed-with-only-a-commit-url", _commit_arm(
        lambda s: {A3: adr_text(3, proof=f"`https://example.org/o/r/commit/{s}`")}, 1, "landed needs a proof")),
    ("commit-after-a-real-proof", _commit_arm(lambda s: {A3: adr_text(3, proof=f"`docs/decisions`, `{s}`")}, 0,
                                              "0 failure(s)")),
    # The triggers fail both ways.
    ("two-options-complete", _arm({A4: adr_text(4, TWO_OPTIONS)}, 0, "0 failure(s)")),
    ("two-options-without-drivers", _arm({A4: adr_text(4, NO_DRIVERS)}, 1, "has no Drivers list")),
    ("option-without-a-bad-bullet", _arm({A4: adr_text(4, TWO_OPTIONS.replace("- Bad: d\n", ""))}, 1,
                                         "has no `- Bad:` bullet")),
    ("option-without-a-good-bullet", _arm({A4: adr_text(4, TWO_OPTIONS.replace("- Good: c\n", ""))}, 1,
                                          "has no `- Good:` bullet")),
    ("first-option-without-a-chosen-reason", _arm({A4: adr_text(4, TWO_OPTIONS.replace(" (chosen: it is fast)", ""))},
                                                  1, "does not carry `(chosen:")),
    ("rejected-adr-chose-nothing", _arm(
        {A4: adr_text(4, TWO_OPTIONS.replace(" (chosen: it is fast)", ""), status="rejected")}, 0, "0 failure(s)")),
    ("one-option-without-drivers", _arm({A4: adr_text(4, ONE_OPTION)}, 0, "0 failure(s)")),
    ("drivers-on-one-option-is-padding", _arm({A4: adr_text(4, ONE_OPTION.replace("Why.\n", "Why.\n\nDrivers:\n- speed\n"))},
                                              1, "Drivers list but only 0 option(s)")),
    ("triggers-from-reaches-the-adr", _arm({A4: adr_text(4, NO_DRIVERS)}, 1, "trigger:", triggers_from=4)),
    ("adr-before-triggers-from-is-not-judged", _arm({A4: adr_text(4, NO_DRIVERS)}, 0, "0 failure(s)",
                                                    triggers_from=5)),
    ("triggers-skip-spares-the-named-adr", _arm({A4: adr_text(4, NO_DRIVERS)}, 0, "0 failure(s)",
                                                triggers_skip=frozenset({4}))),
    ("triggers-skip-spares-only-the-named-adr", _arm({A4: adr_text(4, NO_DRIVERS)}, 1, "trigger:",
                                                     triggers_skip=frozenset({5}))),
    # Where it looks, and what it reads.
    ("project-schema-outranks-the-embedded-one", _project_schema_wins),
    ("embedded-schema-judges-a-header", _embedded_schema_refuses_older_verdict),
    ("no-folder-exits-2", _no_folder_exits_2),
    ("dir-names-the-folder", _dir_names_the_folder),
    ("root-defaults-to-cwd", _root_defaults_to_cwd),
    ("never-looks-in-a-parent-folder", _never_looks_in_a_parent_folder),
    ("trigger-flags-end-to-end", _cli_trigger_flags),
    ("bad-triggers-skip-exits-2", _bad_triggers_skip_exits_2),
    ("never-edits-a-file", _read_only),
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
    ap = argparse.ArgumentParser(prog="adr-check.py", description="Check every ADR's header, proofs and triggers.")
    ap.add_argument("--root", default=".", help="the repository root (default: the current directory)")
    ap.add_argument("--dir", help="the decisions folder, relative to the root (default: found by name)")
    ap.add_argument("--triggers-from", type=int, default=1, metavar="N",
                    help="the first ADR number the trigger check judges (default 1)")
    ap.add_argument("--triggers-skip", default="", metavar="N,N",
                    help="ADR numbers the trigger check does not judge (ADRs written before the check existed)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases")
    args = ap.parse_args(argv)
    if args.self_test:
        return _run_self_test()
    try:
        skip = frozenset(int(n) for n in args.triggers_skip.split(",") if n.strip())
    except ValueError:
        ap.error("--triggers-skip takes ADR numbers separated by commas")
    return run(pathlib.Path(args.root), args.dir, triggers_from=args.triggers_from, triggers_skip=skip)


if __name__ == "__main__":
    sys.exit(main())
