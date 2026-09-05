"""User-owned aliases for Bridge session ids.

Bridge addresses are opaque UUIDs (§5 of the design), which is correct for the
wire but hostile to type. This module adds an *optional*, purely local contacts
file at ``~/.bridge/contacts.json`` mapping short names to session ids::

    {"web": "0a1b...-...-...", "infra": "9f2c...-...-..."}

Design constraints
------------------
* Post-v1 and optional. §15 lists an aliases/contacts file as OUT of v1, so
  nothing here may become load-bearing: an absent, empty, or corrupt file must
  leave routing exactly as it was, and an unknown name is passed through
  unchanged so the router's normal ``unreachable`` guardrail fires.
* The file is *user* state, not router state. The daemon never writes it and
  keeps no cache of it; it re-reads on each resolution (a few hundred bytes) so
  ``bridge alias`` takes effect immediately without restarting anything.
* A real session id always beats an alias spelled the same way, so adding an
  alias can never silently redirect an address that already resolves.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .paths import Paths

#: Alias names are short, shell-safe, and unambiguous on a command line.
ALIAS_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")

#: Anything shaped like a session id is rejected as an alias name, so an alias
#: can never shadow (or be mistaken for) a real Bridge address.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

CONTACTS_MODE = 0o600


class ContactsError(Exception):
    """A contacts file or alias name that a human needs to fix."""


def looks_like_session_id(value: str) -> bool:
    return bool(UUID_RE.match(value))


def validate_name(name: str) -> str:
    """Return ``name`` if it is a usable alias, else raise :class:`ContactsError`."""
    if not isinstance(name, str) or not ALIAS_NAME_RE.match(name):
        raise ContactsError(
            f"invalid alias name {name!r}: use 1-40 characters from A-Z a-z 0-9 . _ -"
        )
    if looks_like_session_id(name):
        raise ContactsError(f"invalid alias name {name!r}: aliases may not look like a session id")
    return name


# --- file I/O --------------------------------------------------------------


def load(paths: Paths) -> dict[str, str]:
    """Read the contacts file. Missing file -> ``{}``; malformed -> ContactsError."""
    path = paths.contacts
    if not path.exists():
        return {}
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ContactsError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContactsError(f"{path} must contain a JSON object of name -> session id")
    out: dict[str, str] = {}
    for name, session_id in raw.items():
        if not isinstance(name, str) or not isinstance(session_id, str):
            raise ContactsError(f"{path} must map string names to string session ids")
        out[name] = session_id
    return out


def _safe_load(paths: Paths) -> dict[str, str]:
    """Best-effort load for the routing path: a broken file is simply no aliases."""
    try:
        return load(paths)
    except ContactsError:
        return {}


def save(paths: Paths, mapping: dict[str, str]) -> None:
    """Write the contacts file atomically with user-only permissions."""
    for name, session_id in mapping.items():
        validate_name(name)
        if not session_id:
            raise ContactsError(f"alias {name!r} has an empty session id")
    paths.ensure()
    path = paths.contacts
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps({k: mapping[k] for k in sorted(mapping)}, indent=2) + "\n"
    tmp.write_text(payload)
    try:
        tmp.chmod(CONTACTS_MODE)
    except (PermissionError, NotImplementedError):  # pragma: no cover - platform dependent
        pass
    tmp.replace(path)
    try:
        path.chmod(CONTACTS_MODE)
    except (PermissionError, NotImplementedError):  # pragma: no cover - platform dependent
        pass


# --- resolution ------------------------------------------------------------


def resolve(paths: Paths, name_or_id: str) -> str:
    """Map an alias to its session id.

    Precedence: a string that is already a known session id wins over an alias
    of the same spelling; then aliases; then the input is returned unchanged so
    the router's ``unreachable`` guardrail produces the error, not this module.
    """
    if not name_or_id:
        return name_or_id
    mapping = _safe_load(paths)
    if name_or_id in mapping.values():
        return name_or_id
    target = mapping.get(name_or_id)
    return target if target else name_or_id


def alias_for(paths: Paths, session_id: str) -> str | None:
    """The first alias (in sorted order) pointing at ``session_id``, if any."""
    mapping = _safe_load(paths)
    for name in sorted(mapping):
        if mapping[name] == session_id:
            return name
    return None


def alias_index(paths: Paths) -> dict[str, str]:
    """``session id -> alias`` for every aliased session (first alias wins)."""
    mapping = _safe_load(paths)
    index: dict[str, str] = {}
    for name in sorted(mapping):
        index.setdefault(mapping[name], name)
    return index


def label(paths: Paths, session_id: str) -> str:
    """Human label for a target: ``alias (id)`` when an alias exists, else the id."""
    name = alias_for(paths, session_id)
    return f"{name} ({session_id})" if name else session_id


# --- mutation --------------------------------------------------------------


def set_alias(paths: Paths, name: str, session_id: str) -> dict[str, str]:
    validate_name(name)
    session_id = str(session_id).strip()
    if not session_id:
        raise ContactsError(f"alias {name!r} needs a session id")
    mapping = load(paths)
    mapping[name] = session_id
    save(paths, mapping)
    return mapping


def remove_alias(paths: Paths, name: str) -> bool:
    """Drop ``name``. Returns False when it was not present."""
    mapping = load(paths)
    if name not in mapping:
        return False
    del mapping[name]
    save(paths, mapping)
    return True


# --- CLI -------------------------------------------------------------------


def render_contacts(mapping: dict[str, str]) -> str:
    if not mapping:
        return "(no aliases; add one with `bridge alias <name> <session-id>`)"
    width = max(len(n) for n in mapping)
    return "\n".join(f"{name:<{width}}  {mapping[name]}" for name in sorted(mapping))


def cli_alias(name: str | None = None, session_id: str | None = None, *, rm: bool = False) -> int:
    paths = Paths.resolve()
    try:
        if rm:
            if not name:
                print("bridge alias --rm needs an alias name")
                return 2
            if not remove_alias(paths, name):
                print(f"no alias {name!r}")
                return 1
            print(f"removed alias {name}")
            return 0
        if name is None:
            print(render_contacts(load(paths)))
            return 0
        if session_id is None:
            mapping = load(paths)
            if name not in mapping:
                print(f"no alias {name!r}")
                return 1
            print(mapping[name])
            return 0
        set_alias(paths, name, session_id)
        print(f"{name} -> {session_id}")
        return 0
    except ContactsError as exc:
        print(str(exc))
        return 2


__all__ = [
    "ALIAS_NAME_RE",
    "ContactsError",
    "alias_for",
    "alias_index",
    "cli_alias",
    "label",
    "load",
    "looks_like_session_id",
    "remove_alias",
    "render_contacts",
    "resolve",
    "save",
    "set_alias",
    "validate_name",
]
