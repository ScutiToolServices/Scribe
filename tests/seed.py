"""Test helper: put a token with a known value into a service's tokens.json (the real way is the admin page)."""

import json

from src.tokens import _hash


def seed_token(settings, token="tok", **extra):
    path = settings.state_dir / "tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(path.read_text("utf-8")) if path.exists() else []
    rows.append({"id": f"t{len(rows)}", "name": "test", "created": 0, "last_used": None, "hash": _hash(token), **extra})
    path.write_text(json.dumps(rows), "utf-8")
    return settings
