"""Read-only view of the unclaimed-credential stash for assertions.

Production never lists the stash: entries are adopted or retired by id.
Tests still need to see everything the stash holds, including entry files
whose manifest row was lost, so this mirrors what an operator would find
on disk.
"""

from __future__ import annotations


def unclaimed_entries(store) -> dict[str, dict]:
    """Manifest rows by id, plus orphaned entry files (no metadata)."""
    entries = dict(store._read_stash_manifest())
    for path in store._host.credentials_dir.glob(".unclaimed-*.enc"):
        entry_id = path.name[len(".unclaimed-"):-len(".enc")]
        entries.setdefault(entry_id, {"createdAt": None})
    return entries
