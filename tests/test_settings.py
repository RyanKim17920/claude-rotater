"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

import pytest

from claude_rotater.exceptions import ConfigError
from claude_rotater.poll_policy import CADENCE_SETTING_KEYS, PollCadence, load_cadence
from claude_rotater.settings import (
    SETTING_GROUPS,
    SETTING_SPECS,
    atomic_write_json,
    AccountsSettings,
    AutoSwitchSettings,
    RunSettings,
    UiSettings,
    effective_settings,
    load_accounts_settings,
    load_run_settings,
    load_settings,
    load_ui_settings,
    merged_with_cli,
    save_settings,
    set_setting,
    settings_path,
    unset_setting,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "interval": None,
        "cooldown": None,
        "include_api_key_accounts": None,
        "strategy": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestLoadSettings:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_retired_keys_from_older_versions_load_cleanly(self, tmp_path: Path):
        """A settings.json written before the statusline feed (loop interval,
        active and urgent cadences) keeps working: the retired keys are
        ignored, kept in the file, and never crash a read or a write."""
        retired = {"intervalSeconds": 20, "pollActiveSeconds": 90,
                   "pollUrgentSeconds": 30}
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 85, "pollSpareSeconds": 300, **retired},
        }))
        assert load_settings(tmp_path) == AutoSwitchSettings(threshold=85.0)
        assert load_cadence(tmp_path) == PollCadence(spare_s=300.0)
        rows = {spec.dotted: value for spec, value, _ in effective_settings(tmp_path)}
        assert "autoswitch.intervalSeconds" not in rows
        assert rows["autoswitch.feedStaleSeconds"] == 600.0
        set_setting(tmp_path, "autoswitch.threshold", "80")
        save_settings(tmp_path, load_settings(tmp_path))
        section = json.loads(settings_path(tmp_path).read_text())["autoswitch"]
        assert {k: section[k] for k in retired} == retired
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.intervalSeconds", "30")

    def test_corrupt_file_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_non_object_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("[1, 2]")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_partial_section_fills_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80}})
        )
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 80.0
        assert loaded.cooldown_seconds == AutoSwitchSettings().cooldown_seconds

    def test_values_are_clamped(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {
                "threshold": 200,
                "hysteresisPct": -5,
                "unhealthyTicks": 0,
            }
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 99.9
        assert loaded.hysteresis_pct == 0.0
        assert loaded.unhealthy_ticks == 1

    def test_bad_types_fall_back_to_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {"threshold": "high", "includeApiKeyAccounts": 1}
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == AutoSwitchSettings().threshold
        assert loaded.include_api_key_accounts is True

    def test_unsupported_strategy_falls_back_to_best(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "chaos"}})
        )
        assert load_settings(tmp_path).strategy == "best"

    def test_consume_first_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "consume-first"}})
        )
        assert load_settings(tmp_path).strategy == "consume-first"

    def test_set_strategy_consume_first(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "consume-first")
        assert load_settings(tmp_path).strategy == "consume-first"


