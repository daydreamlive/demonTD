"""Tests for src/lora_triggers.py — LoRA trigger word injection.

Mirrors the canonical behaviour in demon-public-demo's
``vendor/demon-ui/lib/loraTriggers.ts``. Any drift here vs there is a
bug — the model only fires a LoRA's style when its
``primary_trigger_word`` reaches the text encoder, so the prefix has to
be on the wire for every send.
"""

from __future__ import annotations

import pytest

from lora_triggers import (
    build_trigger_prefix,
    catalog_trigger_words,
    inject,
    strip_leading_triggers,
)


CATALOG = [
    {"id": "acid", "name": "Acid", "trigger_word": "acidcore"},
    {"id": "vapor", "name": "Vapor", "trigger_word": "vaporwave"},
    {"id": "no_trig", "name": "No Trigger", "trigger_word": ""},
    {"id": "missing_field", "name": "Missing"},  # no trigger_word at all
    {"id": "dup", "name": "Dup", "trigger_word": "Acidcore"},  # case dup of "acid"
]


# ---------------------------------------------------------------------
# build_trigger_prefix
# ---------------------------------------------------------------------

def test_build_prefix_empty_when_no_enabled():
    assert build_trigger_prefix(CATALOG, enabled_ids=[]) == ""


def test_build_prefix_empty_when_enabled_lora_has_no_trigger():
    assert build_trigger_prefix(CATALOG, enabled_ids={"no_trig"}) == ""
    assert build_trigger_prefix(CATALOG, enabled_ids={"missing_field"}) == ""


def test_build_prefix_single_enabled():
    assert build_trigger_prefix(CATALOG, enabled_ids={"acid"}) == "acidcore, "


def test_build_prefix_multiple_enabled_preserves_catalog_order():
    # Catalog order is acid, vapor — order respected even if enabled set
    # is iterated differently (it's a set).
    assert (
        build_trigger_prefix(CATALOG, enabled_ids={"vapor", "acid"})
        == "acidcore, vaporwave, "
    )


def test_build_prefix_dedupes_case_insensitive():
    # "acid" → "acidcore", "dup" → "Acidcore" — same word, different case.
    # Should appear once, in first-seen casing.
    result = build_trigger_prefix(CATALOG, enabled_ids={"acid", "dup"})
    assert result == "acidcore, "


def test_build_prefix_gated_by_auto_prepend():
    # auto_prepend=False → "" even with enabled LoRAs and triggers.
    assert build_trigger_prefix(
        CATALOG, enabled_ids={"acid"}, auto_prepend=False
    ) == ""


def test_build_prefix_ignores_unknown_enabled_ids():
    # Enabled id with no catalog match → silently ignored.
    assert build_trigger_prefix(CATALOG, enabled_ids={"ghost"}) == ""


# ---------------------------------------------------------------------
# catalog_trigger_words
# ---------------------------------------------------------------------

def test_catalog_trigger_words_lowercases_and_dedupes():
    # Two entries spell "Acidcore" / "acidcore" — set contains only the lower form once.
    words = catalog_trigger_words(CATALOG)
    assert "acidcore" in words
    assert "vaporwave" in words
    assert "" not in words  # blanks filtered


def test_catalog_trigger_words_handles_missing_field():
    assert catalog_trigger_words([{"id": "x"}]) == set()


# ---------------------------------------------------------------------
# strip_leading_triggers
# ---------------------------------------------------------------------

ALL_TRIGS = {"acidcore", "vaporwave"}


def test_strip_empty_text_returns_empty():
    assert strip_leading_triggers("", ALL_TRIGS) == ""


def test_strip_no_match_returns_unchanged():
    assert strip_leading_triggers("ambient pad", ALL_TRIGS) == "ambient pad"


def test_strip_one_leading_trigger():
    # The canonical behaviour: the trigger token plus its trailing comma
    # is consumed, leaving the operator's clean text (with leading
    # whitespace trimmed).
    assert strip_leading_triggers("acidcore, dreamy pad", ALL_TRIGS) == "dreamy pad"


def test_strip_two_leading_triggers():
    assert (
        strip_leading_triggers("acidcore, vaporwave, ambient pad", ALL_TRIGS)
        == "ambient pad"
    )


def test_strip_case_insensitive():
    assert strip_leading_triggers("AcIdCoRe, dreamy pad", ALL_TRIGS) == "dreamy pad"


def test_strip_repeated_trigger():
    # Accidentally stacked prefix from upstream drift.
    assert (
        strip_leading_triggers("acidcore, acidcore, dreamy pad", ALL_TRIGS)
        == "dreamy pad"
    )


def test_strip_stops_at_first_non_trigger():
    # vaporwave appears AFTER a non-trigger token — it must stay.
    assert (
        strip_leading_triggers("acidcore, dreamy, vaporwave", ALL_TRIGS)
        == "dreamy, vaporwave"
    )


def test_strip_no_triggers_set_returns_unchanged():
    assert strip_leading_triggers("acidcore, x", set()) == "acidcore, x"


# ---------------------------------------------------------------------
# inject — the full convenience pipeline
# ---------------------------------------------------------------------

def test_inject_clean_prompt_with_one_lora():
    result = inject("dreamy pad", CATALOG, enabled_ids={"acid"})
    assert result == "acidcore, dreamy pad"


def test_inject_strips_stale_prefix_then_rebuilds():
    # User's prompt accidentally has a stale prefix for a now-disabled
    # LoRA. Inject erases it cleanly before prepending the current prefix.
    result = inject("vaporwave, dreamy pad", CATALOG, enabled_ids={"acid"})
    assert result == "acidcore, dreamy pad"


def test_inject_no_double_prepend_on_re_send():
    # First send adds the prefix. If we accidentally re-inject the
    # output, it must NOT double-prepend.
    first = inject("dreamy pad", CATALOG, enabled_ids={"acid"})
    again = inject(first, CATALOG, enabled_ids={"acid"})
    assert first == again == "acidcore, dreamy pad"


def test_inject_auto_prepend_off_strips_but_does_not_prepend():
    # Even with auto_prepend off, the strip half still runs — so the
    # operator can pick up their clean prompt after toggling off.
    result = inject(
        "acidcore, dreamy pad", CATALOG, enabled_ids={"acid"}, auto_prepend=False
    )
    assert result == "dreamy pad"


def test_inject_empty_prompt_returns_just_prefix():
    # An empty prompt with a LoRA enabled → just the trigger prefix (no
    # trailing user text). Wire still receives the trigger.
    result = inject("", CATALOG, enabled_ids={"acid"})
    assert result == "acidcore, "
