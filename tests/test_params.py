"""Unit tests for the param schema."""

from __future__ import annotations

import params as P


def test_every_continuous_param_has_wire_name():
    for p in P.PARAMS:
        if p.category == "continuous":
            assert p.wire_name, f"{p.name} is continuous but has no wire_name"


def test_every_init_param_has_wire_name():
    for p in P.PARAMS:
        if p.category == "init":
            assert p.wire_name, f"{p.name} is init but has no wire_name"


def test_no_duplicate_par_names():
    names = [p.name for p in P.PARAMS]
    assert len(names) == len(set(names)), "duplicate TD par name"


def test_no_duplicate_wire_names():
    wire_names = [p.wire_name for p in P.PARAMS if p.wire_name]
    assert len(wire_names) == len(set(wire_names)), "duplicate wire name"


def test_all_param_pages_known():
    seen_pages = {p.page for p in P.PARAMS}
    for page in seen_pages:
        assert page in P.PAGES, f"page {page!r} not listed in PAGES"


def test_menu_pars_have_menu_names_and_labels():
    for p in P.PARAMS:
        if p.type == "Menu":
            assert p.menu_names, f"{p.name}: Menu without menu_names"
            assert len(p.menu_labels) == 0 or len(p.menu_labels) == len(p.menu_names)


def test_init_param_names_freezeset_matches():
    explicit = {p.name for p in P.PARAMS if p.category == "init"}
    assert explicit == P.INIT_PARAM_NAMES


def test_continuous_param_names_freezeset_matches():
    explicit = {p.name for p in P.PARAMS if p.category == "continuous"}
    assert explicit == P.CONTINUOUS_PARAM_NAMES


def test_session_config_defaults_has_all_init_keys():
    cfg = P.session_config_defaults()
    expected_keys = {p.wire_name for p in P.PARAMS if p.category == "init" and p.wire_name}
    assert set(cfg.keys()) == expected_keys


def test_continuous_defaults_has_all_continuous_keys():
    d = P.continuous_defaults()
    expected_keys = {p.wire_name for p in P.PARAMS if p.category == "continuous" and p.wire_name}
    assert set(d.keys()) == expected_keys


def test_discrete_pulse_map_targets_real_pars():
    for pulse_name in P.DISCRETE_PULSE_TO_KIND:
        assert pulse_name in P.PARAM_BY_NAME, f"discrete pulse {pulse_name} not in PARAMS"


def test_param_count_in_expected_ballpark():
    """Sanity check that the schema is roughly the size we claimed."""
    # 12 session + 11 init + ~6 prompt/lora + 24 synthesis + 10 rcfg/dcw + 5 curves + 10 sources
    assert 60 <= len(P.PARAMS) <= 120


def test_vae_window_default_matches_canonical_web_client():
    """Regression: vae_window=6.0 (vs the web client's 0.36) made param
    changes apply with multi-second lag on the post-2026-06 backend —
    the 'slow params' bug. The default and min must allow 0.36."""
    p = P.PARAM_BY_NAME["Vaewindow"]
    assert p.default == 0.36
    assert p.min is not None and p.min <= 0.36


def test_walk_window_default_matches_canonical_clients():
    """The deployed web client (public/config.json) and the VST (since
    2026-06-09) both send walk_window=true — fleet pods expect it for
    >60s sources. No effect for shorter sources (backend-gated)."""
    p = P.PARAM_BY_NAME["Walkwindow"]
    assert p.default is True
