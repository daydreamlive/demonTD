"""LoRA trigger-word injection — TD port of demon-public-demo's
``vendor/demon-ui/lib/loraTriggers.ts``.

Each LoRA in the server's catalog carries a
``metadata.primary_trigger_word`` — the activation word it was trained
against. For the LoRA's style to actually fire, that word has to reach
the model's text encoder. We do NOT store it in the user's ``Prompt`` /
``Promptb`` parameters (those stay the operator's clean prompt text);
instead we inject the triggers onto the WIRE at send-time.

``build_trigger_prefix`` builds the comma-joined prefix for currently
enabled LoRAs. ``SendPrompt`` in ``demon_ext.py`` prepends it to both
``tags`` and ``tags_b`` right before the WS ``prompt`` message goes out.
Callers always pass the clean prompt text; ``SendPrompt`` adds the
triggers. The prefix is computed fresh on every send, so toggling a
LoRA immediately changes what the encoder sees on the next send.

Gated on the ``Autoprependloratriggers`` toggle in ``params.py`` (default
on). With it off, the operator owns the trigger workflow manually and
``build_trigger_prefix`` returns ``""``.

All functions here are pure — they take catalog data + enabled-ids as
input, no TD globals. That keeps them unit-testable in ``pytest``.
"""

from __future__ import annotations

from typing import Iterable


def build_trigger_prefix(
    catalog_rows: Iterable[dict],
    enabled_ids: Iterable[str],
    auto_prepend: bool = True,
) -> str:
    """Return the trigger prefix for the currently-enabled LoRAs.

    Walks ``catalog_rows`` in order, picks each entry whose ``id`` is in
    ``enabled_ids``, pulls its ``trigger_word``, dedupes
    case-insensitively (preserving first-seen order), and joins with
    ``", "``. The result has a trailing ``", "`` so it can be cheaply
    concatenated ahead of a clean prompt.

    Returns ``""`` when no enabled LoRA has a non-empty trigger word, or
    when ``auto_prepend`` is ``False`` (manual workflow).

    Mirrors ``enabledLoraTriggerPrefix`` in ``loraTriggers.ts``.
    """
    if not auto_prepend:
        return ""

    enabled = {str(i) for i in enabled_ids}
    if not enabled:
        return ""

    seen: set[str] = set()
    triggers: list[str] = []
    for row in catalog_rows:
        rid = str(row.get("id", ""))
        if rid not in enabled:
            continue
        raw = row.get("trigger_word") or ""
        trimmed = str(raw).strip()
        if not trimmed:
            continue
        key = trimmed.lower()
        if key in seen:
            continue
        seen.add(key)
        triggers.append(trimmed)

    if not triggers:
        return ""
    return ", ".join(triggers) + ", "


def catalog_trigger_words(catalog_rows: Iterable[dict]) -> set[str]:
    """All known LoRA trigger words in the catalog (lowercased), enabled
    or not. The basis for stripping a trigger prefix off a prompt."""
    out: set[str] = set()
    for row in catalog_rows:
        raw = row.get("trigger_word") or ""
        trimmed = str(raw).strip().lower()
        if trimmed:
            out.add(trimmed)
    return out


def strip_leading_triggers(text: str, all_triggers: Iterable[str]) -> str:
    """Strip a leading LoRA-trigger prefix off a prompt, returning the
    operator's clean text.

    Drops leading comma-separated tokens that match any known catalog
    trigger word — ANY trigger, enabled or not, however many times it
    repeats. It is the inverse of the prefix ``build_trigger_prefix``
    builds, but resilient: it recovers the clean prompt from a stale
    prefix, a prefix for a since-disabled LoRA, or a prefix accidentally
    stacked N times. Matching is case-insensitive; the first non-trigger
    token ends the strip.

    This is the guarantee behind "a disabled LoRA's trigger is never on
    the wire, an enabled LoRA's trigger is on it exactly once": the send
    path runs the user's text through here before prepending the current
    prefix, so whatever prefix drift happened upstream is erased and
    rebuilt cleanly. Trigger words are deliberately distinctive
    activation tokens, so a clean prompt legitimately leading with one
    (then a comma) is vanishingly unlikely.

    Mirrors ``stripLeadingTriggers`` in ``loraTriggers.ts``.
    """
    if not text:
        return text
    triggers = {str(t).strip().lower() for t in all_triggers if str(t).strip()}
    if not triggers:
        return text
    parts = text.split(",")
    i = 0
    while i < len(parts) and parts[i].strip().lower() in triggers:
        i += 1
    if i == 0:
        return text
    return ",".join(parts[i:]).lstrip()


def inject(
    text: str,
    catalog_rows: Iterable[dict],
    enabled_ids: Iterable[str],
    auto_prepend: bool = True,
) -> str:
    """Convenience: ``build_trigger_prefix + stripLeadingTriggers + concat``.

    Apply at every ``prompt`` send site for both ``tags`` and ``tags_b``.
    """
    # Materialize catalog_rows once — both helpers iterate it.
    rows = list(catalog_rows)
    prefix = build_trigger_prefix(rows, enabled_ids, auto_prepend=auto_prepend)
    all_triggers = catalog_trigger_words(rows)
    clean = strip_leading_triggers(text or "", all_triggers)
    return prefix + clean
