"""Server-event dispatch table — pure, no TouchDesigner dependencies.

Maps every server->client JSON event `type` demonTD handles to the
DemonExt method that handles it (`_ev_*`, uniform signature
``(kind: str, data: dict)``). `demon_ext.py` materializes this into a
bound-method dict at __init__ and `_on_text` dispatches through it.

This table is the subject of tests/test_contract.py's
test_event_dispatch_parity: EVENT_HANDLERS ∪ the whitelist's
events_ignored must equal the vendored contract's event set, in BOTH
directions — a new server event with no handler fails, and a handler
for an event the server no longer emits also fails.
"""
from __future__ import annotations

EVENT_HANDLERS: dict[str, str] = {
    "ready":              "_ev_ready",
    "lora_catalog":       "_ev_lora_catalog",
    "params_update":      "_ev_params_update",
    "prompt_applied":     "_ev_prompt_applied",
    "swap_ready":         "_ev_swap_ready",
    "timbre_set":         "_ev_log_kind",
    "timbre_cleared":     "_ev_log_kind",
    "structure_set":      "_ev_log_kind",
    "structure_cleared":  "_ev_log_kind",
    "timbre_failed":      "_ev_server_error",
    "structure_failed":   "_ev_server_error",
    "swap_failed":        "_ev_server_error",
    "error":              "_ev_server_error",
    "command_failed":     "_ev_command_failed",
    "stem_assets":        "_ev_stem_assets",
    "stem_failed":        "_ev_stem_failed",
    "depth_applied":      "_ev_depth_applied",
    "params_echo":        "_ev_params_echo",
    "prompt_blend_echo":  "_ev_prompt_blend_echo",
}
