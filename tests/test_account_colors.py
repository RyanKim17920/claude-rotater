"""account_colors: one color per account identity, kept in the shared cache."""

from __future__ import annotations

import json

from claude_rotater import account_colors

A, B, C, D = ("a@x", "oa"), ("b@x", "ob"), ("c@x", ""), ("d@x", "od")


def test_new_accounts_take_the_lowest_free_index_and_keep_it(tmp_path):
    store = tmp_path / "colors.json"
    assert account_colors.indices([A, B], store) == {A: 0, B: 1}
    assert account_colors.indices([B, C], store) == {B: 1, C: 2}  # order asked never repaints
    assert account_colors.indices([C, A, B], store) == {C: 2, A: 0, B: 1}


def test_a_freed_index_is_reused_only_once_nothing_keeps_it(tmp_path):
    store = tmp_path / "colors.json"
    account_colors.indices([A, B], store)
    kept = json.loads(store.read_text())
    del kept["a@x|oa"]  # forgotten (e.g. a store cleaned by hand)
    store.write_text(json.dumps(kept))
    assert account_colors.indices([D], store) == {D: 0}


def test_two_profiles_see_one_color_for_a_shared_account(tmp_path):
    store = tmp_path / "colors.json"
    personal = account_colors.indices([A, B], store)
    work = account_colors.indices([C, B], store)  # #2 here, #1 there: identity decides
    assert personal[B] == work[B]


def test_an_unreadable_store_starts_over(tmp_path):
    store = tmp_path / "colors.json"
    store.write_text("{not json")
    assert account_colors.indices([A], store) == {A: 0}
    assert json.loads(store.read_text()) == {"a@x|oa": 0}


def test_the_default_store_is_the_shared_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ROTA_SHARED_HOME", str(tmp_path))
    account_colors.indices([A])
    assert (tmp_path / "cache" / account_colors.STORE_FILENAME).exists()
