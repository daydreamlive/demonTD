"""Tests for src/params_pacer.py + params.filter_params_for_wire."""

import json
import time

import params as P
from params_pacer import ParamsPacer
from telemetry import SmoothnessStats


# -- filter_params_for_wire ---------------------------------------------------


def test_filter_strips_server_rejected_keys():
    raw = {"denoise": 0.5, "prompt_blend": 0.3, "timbre_strength": 1.0,
           "lora_blend": 0.7}
    out = P.filter_params_for_wire(raw, frozenset())
    assert out == {"denoise": 0.5}


def test_filter_drops_non_enabled_lora_strengths():
    raw = {"lora_str_bach": 0.9, "lora_str_jazz": 0.4, "denoise": 0.1}
    out = P.filter_params_for_wire(raw, frozenset({"jazz"}))
    assert out == {"lora_str_jazz": 0.4, "denoise": 0.1}


def test_filter_returns_new_dict_never_mutates():
    raw = {"prompt_blend": 0.5, "denoise": 0.2}
    out = P.filter_params_for_wire(raw, frozenset())
    assert raw == {"prompt_blend": 0.5, "denoise": 0.2}
    assert out is not raw


def test_filter_empty_raw_is_empty_dict():
    assert P.filter_params_for_wire({}, frozenset()) == {}


# -- ParamsPacer --------------------------------------------------------------


def _pacer(messages=None, sends=None, build=None, send_ok=True, stats=None):
    sends = sends if sends is not None else []

    def default_build():
        return json.dumps({"type": "params", "raw": {}, "playback_pos": 0.0})

    def default_send(msg):
        sends.append(msg)
        return send_ok

    return ParamsPacer(
        build_message=build or default_build,
        send=default_send,
        stats=stats,
        interval_s=0.001,
        log=lambda m: None,
    ), sends


def test_tick_once_sends_built_message():
    pacer, sends = _pacer()
    assert pacer.tick_once() is True
    assert len(sends) == 1
    assert json.loads(sends[0])["type"] == "params"


def test_tick_once_skips_when_build_returns_none():
    pacer, sends = _pacer(build=lambda: None)
    assert pacer.tick_once() is False
    assert sends == []
    # No send yet → watchdog sees +inf age (restart-if-dead handles it).
    assert pacer.last_send_age() == float("inf")


def test_empty_params_message_is_still_sent():
    """An empty raw dict IS the keepalive — must not be skipped."""
    msg = json.dumps({"type": "params", "raw": {}, "playback_pos": 1.5})
    pacer, sends = _pacer(build=lambda: msg)
    assert pacer.tick_once() is True
    assert sends == [msg]


def test_fail_streak_counts_and_resets():
    state = {"ok": False}
    sends = []

    def send(msg):
        sends.append(msg)
        return state["ok"]

    pacer = ParamsPacer(build_message=lambda: "{}", send=send,
                        interval_s=0.001, log=lambda m: None)
    pacer.tick_once()
    pacer.tick_once()
    assert pacer.send_fail_streak == 2
    state["ok"] = True
    pacer.tick_once()
    assert pacer.send_fail_streak == 0


def test_last_send_age_advances_after_success():
    pacer, _ = _pacer()
    pacer.tick_once()
    age1 = pacer.last_send_age()
    time.sleep(0.01)
    assert pacer.last_send_age() > age1


def test_stats_record_send_gaps():
    stats = SmoothnessStats()
    pacer, _ = _pacer(stats=stats)
    pacer.tick_once()
    time.sleep(0.005)
    pacer.tick_once()
    snap = stats.drain()
    assert snap["params_sends"] == 2
    assert snap["params_gap_max_ms"] >= 5.0


def test_thread_lifecycle_and_steady_sends():
    pacer, sends = _pacer()
    pacer.start()
    t1 = pacer._thread
    pacer.start()  # idempotent
    assert pacer._thread is t1
    deadline = time.monotonic() + 2.0
    while len(sends) < 3 and time.monotonic() < deadline:
        time.sleep(0.002)
    pacer.stop()
    assert not pacer.is_alive
    assert len(sends) >= 3, "pacer thread never reached steady sends"


def test_pacer_survives_build_exception():
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "{}"

    sends = []
    pacer = ParamsPacer(build_message=build,
                        send=lambda m: sends.append(m) or True,
                        interval_s=0.001, log=lambda m: None)
    pacer.start()
    deadline = time.monotonic() + 2.0
    while not sends and time.monotonic() < deadline:
        time.sleep(0.002)
    pacer.stop()
    assert sends, "pacer died on a build_message exception"


def test_restart_after_stop():
    pacer, sends = _pacer()
    pacer.start()
    pacer.stop()
    assert not pacer.is_alive
    pacer.start()
    assert pacer.is_alive
    pacer.stop()


def test_vst_parity_keys_pass_through_glide():
    """steps_override (int) and method (str) are seeded into the params
    raw at ready (VST parity) — the glide layer must pass them verbatim."""
    from param_glide import GlideEngine
    eng = GlideEngine(now=lambda: 0.0)
    out = eng.step({"steps_override": 8, "method": "ode"}, now=0.0)
    assert out == {"steps_override": 8, "method": "ode"}
