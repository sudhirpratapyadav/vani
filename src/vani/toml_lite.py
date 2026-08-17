"""TOML reader with no dependencies.

Uses the stdlib `tomllib` when it exists (Python 3.11+). On older interpreters
— Ubuntu 22.04 still ships 3.10 — it falls back to the small parser below,
which covers the subset vani's config file uses: comments, `[tables]` (dotted
names included), strings, numbers, booleans, and arrays of scalars written on
one line or spread over several.

Anything outside that subset (inline tables, arrays of tables, multi-line
strings, dates) raises TomlError instead of being quietly misread.
"""
from __future__ import annotations

from typing import Any

try:  # Python 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    _tomllib = None


class TomlError(ValueError):
    """The config file could not be parsed."""


def loads(text: str) -> dict[str, Any]:
    if _tomllib is not None:
        try:
            return _tomllib.loads(text)
        except _tomllib.TOMLDecodeError as exc:
            raise TomlError(str(exc)) from None
    return _loads_fallback(text)


# --------------------------------------------------------------------------
# Fallback parser


def _loads_fallback(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    table = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        lineno = i + 1
        line = _strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue

        if line.startswith("["):
            if line.startswith("[[") or not line.endswith("]"):
                raise TomlError(f"line {lineno}: unsupported table header {line!r}")
            table = root
            for part in line[1:-1].split("."):
                part = part.strip().strip('"').strip("'")
                if not part:
                    raise TomlError(f"line {lineno}: empty table name")
                nxt = table.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise TomlError(f"line {lineno}: {part!r} is already a value")
                table = nxt
            continue

        key, sep, raw = line.partition("=")
        if not sep:
            raise TomlError(f"line {lineno}: expected key = value, got {line!r}")
        # An array may continue on the following lines until the brackets close.
        while raw.count("[") > raw.count("]"):
            if i >= len(lines):
                raise TomlError(f"line {lineno}: unterminated array")
            raw += " " + _strip_comment(lines[i]).strip()
            i += 1
        table[key.strip().strip('"').strip("'")] = _value(raw.strip(), lineno)
    return root


def _strip_comment(line: str) -> str:
    """Drop a trailing `# comment`, ignoring '#' inside quoted strings."""
    quote = ""
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:idx]
    return line


def _value(raw: str, lineno: int) -> Any:
    if not raw:
        raise TomlError(f"line {lineno}: missing value")
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise TomlError(f"line {lineno}: unterminated array")
        return [_value(item, lineno) for item in _split_items(raw[1:-1], lineno)]
    if raw.startswith("{"):
        raise TomlError(f"line {lineno}: inline tables are not supported")
    if raw[0] in "\"'":
        if len(raw) < 2 or raw[-1] != raw[0]:
            raise TomlError(f"line {lineno}: unterminated string {raw!r}")
        body = raw[1:-1]
        return _unescape(body) if raw[0] == '"' else body
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw, 10)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        raise TomlError(f"line {lineno}: cannot parse value {raw!r}") from None


def _split_items(body: str, lineno: int) -> list[str]:
    items, cur, quote, depth = [], "", "", 0
    for ch in body:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch == "[":
            depth, cur = depth + 1, cur + ch
        elif ch == "]":
            depth, cur = depth - 1, cur + ch
        elif ch == "," and depth == 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if quote:
        raise TomlError(f"line {lineno}: unterminated string in array")
    if cur.strip():
        items.append(cur.strip())
    return [item for item in items if item]


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}


def _unescape(body: str) -> str:
    if "\\" not in body:
        return body
    out, it = "", iter(range(len(body)))
    skip = False
    for idx in it:
        if skip:
            skip = False
            continue
        ch = body[idx]
        if ch == "\\" and idx + 1 < len(body):
            out += _ESCAPES.get(body[idx + 1], body[idx + 1])
            skip = True
        else:
            out += ch
    return out