class TestSaveSettings:
    def test_roundtrip(self, tmp_path: Path):
        custom = AutoSwitchSettings(threshold=85.0, cooldown_seconds=60.0)
        save_settings(tmp_path, custom)
        assert load_settings(tmp_path) == custom

    def test_unknown_keys_survive(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_0600(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings())
        mode = stat.S_IMODE(settings_path(tmp_path).stat().st_mode)
        assert mode == 0o600


class TestUiSettings:
    def test_missing_file_defaults_to_auto(self, tmp_path: Path):
        assert load_ui_settings(tmp_path) == UiSettings(theme="auto")

    def test_reads_auto(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "auto"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_reads_light(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        assert load_ui_settings(tmp_path).theme == "light"

    def test_unknown_theme_clamps_to_default(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "purple"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_set_and_unset_ui_theme(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.theme", "light") == "light"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "ui": {"theme": "light"}}
        assert unset_setting(tmp_path, "ui.theme") is True
        assert "ui" not in json.loads(settings_path(tmp_path).read_text())

    def test_set_rejects_bad_choice(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="dark, light"):
            set_setting(tmp_path, "ui.theme", "purple")


class TestSettingSpecs:
    def test_registry_covers_every_dataclass_field(self):
        by_source: dict[str, set[str]] = {}
        for spec in SETTING_SPECS.values():
            by_source.setdefault(spec.source, set()).add(spec.field)
        assert by_source["autoswitch"] == {
            f.name for f in AutoSwitchSettings.__dataclass_fields__.values()
        }
        assert by_source["ui"] == {
            f.name for f in UiSettings.__dataclass_fields__.values()
        }
        assert by_source["cadence"] == {
            f.name for f in PollCadence.__dataclass_fields__.values()
        }
        assert by_source["run"] == {"share_default"}
        assert by_source["accounts"] == {"oauth_only"}

    def test_every_spec_group_is_a_known_section(self):
        groups = {key for key, _ in SETTING_GROUPS}
        for spec in SETTING_SPECS.values():
            assert spec.group in groups, spec.dotted

    def test_groups_are_unique_and_titled(self):
        keys = [key for key, _ in SETTING_GROUPS]
        assert len(keys) == len(set(keys))
        assert all(title for _, title in SETTING_GROUPS)

    def test_every_group_has_a_spec(self):
        # An empty section would render as a bare heading on the Config page.
        used = {spec.group for spec in SETTING_SPECS.values()}
        assert used == {key for key, _ in SETTING_GROUPS}

    def test_labels_are_present_and_unique(self):
        labels = [spec.label for spec in SETTING_SPECS.values()]
        assert all(labels)
        assert len(labels) == len(set(labels))

    def test_group_assignments(self):
        assert SETTING_SPECS["autoswitch.pollSpareSeconds"].group == "polling"
        assert "autoswitch.intervalSeconds" not in SETTING_SPECS  # retired
        assert SETTING_SPECS["ui.theme"].group == "ui"
        for key in (
            "threshold", "cooldownSeconds", "hysteresisPct", "strategy",
            "model", "includeApiKeyAccounts", "unhealthyTicks",
        ):
            assert SETTING_SPECS[f"autoswitch.{key}"].group == "autoswitch"

    def test_group_does_not_change_json_location(self):
        # `group` is display only: settings.json keeps section/json_key.
        assert SETTING_SPECS["autoswitch.pollSpareSeconds"].section == "autoswitch"

    def test_defaults_match_dataclass(self):
        sources = {
            "autoswitch": AutoSwitchSettings(),
            "ui": UiSettings(),
            "cadence": PollCadence(),
            "run": RunSettings(),
            "accounts": AccountsSettings(),
        }
        for spec in SETTING_SPECS.values():
            assert spec.default == getattr(sources[spec.source], spec.field)

    def test_cadence_specs_mirror_poll_policy(self):
        """One source of truth: cadence bounds and defaults come from
        poll_policy, and every cadence key lands on the Polling section."""
        assert set(CADENCE_SETTING_KEYS) == {
            spec.json_key for spec in SETTING_SPECS.values() if spec.source == "cadence"
        }
        for json_key, (field, kind, lo, hi) in CADENCE_SETTING_KEYS.items():
            spec = SETTING_SPECS[f"autoswitch.{json_key}"]
            assert (spec.field, spec.kind, spec.lo, spec.hi) == (field, kind, lo, hi)
            assert spec.group == "polling"
            assert spec.label
        assert SETTING_SPECS["autoswitch.pollSpareSeconds"].default == 600.0
        assert SETTING_SPECS["autoswitch.pollHourlyBudget"].default == 20
        assert SETTING_SPECS["autoswitch.feedStaleSeconds"].default == 600.0
        for retired in ("pollActiveSeconds", "pollUrgentSeconds"):
            assert f"autoswitch.{retired}" not in SETTING_SPECS

    def test_cadence_keys_do_not_leak_into_autoswitch_settings(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.feedStaleSeconds", "120")
        settings = load_settings(tmp_path)
        assert settings == AutoSwitchSettings()
        save_settings(tmp_path, settings)
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["autoswitch"]["feedStaleSeconds"] == 120.0
        assert load_cadence(tmp_path).feed_stale_s == 120.0

    def test_effective_settings_reports_cadence(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.pollHourlyBudget", "12")
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in effective_settings(tmp_path)}
        assert rows["autoswitch.pollHourlyBudget"] == (12, True)
        assert rows["autoswitch.pollSpareSeconds"] == (600.0, False)

    @pytest.mark.parametrize("key, bad", [
        ("autoswitch.feedStaleSeconds", "59"),
        ("autoswitch.pollHourlyBudget", "0"),
        ("autoswitch.pollHourlyBudget", "2.5"),
        ("autoswitch.pollSpareSeconds", "3601"),
    ])
    def test_cadence_set_rejects_out_of_range(self, tmp_path: Path, key, bad):
        with pytest.raises(ConfigError):
            set_setting(tmp_path, key, bad)


class TestSetUnsetSetting:
    def test_set_writes_minimal_file(self, tmp_path: Path):
        value = set_setting(tmp_path, "autoswitch.threshold", "80")
        assert value == 80.0
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "autoswitch": {"threshold": 80.0}}

    def test_set_int_kind_coerces_and_rejects_floats(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.unhealthyTicks", "5") == 5
        with pytest.raises(ConfigError, match="integer"):
            set_setting(tmp_path, "autoswitch.unhealthyTicks", "3.5")

    def test_set_rejects_out_of_range_without_writing(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.threshold", "200")
        assert not settings_path(tmp_path).exists()

    def test_set_rejects_unknown_key(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.bogus", "1")

    def test_set_string_kind_round_trips(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.model", "Fable") == "Fable"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["autoswitch"]["model"] == "Fable"
        assert load_settings(tmp_path).model == "Fable"

    def test_set_string_kind_rejects_empty(self, tmp_path: Path):
        with pytest.raises(ConfigError, match=r"ctrl\+r"):
            set_setting(tmp_path, "autoswitch.model", "   ")
        assert not settings_path(tmp_path).exists()

    def test_garbage_model_value_falls_back_to_none(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"model": 123}})
        )
        assert load_settings(tmp_path).model is None

    def test_set_rejects_bool_words_strictly(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "FALSE") is False
        with pytest.raises(ConfigError, match="true or false"):
            set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "falsy")

    def test_set_on_corrupt_file_raises_and_preserves_it(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            set_setting(tmp_path, "autoswitch.threshold", "80")
        assert settings_path(tmp_path).read_text() == "{not json"

    def test_unset_removes_key_and_empty_section(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "80")
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert "autoswitch" not in raw

    def test_unset_stamps_schema_version_on_unversioned_file(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"threshold": 80}})
        )
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["schemaVersion"] == 1

    def test_unset_absent_key_is_noop(self, tmp_path: Path):
        assert unset_setting(tmp_path, "autoswitch.threshold") is False
        assert not settings_path(tmp_path).exists()


class TestEffectiveSettings:
    def test_missing_file_reports_all_defaults(self, tmp_path: Path):
        rows = effective_settings(tmp_path)
        assert len(rows) == len(SETTING_SPECS)
        assert all(not is_set for _, _, is_set in rows)

    def test_presence_not_value_equality_marks_set(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "90")  # equals default
        by_key = {spec.dotted: is_set for spec, _, is_set in effective_settings(tmp_path)}
        assert by_key["autoswitch.threshold"] is True
        assert by_key["autoswitch.cooldownSeconds"] is False


class TestMergedWithCli:
    def test_no_flags_returns_settings_unchanged(self):
        base = AutoSwitchSettings(threshold=80.0)
        assert merged_with_cli(base, _args()) is base

    def test_cli_beats_settings(self):
        base = AutoSwitchSettings(threshold=80.0, cooldown_seconds=10.0)
        merged = merged_with_cli(base, _args(threshold=60.0, interval=30.0))
        assert merged.threshold == 60.0
        assert merged.cooldown_seconds == 10.0  # untouched

    def test_cli_values_are_clamped(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(threshold=1.0))
        assert merged.threshold == 50.0

    def test_boolean_override(self):
        merged = merged_with_cli(
            AutoSwitchSettings(), _args(include_api_key_accounts=True)
        )
        assert merged.include_api_key_accounts is True

    def test_model_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(model="Fable"))
        assert merged.model == "Fable"

    def test_strategy_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(strategy="consume-first"))
        assert merged.strategy == "consume-first"


class TestAtomicWriteThroughSymlink:
    """A rename does not follow links, so renaming onto a symlinked path
    detaches it and the target silently stops updating. Covers the write
    itself plus the two placement decisions it forces: the temp file goes
    beside the RESOLVED target (else EXDEV across mounts), the 0700 chmod
    stays on the directory rota owns (else it narrows — or cannot touch —
    a foreign one)."""

    def test_write_preserves_the_link_and_updates_the_target(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"
        tracked.write_text(json.dumps({"tracked": True}))
        link = live / "settings.json"
        link.symlink_to(tracked)

        atomic_write_json(link, {"written": "through"})

        assert link.is_symlink(), "the dotfiles link must survive the write"
        assert json.loads(tracked.read_text()) == {"written": "through"}

    def test_dangling_link_writes_where_it_points(self, tmp_path):
        target = tmp_path / "gone" / "settings.json"
        link = tmp_path / "settings.json"
        link.symlink_to(target)

        atomic_write_json(link, {"dangling": "ok"})

        assert link.is_symlink()
        assert json.loads(target.read_text()) == {"dangling": "ok"}

    def test_plain_file_write_unchanged(self, tmp_path):
        p = tmp_path / "settings.json"
        atomic_write_json(p, {"plain": 1})
        assert not p.is_symlink()
        assert json.loads(p.read_text()) == {"plain": 1}

    def test_temp_file_is_created_beside_the_target(self, tmp_path, monkeypatch):
        """Beside the LINK, the rename hits EXDEV whenever the target is on
        another mount — the write fails outright. Assert the placement
        directly; staging two filesystems in a unit test is not portable."""
        import tempfile
        from claude_rotater import settings as S
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)
        seen = []
        real_mkstemp = tempfile.mkstemp
        monkeypatch.setattr(
            S.tempfile, "mkstemp",
            lambda *a, **kw: (seen.append(kw.get("dir")), real_mkstemp(*a, **kw))[1],
        )

        atomic_write_json(link, {"x": 1})

        assert seen == [str(repo)], f"tmp must land beside the target, got {seen}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_hardening_stays_on_the_directory_rota_owns(self, tmp_path):
        """The 0700 belongs to rota's own dir. On the target's parent it
        would narrow a foreign directory, and raise PermissionError when
        that parent cannot be chmod'ed at all."""
        repo = tmp_path / "repo"; repo.mkdir(mode=0o755)
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)

        atomic_write_json(link, {"x": 1})

        assert (repo.stat().st_mode & 0o777) == 0o755, "foreign dir untouched"
        assert (live.stat().st_mode & 0o777) == 0o700, "our dir hardened"
        assert (tracked.stat().st_mode & 0o777) == 0o600, "file still 0600"


