#!/usr/bin/env python3
"""Reject Korean text in first-party project files and filenames."""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
HANGUL = re.compile(
    r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af\ud7b0-\ud7ff\uffa0-\uffdc]"
)


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    paths = sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})
    violations = []
    checked = 0
    for name in paths:
        if HANGUL.search(name):
            violations.append(f"{name}: filename must be English")
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        for line_number, line in enumerate(content.splitlines(), 1):
            if HANGUL.search(line):
                # Report locations without reproducing non-English content.
                violations.append(f"{name}:{line_number}: Korean text found")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(f"English content check passed: {checked} text files, {len(paths)} filenames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
