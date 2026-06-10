"""Tests for src/param_glide.py GlideEngine — VST-parity send shaping.

The contract: lora_str_* debounces (one commit per gesture — the pod
re-fits weights per >0.02 delta), prompt_blend/timbre_strength glide
(~250 ms one-pole), everything else passes through verbatim same-tick.
"""

import param_glide
from param_glide import GlideEngine


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def _engine(clock=None, **kw):
    clock = clock or Clock()
    return GlideEngine(now=clock, **kw), clock


# -- pass-through -------------------------------------------------------------


def test_non_blend_floats_pass_verbatim_same_tick():
    """User decision: VST model — denoise/structure/feedback stream RAW."""
    eng, clock = _engine()
    out = eng.step({"denoise": 0.85, "hint_strength": 0.3,
                    "feedback": 0.1}, now=clock.t)
    assert out == {"denoise": 0.85, "hint_strength": 0.3, "feedback": 0.1}
    clock.advance(0.016)
    out = eng.step({"denoise": 0.10}, now=clock.t)  # hard step, no glide
    assert out["denoise"] == 0.10


def test_non_float_types_pass_verbatim():
    eng, clock = _engine()
    targets = {"seed": 42, "dcw_enabled": True, "dcw_wavelet": "db4",
               "rcfg_mode": "off"}
    assert eng.step(targets, now=clock.t) == targets


# -- glide (blends) -----------------------------------------------------------


def test_blend_first_seen_snaps_immediately():
    """The post-ready re-assert must go out verbatim — no glide-from-zero."""
    eng, clock = _engine()
    out = eng.step({"prompt_blend": 0.4}, now=clock.t)
    assert out["prompt_blend"] == 0.4


def test_blend_glides_monotonically_and_arrives():
    eng, clock = _engine()
    eng.step({"prompt_blend": 0.0}, now=clock.t)
    values = []
    for _ in range(60):  # 60 x 16 ms ≈ 1 s >> 250 ms glide
        clock.advance(0.016)
        values.append(eng.step({"prompt_blend": 1.0},
                               now=clock.t)["prompt_blend"])
    assert all(b >= a for a, b in zip(values, values[1:]))
    assert 0.0 < values[0] < 1.0  # actually gliding, not stepping
    assert values[-1] == 1.0      # convergence snap → exact arrival


def test_blend_settles_within_glide_window():
    eng, clock = _engine()
    eng.step({"timbre_strength": 0.0}, now=clock.t)
    v = 0.0
    while clock.t < param_glide.BLEND_GLIDE_MS / 1000.0:
        clock.advance(0.016)
        v = eng.step({"timbre_strength": 1.0}, now=clock.t)["timbre_strength"]
    assert v > 0.93  # ~95% settled at the nominal glide time


def test_zero_floor_no_denormal_tail():
    """No emitted value may sit in (0, 1e-6) while gliding to target 0 —
    the engine's int(steps/denoise)-style math turns a 1e-17 tail into a
    giant allocation server-side."""
    eng, clock = _engine()
    eng.step({"prompt_blend": 0.5}, now=clock.t)
    saw = []
    for _ in range(400):
        clock.advance(0.016)
        saw.append(eng.step({"prompt_blend": 0.0},
                            now=clock.t)["prompt_blend"])
    assert not any(0.0 < v < 1e-6 for v in saw)
    assert saw[-1] == 0.0


# -- debounce (lora_str_*) ------------------------------------------------------


def test_lora_first_seen_commits_instantly():
    """No 300 ms dead zone right after enable_lora."""
    eng, clock = _engine()
    out = eng.step({"lora_str_bach": 0.9}, now=clock.t)
    assert out["lora_str_bach"] == 0.9


def test_lora_drag_commits_once_after_quiet():
    """A continuous drag emits the pre-drag value throughout, then ONE
    commit 300 ms after the last movement — one refit per gesture."""
    eng, clock = _engine()
    eng.step({"lora_str_x": 0.2}, now=clock.t)

    # Drag: a new value every 30 ms for ~0.6 s (well past the window —
    # each movement re-arms the quiet timer).
    v = 0.2
    emitted = set()
    for i in range(20):
        clock.advance(0.030)
        v = round(0.2 + (i + 1) * 0.035, 4)
        emitted.add(eng.step({"lora_str_x": v}, now=clock.t)["lora_str_x"])
    assert emitted == {0.2}  # nothing leaked mid-drag

    # Release: quiet for 300 ms → exactly one commit to the final value.
    clock.advance(0.299)
    assert eng.step({"lora_str_x": v}, now=clock.t)["lora_str_x"] == 0.2
    clock.advance(0.002)
    assert eng.step({"lora_str_x": v}, now=clock.t)["lora_str_x"] == v


def test_lora_disabled_then_reenabled_is_first_seen():
    eng, clock = _engine()
    eng.step({"lora_str_x": 0.5}, now=clock.t)
    clock.advance(0.016)
    eng.step({}, now=clock.t)  # disabled → filtered out → state dropped
    clock.advance(0.016)
    out = eng.step({"lora_str_x": 0.8}, now=clock.t)
    assert out["lora_str_x"] == 0.8  # instant, not debounced from 0.5


def test_lora_debounce_does_not_glide():
    """Debounced keys bypass the glide entirely — a glide would smear
    the refit storm across more >0.02 steps."""
    eng, clock = _engine()
    eng.step({"lora_str_x": 0.0}, now=clock.t)
    clock.advance(0.301)
    out = eng.step({"lora_str_x": 1.0}, now=clock.t)
    # After one change + quiet window the commit is the EXACT target —
    # no intermediate one-pole values ever.
    clock.advance(0.301)
    out = eng.step({"lora_str_x": 1.0}, now=clock.t)
    assert out["lora_str_x"] == 1.0


# -- state hygiene ---------------------------------------------------------------


def test_stale_keys_are_pruned():
    eng, clock = _engine()
    eng.step({"lora_str_a": 0.1, "prompt_blend": 0.2}, now=clock.t)
    clock.advance(0.016)
    eng.step({"denoise": 0.5}, now=clock.t)
    assert eng._deb == {}
    assert eng._glide_v == {}


def test_mixed_dict_policies_coexist():
    eng, clock = _engine()
    out = eng.step({
        "denoise": 0.85,            # pass
        "seed": 7,                  # pass
        "prompt_blend": 0.4,        # glide (first-seen snap)
        "lora_str_jazz": 0.6,       # debounce (first-seen commit)
    }, now=clock.t)
    assert out == {"denoise": 0.85, "seed": 7,
                   "prompt_blend": 0.4, "lora_str_jazz": 0.6}
