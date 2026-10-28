"""Added-desc checkpoint advance rule."""

from __future__ import annotations

from ode_lookup_db.scraper import should_advance_checkpoint


def test_advances_after_a_clean_complete_run():
    assert should_advance_checkpoint(failed=0, unfetched=0)


def test_holds_when_limit_left_discs_unfetched():
    # --limit truncates the target list after discovery. Those discs would fall
    # below the new checkpoint, where the added-desc walk never looks again.
    assert not should_advance_checkpoint(failed=0, unfetched=900)


def test_holds_when_a_fetch_failed():
    assert not should_advance_checkpoint(failed=1, unfetched=0)


def test_holds_when_both():
    assert not should_advance_checkpoint(failed=2, unfetched=5)
