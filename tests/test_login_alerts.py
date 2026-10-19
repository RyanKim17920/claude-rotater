"""Header note, notification and log for logins that died or are ending."""

from __future__ import annotations

import json

import pytest

from claude_rotater import login_alerts as la
from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

# Bound at import, before conftest swaps the module attribute for a recorder.
REAL_NOTIFY = la.notify_macos

NOW = 1_800_000_000.0
DAY = 86400.0


def st(number, *, dead=None, ends_in=None, email=None):
    return la.LoginState(
        number, email or f"u{number}@x.com", dead_cause=dead,
        ends_at=None if ends_in is None else NOW + ends_in,
    )


class TestCollect:
    def test_unknown_or_far_end_says_nothing(self):
        assert la.collect([st("1"), st("2", ends_in=10 * DAY)], NOW, 3) == []

    def test_ending_within_warn_days(self):
        (alert,) = la.collect([st("3", ends_in=2 * DAY + 60)], NOW, 3)
        assert (alert.kind, alert.number) == (la.ENDING, "3")
        assert alert.message() == "#3 login ends in 2d — press l to log in again"

    def test_ended_and_dead_are_reported_even_with_warnings_off(self):
        alerts = la.collect([st("1", ends_in=-5), st("2", dead="invalid_grant")], NOW, 0)
        assert [(a.number, a.kind) for a in alerts] == [("2", la.DEAD), ("1", la.ENDED)]

    def test_warn_days_zero_silences_only_ending(self):
        assert la.collect([st("1", ends_in=3600)], NOW, 0) == []

    def test_dead_wins_over_its_end_date(self):
        (alert,) = la.collect([st("1", dead="invalid_grant", ends_in=DAY)], NOW, 3)
        assert alert.kind == la.DEAD and alert.cause == "invalid_grant"

    def test_order_is_severity_then_soonest(self):
        alerts = la.collect(
            [st("1", ends_in=2 * DAY), st("2", ends_in=3600), st("3", ends_in=-1)], NOW, 3
        )
        assert [a.number for a in alerts] == ["3", "2", "1"]


class TestHeaderNote:
    def test_empty(self):
        assert la.header_note([]) == ""

    def test_revoked_says_what_and_which_key(self):
        note = la.header_note(la.collect([st("3", dead="invalid_grant")], NOW, 3))
        assert note == "#3 login was revoked (signed in elsewhere?) — press l to log in again"

    def test_endings_are_listed_together(self):
        note = la.header_note(
            la.collect([st("3", ends_in=2 * DAY + 5), st("1", ends_in=5 * 3600 + 5)], NOW, 3)
        )
        assert note == "login ends in 5h: #1, #3 2d — press l"

    def test_failure_counts_the_rest(self):
        note = la.header_note(la.collect([st("2", dead="x"), st("1", ends_in=DAY)], NOW, 3))
        assert note.endswith("(+1 more)") and note.startswith("#2 login was revoked")

    def test_duration_never_reads_zero(self):
        assert la.duration_words(10) == "1m"


class TestStatesFromSnapshot:
    def _acc(self, number, *, sentinel=None, last_error=None, kind="oauth", disabled=False, ends=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            number=number, email=f"u{number}@x.com", kind=kind, disabled=disabled,
            login_expires_at=ends,
            usage=SimpleNamespace(sentinel=sentinel, last_error=last_error),
        )

    def test_relogin_sentinel_is_dead_with_its_cause(self):
        (s,) = la.states_from_snapshot([self._acc("2", sentinel=USAGE_RELOGIN_REQUIRED, last_error="invalid_grant")])
        assert s.dead_cause == "invalid_grant"

    def test_relogin_without_error_has_a_plain_cause(self):
        (s,) = la.states_from_snapshot([self._acc("2", sentinel=USAGE_RELOGIN_REQUIRED)])
        assert s.dead_cause == la.REFUSED_CAUSE

    def test_other_sentinels_are_not_dead(self):
        (s,) = la.states_from_snapshot([self._acc("2", sentinel="token expired", ends=NOW)])
        assert s.dead_cause is None and s.ends_at == NOW

    def test_api_key_and_parked_accounts_are_left_out(self):
        assert la.states_from_snapshot(
            [self._acc("1", kind="api_key"), self._acc("2", disabled=True)]
        ) == []