class TestLoginWarnDays:
    def test_default_and_clamp(self, tmp_path):
        import json as _json

        from claude_rotater.settings import load_ui_settings, settings_path

        assert load_ui_settings(tmp_path).login_warn_days == 3
        for raw, expected in ((7, 7), (999, 60), (-4, 0), ("x", 3), (True, 3), (2.9, 2)):
            settings_path(tmp_path).write_text(_json.dumps({"ui": {"loginWarnDays": raw}}))
            assert load_ui_settings(tmp_path).login_warn_days == expected, raw

    def test_bad_theme_keeps_valid_days(self, tmp_path):
        import json as _json

        from claude_rotater.settings import load_ui_settings, settings_path

        settings_path(tmp_path).write_text(_json.dumps({"ui": {"theme": "neon", "loginWarnDays": 9}}))
        ui = load_ui_settings(tmp_path)
        assert (ui.theme, ui.login_warn_days) == ("auto", 9)

    def test_set_validates_range(self, tmp_path):
        from claude_rotater.exceptions import ConfigError
        from claude_rotater.settings import load_ui_settings, set_setting

        set_setting(tmp_path, "ui.loginWarnDays", "10")
        assert load_ui_settings(tmp_path).login_warn_days == 10
        with pytest.raises(ConfigError):
            set_setting(tmp_path, "ui.loginWarnDays", "61")


