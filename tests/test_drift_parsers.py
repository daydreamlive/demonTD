"""Tests for scripts/check_protocol_drift.py's wire-contract parser.

Regression for the 2026-06 silent-blindness failure: the demo repo's SDK
refactor moved/merged the protocol sources; the legacy regexes against
the new layout produced poisoned/empty sets and the checker would have
gone green while real drift (command_failed, etc.) shipped unflagged.
"""

import os
import sys

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from check_protocol_drift import parse_wire_contract  # noqa: E402


_FIXTURE = """
// AUTO-GENERATED — do not edit.
export type CommandName =
  | "params"
  | "prompt"
  | "swap_source";

export const COMMAND_NAMES: readonly CommandName[] = [
  "params",
  "prompt",
  "swap_source",
] as const;

export type EventName =
  | "init_ack"
  | "ready"
  | "command_failed";

export const EVENT_NAMES: readonly EventName[] = [
  "init_ack",
  "ready",
  "command_failed",
] as const;

export type HandshakeCommandName =
  | "upload_track";

export type HandshakeEventName =
  | "upload_ok"
  | "upload_failed";

export interface ParamsCommand {
  type: "params";
  raw: Record<string, unknown>;
  playback_pos?: number;
}

export interface SessionConfigPayload {
  sde?: boolean;
  /** doc comment line that must be skipped
   *  vae_window: not_a_field
   */
  vae_window?: number;
  prompt_b?: string | null;
  // line comment: fake_field?: number;
  [k: string]: unknown;
}
"""


def test_parses_events_including_handshake():
    c = parse_wire_contract(_FIXTURE)
    assert c is not None
    assert c["server_types"] == {"init_ack", "ready", "command_failed",
                                 "upload_ok", "upload_failed"}


def test_parses_commands_including_handshake():
    c = parse_wire_contract(_FIXTURE)
    assert c["client_types"] == {"params", "prompt", "swap_source",
                                 "upload_track"}


def test_command_payload_type_literals_do_not_poison_events():
    """The whole reason this parser exists: `type: "params"` inside a
    command payload interface must NOT land in server_types (the legacy
    TS_TYPE_LITERAL_RE swept those up)."""
    c = parse_wire_contract(_FIXTURE)
    assert "params" not in c["server_types"]


def test_session_fields_skip_comments_and_index_signature():
    c = parse_wire_contract(_FIXTURE)
    assert c["session_fields"] == {"sde", "vae_window", "prompt_b"}


def test_absent_or_unparseable_returns_none():
    assert parse_wire_contract("") is None
    assert parse_wire_contract("export const FOO = 1;") is None
    # Arrays present but empty → unusable → None (fall back to legacy).
    assert parse_wire_contract(
        "export const EVENT_NAMES = [];\n"
        "export const COMMAND_NAMES = [];") is None
