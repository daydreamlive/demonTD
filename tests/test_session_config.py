"""Unit tests for session_config.build_session_config (the pure
SessionConfig builder demon_ext.py wraps)."""
from __future__ import annotations

import params as P
import session_config


def _build(**kw):
    defaults = dict(par_values={}, enabled_loras=[], lora_strengths={},
                    prompt_b="", device_id="dev-1", zstd_available=True)
    defaults.update(kw)
    pv = defaults.pop("par_values")
    return session_config.build_session_config(pv, **defaults)


def test_defaults_come_from_params_py():
    cfg = _build()
    for wire_key, _cast in session_config.PAR_BACKED_FIELDS:
        assert cfg[wire_key] == P.PARAM_BY_WIRE[wire_key].default, wire_key


def test_par_values_override_defaults():
    cfg = _build(par_values={"Vaewindow": 1.5, "Steps": 16, "Sde": 1})
    assert cfg["vae_window"] == 1.5
    assert cfg["steps"] == 16
    assert cfg["sde"] is True


def test_uncastable_par_value_falls_back_to_default():
    cfg = _build(par_values={"Depth": "not-a-number"})
    assert cfg["depth"] == P.PARAM_BY_WIRE["depth"].default


def test_zstd_toggle_controls_compression_key():
    assert "compression" not in _build(zstd_available=True)
    assert _build(zstd_available=False)["compression"] == "none"


def test_client_id_none_when_no_device_id():
    # encode_config drops None-valued keys on the wire.
    assert _build(device_id=None)["client_id"] is None
    assert _build(device_id="abc")["client_id"] == "abc"


def test_session_state_passthrough():
    cfg = _build(enabled_loras=["a", "b"],
                 lora_strengths={"a": 0.7}, prompt_b="b side")
    assert cfg["enabled_loras"] == ["a", "b"]
    assert cfg["lora_strengths"] == {"a": 0.7}
    assert cfg["prompt_b"] == "b side"
    assert cfg["use_server_fixture"] is False


def test_prompt_b_none_becomes_empty_string():
    assert _build(prompt_b=None)["prompt_b"] == ""


def test_par_names_resolve():
    names = session_config.par_names()
    assert names["vae_window"] == "Vaewindow"
    assert names["prompt"] == "Initprompt"
    for td_name in names.values():
        assert td_name in P.PARAM_BY_NAME
