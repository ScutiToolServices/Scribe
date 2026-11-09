"""Fails when a shared admin file drifts from a sibling tool service.

Every tool service keeps byte-identical copies of the files in SHARED, except
the lines matched by ALLOWED. Run from any repo: python tools/check_sync.py
"""
import difflib
import re
import sys
from pathlib import Path

SIBLINGS = ("Bard", "Scribe", "Reader", "Lens", "Mimic")
SHARED = ("src/admin_api.py", "src/tokens.py", "src/middleware.py", "web/index.html", "web/playground.js",
          "tests/test_admin_api.py", "tests/test_tokens.py", "tests/seed.py", "tools/check_sync.py")
ALLOWED = re.compile(r"^(PREFIX = |const CONFIG = )")  # per-project: token prefix and page config


def lines(path: Path) -> list[str]:
    return [("<per-project line>" if ALLOWED.match(line) else line) for line in path.read_text("utf-8").splitlines()]


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    drift = False
    for sibling in SIBLINGS:
        other = here.parent / sibling
        if sibling == here.name or not other.is_dir():
            continue
        for name in SHARED:
            diff = list(difflib.unified_diff(lines(here / name), lines(other / name), here.name, other.name, lineterm="", n=0))
            if diff:
                drift = True
                print(f"{name} differs from {sibling}:", *diff[:40], sep="\n")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
