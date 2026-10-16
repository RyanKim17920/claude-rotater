"""rota never deletes credentials it did not create.

The inherited run-once migrations moved upstream's per-account backups out of
the ``claude-code`` keyring service and then deleted the source items. rota
has no users of that format, and a ``claude-code`` item on a machine running
rota belongs to someone else, so constructing a switcher must leave every
such item alone on every platform.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from claude_rotater.models import Platform
from claude_rotater.switcher import ClaudeAccountSwitcher

LEGACY_SERVICE = "claude-code"


class FakeKeyring:
    """Records every call; any delete fails the test."""

    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self, store):
        self.store = dict(store)
        self.deleted: list[tuple[str, str]] = []

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.deleted.append((service, username))
        self.store.pop((service, username), None)


def _seed(switcher: ClaudeAccountSwitcher) -> None:
    switcher._setup_directories()
    switcher._write_json(
        switcher.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {"1": {"email": "a@example.com"}},
        },
    )


@pytest.mark.parametrize("platform", [Platform.MACOS, Platform.WINDOWS, Platform.LINUX])
def test_switcher_construction_leaves_legacy_items_alone(
    temp_home, block_real_keychain, platform
):
    legacy = {
        (LEGACY_SERVICE, "account-1-a@example.com"): json.dumps({"claudeAiOauth": {}}),
        (LEGACY_SERVICE, "account-None-a@example.com"): "x",
    }
    fake = FakeKeyring(legacy)
    for (service, user), value in legacy.items():
        block_real_keychain.set_password(service, user, value)

    with (
        patch.dict(sys.modules, {"keyring": fake}),
        patch.object(Platform, "detect", return_value=platform),
    ):
        first = ClaudeAccountSwitcher()
        _seed(first)
        # A second construction is the one that would have found the seeded
        # sequence and run the migrations.
        ClaudeAccountSwitcher()

    assert fake.deleted == []
    assert fake.store == legacy
    for (service, user), value in legacy.items():
        assert block_real_keychain.get_password(service, user) == value
    assert not (first.backup_dir / ".migrations.json").exists()


def test_no_migration_module_ships():
    import importlib.util

    assert importlib.util.find_spec("claude_rotater.migrations") is None
