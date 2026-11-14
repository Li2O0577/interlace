"""One canonical text form of a definition, and its digest. Two equal definitions always hash the same."""
from __future__ import annotations

import hashlib
import json


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    """SHA-256 of the canonical form. ``value`` must be plain data: dict, list, str, int, float, bool, None."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
