"""Source-text checks on src/demon_ext.py — the ONE module that can't be
imported outside TouchDesigner (it touches TD globals at class scope).

Everything import-testable lives in the other suites; these assertions
cover the wiring demon_ext.py itself must carry:

* every method named in events.EVENT_HANDLERS exists,
* the LoRA trigger-prepend pipeline is connected (the v0.2.15 bug class:
  encoder existed, catalog carried trigger words, but SendPrompt never
  injected them),
* SendPrompt refreshes tags_b mid-session (the prompt-B-blend-goes-stale
  bug class).
"""
from __future__ import annotations

import re
from pathlib import Path

import events

SRC = (Path(__file__).resolve().parent.parent / "src" / "demon_ext.py"
       ).read_text()


def _method_body(name: str) -> str:
    """Slice a method body out of the class source (up to the next
    `def` at the same indent)."""
    m = re.search(rf"\n(    def {re.escape(name)}\(.*?)(?=\n    def |\Z)",
                  SRC, re.DOTALL)
    return m.group(1) if m else ""


def test_every_event_handler_method_exists():
    missing = [meth for meth in set(events.EVENT_HANDLERS.values())
               if f"def {meth}(" not in SRC]
    assert not missing, (
        f"events.EVENT_HANDLERS names methods demon_ext.py doesn't "
        f"define: {sorted(missing)}")


def test_send_prompt_injects_lora_triggers():
    body = _method_body("SendPrompt")
    assert body, "SendPrompt method not found in demon_ext.py"
    assert ("lora_triggers.inject(" in body
            or "build_trigger_prefix(" in body), (
        "SendPrompt doesn't run prompts through lora_triggers — enabled "
        "LoRAs' trigger words never reach the text encoder")


def test_send_prompt_passes_tags_b():
    body = _method_body("SendPrompt")
    assert re.search(r"encode_prompt\([^)]*tags_b\s*=", body, re.DOTALL), (
        "SendPrompt doesn't pass tags_b= to wire.encode_prompt — runtime "
        "edits to Prompt B never reach the wire and the Promptblend "
        "slider blends a stale B side")


def test_lora_catalog_captures_trigger_word():
    body = _method_body("_apply_lora_catalog")
    assert body, "_apply_lora_catalog method not found in demon_ext.py"
    assert "primary_trigger_word" in body, (
        "_apply_lora_catalog doesn't capture metadata.primary_trigger_word "
        "— the trigger column is dead and SendPrompt has nothing to inject")


def test_params_not_streamed_before_ready():
    """The params pacer must send NOTHING until `ready`: streaming into
    the pod's 30-40s synchronous VAE encode wedges its keepalive and
    1011s the session mid-encode (the rtmg-vst stays silent until
    Streaming for the same reason). _build_params_message must short out
    on `_saw_ready`."""
    body = _method_body("_build_params_message")
    assert body, "_build_params_message not found in demon_ext.py"
    # The early-return guard must include _saw_ready (no params pre-ready).
    m = re.search(r"if not self\._connected[^\n]*:\s*\n\s*return None", body)
    assert m and "_saw_ready" in m.group(0), (
        "_build_params_message must return None until self._saw_ready — "
        "otherwise it floods the encoding pod pre-`ready` and gets 1011'd")


def test_lora_strength_is_debounced_not_streamed_raw():
    """LoRA-strength fader changes must go through the debounce
    (_lora_str_pending), NOT straight into _dirty — streaming every
    intermediate value refit-storms the pod and stalls the decode
    frontier (the 'source flash', worse in TD than the VST)."""
    # The Lorastr branch records pending, and the per-frame flush is wired.
    assert "_lora_str_pending[lora_id] = (" in SRC, (
        "Lorastr OnParChange must record a debounce-pending value")
    assert "_flush_lora_strength_debounce()" in SRC, (
        "_drain_inbound must call the LoRA-strength debounce flush")
    # And it must NOT dirty lora_str directly from the fader handler.
    assert 'self._dirty[f"lora_str_{lora_id}"] = value' not in SRC, (
        "raw lora_str streaming reintroduced — must debounce instead")


def test_build_tox_ships_every_module_demon_ext_imports():
    """Inside TD, demon_ext resolves siblings via mod('<name>'), which
    only works for modules build_tox.py synced into Text DATs. A module
    imported here but missing from SRC_FILES compiles fine in pytest and
    then fails to build the .tox (tdError: Could not find specified
    module) — the session_config/events/contract_check miss."""
    build_src = (Path(__file__).resolve().parent.parent / "build"
                 / "build_tox.py").read_text()
    m = re.search(r"SRC_FILES\s*=\s*\[([^\]]+)\]", build_src)
    assert m, "SRC_FILES list not found in build/build_tox.py"
    shipped = set(re.findall(r'"(\w+)\.py"', m.group(1)))

    imported = set(re.findall(r"_mod\('(\w+)'\)", SRC))
    missing = imported - shipped
    assert not missing, (
        f"demon_ext.py imports modules build_tox.py never syncs into the "
        f".tox: {sorted(missing)} — add them to SRC_FILES (and the "
        f"sys.modules invalidation list) in build/build_tox.py")
