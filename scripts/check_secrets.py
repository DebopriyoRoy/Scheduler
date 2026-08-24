#!/usr/bin/env python3
"""Fail when common sensitive files or populated Square tokens are tracked by Git."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
TOKEN_PATTERN = re.compile(
    r"^SQUARE_(?:SANDBOX|PRODUCTION)_ACCESS_TOKEN\s*=\s*([^\s#]+)",
    re.MULTILINE,
)


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def main() -> int:
    tracked = [Path(path) for path in git_output("ls-files").splitlines()]
    problems: list[str] = []

    for path in tracked:
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"Sensitive file is tracked: {path}")
            continue
        absolute_path = ROOT / path
        try:
            content = absolute_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if TOKEN_PATTERN.search(content):
            problems.append(f"Populated Square access token assignment found in: {path}")

    if git_output("log", "--all", "--format=%H", "--", ".env").strip():
        problems.append(".env appears in Git history")

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1

    print("Tracked-secret check: PASS")
    print(f"Tracked files inspected: {len(tracked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