class TestNotifier:
    @pytest.fixture
    def sent(self):
        return []

    @pytest.fixture
    def notifier(self, tmp_path, sent):
        return la.LoginNotifier(tmp_path, send=lambda t, b: sent.append((t, b)) or True)

    def test_dead_is_notified_and_logged_once_a_day(self, notifier, sent, caplog):
        alerts = la.collect([st("3", dead="invalid_grant")], NOW, 3)
        with caplog.at_level("WARNING", logger="claude_rotater.login_alerts"):
            assert notifier.observe(alerts, NOW, notify=True) == alerts
        assert sent == [("rota", "#3 login was revoked (signed in elsewhere?) — press l to log in again")]
        assert "cause: invalid_grant" in caplog.text and "Account 3" in caplog.text
        assert notifier.observe(alerts, NOW + DAY - 1, notify=True) == []
        assert notifier.observe(alerts, NOW + DAY, notify=True) == alerts
        assert len(sent) == 2

    def test_state_survives_a_restart(self, tmp_path, sent):
        alerts = la.collect([st("3", dead="invalid_grant")], NOW, 3)
        la.LoginNotifier(tmp_path, send=lambda t, b: sent.append(b) or True).observe(alerts, NOW, notify=True)
        again = la.LoginNotifier(tmp_path, send=lambda t, b: sent.append(b) or True)
        assert again.observe(alerts, NOW + 60, notify=True) == []
        assert len(sent) == 1

    def test_ending_waits_for_its_last_day(self, notifier, sent):
        assert notifier.observe(la.collect([st("1", ends_in=2 * DAY)], NOW, 3), NOW, notify=True) == []
        assert sent == []
        due = notifier.observe(la.collect([st("1", ends_in=DAY - 1)], NOW, 3), NOW, notify=True)
        assert [a.kind for a in due] == [la.ENDING] and len(sent) == 1

    def test_ending_then_ended_notifies_again(self, notifier, sent):
        notifier.observe(la.collect([st("1", ends_in=3600)], NOW, 3), NOW, notify=True)
        notifier.observe(la.collect([st("1", ends_in=-1)], NOW, 3), NOW + 60, notify=True)
        assert len(sent) == 2 and "ended" in sent[1][1]

    def test_relogin_then_death_again_is_reported_at_once(self, notifier, sent):
        dead = la.collect([st("3", dead="invalid_grant")], NOW, 3)
        notifier.observe(dead, NOW, notify=True)
        notifier.observe([], NOW + 60, notify=True)  # logged in again: no alert
        notifier.observe(dead, NOW + 120, notify=True)
        assert len(sent) == 2

    def test_notifications_off_still_logs(self, notifier, sent, caplog):
        alerts = la.collect([st("3", dead="invalid_grant")], NOW, 3)
        with caplog.at_level("WARNING", logger="claude_rotater.login_alerts"):
            assert notifier.observe(alerts, NOW, notify=False) == alerts
        assert sent == [] and "Account 3" in caplog.text

    def test_corrupt_state_is_ignored(self, notifier, sent):
        notifier.path.parent.mkdir(parents=True, exist_ok=True)
        notifier.path.write_text("{not json")
        notifier.observe(la.collect([st("3", dead="x")], NOW, 3), NOW, notify=True)
        assert len(sent) == 1
        assert json.loads(notifier.path.read_text())


class TestSuiteGuard:
    def test_default_notifier_never_reaches_the_desktop(self, tmp_path, request):
        la.LoginNotifier(tmp_path).observe(la.collect([st("3", dead="x")], NOW, 3), NOW, notify=True)
        assert request.node.notifications_sent == [("rota", la.LoginAlert("3", "", la.DEAD).message())]


class TestNotifyMacos:
    def test_text_goes_as_argv_never_into_the_script(self, monkeypatch):
        calls = []
        monkeypatch.setattr(la.sys, "platform", "darwin")
        monkeypatch.setattr(la.subprocess, "run", lambda args, **kw: calls.append(args))
        assert REAL_NOTIFY("rota", 'evil" & do shell script "x')
        args = calls[0]
        assert args[0] == "osascript" and args[-2:] == ["rota", 'evil" & do shell script "x']
        assert "evil" not in args[2]

    def test_failure_is_quiet(self, monkeypatch):
        monkeypatch.setattr(la.sys, "platform", "darwin")

        def boom(*_a, **_k):
            raise FileNotFoundError("osascript")

        monkeypatch.setattr(la.subprocess, "run", boom)
        assert REAL_NOTIFY("rota", "x") is False

    def test_other_platforms_do_nothing(self, monkeypatch):
        monkeypatch.setattr(la.sys, "platform", "linux")
        monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
        assert REAL_NOTIFY("rota", "x") is False
