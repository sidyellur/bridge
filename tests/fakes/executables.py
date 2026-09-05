"""Fake ``claude``/``codex`` executables that capture argv+env and never resolve
to a real vendor binary.

``make_capture_exe`` writes a small Python script onto disk (mode 0755) that
appends a JSON record of its argv and a whitelist of env vars to a capture file,
then exits 0 (or execs a supplied inline handler). Tests point ``PATH`` or an
explicit executable path at these to assert exact launch behavior.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_CAPTURE_TEMPLATE = """#!{python}
import json, os, sys, time
rec = {{
    "name": {name!r},
    "argv": sys.argv[1:],
    "env": {{k: os.environ.get(k) for k in {watch_keys!r}}},
    "cwd": os.getcwd(),
    "ts": time.time(),
}}
with open({capture!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\\n")
{extra}
sys.exit({exit_code})
"""

DEFAULT_WATCH_KEYS = [
    "BRIDGE_SESSION_ID",
    "BRIDGE_ROUTER_SOCKET",
    "BRIDGE_ROUTER_TOKEN_PATH",
    "BRIDGE_HOME",
]


def make_capture_exe(
    directory: Path,
    name: str,
    capture: Path,
    *,
    exit_code: int = 0,
    watch_keys: list[str] | None = None,
    extra: str = "",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    script = _CAPTURE_TEMPLATE.format(
        python=sys.executable,
        name=name,
        watch_keys=watch_keys or DEFAULT_WATCH_KEYS,
        capture=str(capture),
        extra=extra,
        exit_code=exit_code,
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def read_captures(capture: Path) -> list[dict]:
    if not capture.exists():
        return []
    out = []
    for line in capture.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def prepend_path(directory: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    env["PATH"] = f"{directory}{os.pathsep}{env.get('PATH', '')}"
    return env
