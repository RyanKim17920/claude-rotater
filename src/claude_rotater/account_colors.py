"""One color per account, the same in every view, session and profile.

An account's color is an index into the theme's account hues
(:attr:`claude_rotater.tui.theme.Palette.accounts`), handed out first come,
lowest free first, and kept by identity in the shared cache, so an account
two profiles hold is one color in both, and adding or removing an account
never repaints the others.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from claude_rotater.paths import get_shared_cache_dir

_logger = logging.getLogger(__name__)

STORE_FILENAME = "account_colors.json"

Identity = tuple[str, str]  # (email, organization uuid)


def identity_key(identity: Identity) -> str:
    return f"{identity[0]}|{identity[1]}"


def indices(identities: Iterable[Identity], path: Path | None = None) -> dict[Identity, int]:
    """``{identity: color index}``, giving each new identity the lowest
    index no kept identity has, and saving any it gave."""
    path = path or get_shared_cache_dir() / STORE_FILENAME
    try:
        kept = json.loads(path.read_text())
        kept = {k: int(v) for k, v in kept.items()} if isinstance(kept, dict) else {}
    except (OSError, ValueError, TypeError):
        kept = {}
    out: dict[Identity, int] = {}
    added = False
    for identity in identities:
        key = identity_key(identity)
        if key not in kept:
            taken = set(kept.values())
            kept[key] = next(i for i in range(len(taken) + 1) if i not in taken)
            added = True
        out[identity] = kept[key]
    if added:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(kept, indent=0, sort_keys=True))
            os.replace(tmp, path)
        except OSError as e:
            _logger.warning(f"could not keep account colors: {e!r}")
    return out