class TestProfileSettings:
    """``run.shareDefault`` and ``accounts.oauthOnly``: plain bools, read
    leniently, set strictly, shown on the Config page's Profile section."""

    def test_defaults(self, tmp_path: Path):
        assert load_run_settings(tmp_path) == RunSettings(share_default=True)
        assert load_accounts_settings(tmp_path) == AccountsSettings(oauth_only=False)

    def test_specs_are_bools_in_the_profile_group(self):
        for key in ("run.shareDefault", "accounts.oauthOnly"):
            spec = SETTING_SPECS[key]
            assert spec.kind == "bool" and spec.group == "profile"
            assert spec.label and spec.help

    def test_set_round_trips_into_their_own_sections(self, tmp_path: Path):
        set_setting(tmp_path, "run.shareDefault", "no")
        set_setting(tmp_path, "accounts.oauthOnly", "yes")
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["run"] == {"shareDefault": False}
        assert raw["accounts"] == {"oauthOnly": True}
        assert load_run_settings(tmp_path).share_default is False
        assert load_accounts_settings(tmp_path).oauth_only is True

    def test_set_rejects_non_bools(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="true or false"):
            set_setting(tmp_path, "accounts.oauthOnly", "maybe")

    @pytest.mark.parametrize("bad", ["false", 0, 1, None, [True]])
    def test_non_bool_values_on_disk_read_as_default(self, tmp_path: Path, bad):
        atomic_write_json(
            settings_path(tmp_path),
            {"run": {"shareDefault": bad}, "accounts": {"oauthOnly": bad}},
        )
        assert load_run_settings(tmp_path).share_default is True
        assert load_accounts_settings(tmp_path).oauth_only is False

    def test_non_object_sections_read_as_default(self, tmp_path: Path):
        atomic_write_json(settings_path(tmp_path), {"run": "x", "accounts": [1]})
        assert load_run_settings(tmp_path) == RunSettings()
        assert load_accounts_settings(tmp_path) == AccountsSettings()

    def test_effective_settings_report_them(self, tmp_path: Path):
        set_setting(tmp_path, "accounts.oauthOnly", "true")
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in effective_settings(tmp_path)}
        assert rows["accounts.oauthOnly"] == (True, True)
        assert rows["run.shareDefault"] == (True, False)
