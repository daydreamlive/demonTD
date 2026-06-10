"""Param glide / debounce / blend pacing — VST-parity send shaping.

Why this exists
---------------
The post-2026-06 DEMON backend does expensive work per param delta:
LoRA strengths trigger a weight RE-FIT on every >0.02 change, and the
blend paths (prompt_blend / timbre_strength) mutate conditioning
multiple times per engine tick if spammed. Streaming raw per-UI-event
knob values (what demonTD did) turns one drag into a refit/conditioning
storm → the generation pipeline stalls → the loop plays stale latents
and SOURCE AUDIO BLEEDS THROUGH. rtmg-vst hit the identical failure the
day of the deploy and fixed it with exactly the shaping implemented
here (commits 32ad83f / 75035da); the web client has always shipped the
same protections (usePromptBlendSync 100 ms throttle, lora dispatcher
300 ms debounce).

What this module does (always-on, no user pars — the VST model):
  * `lora_str_<id>` — TRAILING-EDGE DEBOUNCE (300 ms quiet → ONE commit
    per gesture). Not smoothing: a glide would just stretch the refit
    storm across more 0.02 steps.
  * `prompt_blend` / `timbre_strength` — one-pole GLIDE (~250 ms) and
    their dedicated WS messages are sent at most every 40 ms
    (ThrottledSender) with a trailing flush so the final value always
    lands.
  * everything else (denoise, structure, feedback, seed, strings,
    bools, curve outputs) — passes through VERBATIM, same tick.

Threading: a GlideEngine instance is owned by the params-pacer thread
(only `step()` mutates state; DemonExt recreates the engine on Connect
for fresh first-seen state). TD-free, injected clock, unit-testable.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

# VST kLiveSendThrottleMs=40 (25 Hz) for the dedicated blend messages.
BLEND_SEND_INTERVAL_MS = 40.0
# VST one-pole glide settle (~250 ms) for blend values.
BLEND_GLIDE_MS = 250.0
# VST kLoraCommitQuietMs / web dispatcher DEBOUNCE_MS.
LORA_DEBOUNCE_MS = 300.0
# Web clients' change-significance epsilon for blend sends.
EPSILON = 1e-3
# Web TWEEN_ZERO_FLOOR: the engine does int(steps/denoise)-style math;
# a glide tail value of ~1e-17 once requested a 512-PiB allocation
# server-side. Anything this close to a zero target IS zero.
ZERO_FLOOR = 1e-6
# Convergence snap — a one-pole never arrives on its own.
CONVERGE_EPS = 1e-4

# Keys that glide (the blend paths). Everything else float passes raw —
# user decision: follow the VST (blends glide, lora debounces, the rest
# streams raw), NOT the web client's optional all-float tween.
GLIDE_KEYS = frozenset({"prompt_blend", "timbre_strength"})
DEBOUNCE_PREFIX = "lora_str_"


class GlideEngine:
    """Per-key send shaping for one session. Owned by the pacer thread."""

    def __init__(self,
                 glide_ms: float = BLEND_GLIDE_MS,
                 debounce_ms: float = LORA_DEBOUNCE_MS,
                 now: Callable[[], float] = time.monotonic):
        self._glide_tau = max(1e-3, glide_ms / 1000.0 / 3.0)  # ~95% in glide_ms
        self._debounce_s = debounce_ms / 1000.0
        self._now = now
        # glide state: key -> current value
        self._glide_v: dict[str, float] = {}
        # debounce state: key -> (committed, pending, last_change_t)
        self._deb: dict[str, tuple[float, float, float]] = {}
        self._last_step_t: float | None = None

    @staticmethod
    def policy(key: str, value: Any) -> str:
        """'debounce' | 'glide' | 'pass' for a given raw entry."""
        if key.startswith(DEBOUNCE_PREFIX):
            return "debounce"
        if key in GLIDE_KEYS and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            return "glide"
        return "pass"

    def step(self, targets: dict[str, Any],
             now: float | None = None) -> dict[str, Any]:
        """Produce the dict to actually send for this tick.

        First-seen keys snap to their target immediately (load-bearing:
        the post-ready param re-assert must go out verbatim, and a LoRA
        just enabled must not sit in a 300 ms dead zone). Keys absent
        from `targets` (LoRA disabled → filtered out upstream) drop
        their state, so a later re-add is first-seen again.
        """
        if now is None:
            now = self._now()
        dt = 0.0 if self._last_step_t is None else max(0.0,
                                                       now - self._last_step_t)
        self._last_step_t = now
        # One-pole coefficient for this tick.
        alpha = 1.0 - math.exp(-dt / self._glide_tau) if dt > 0.0 else 0.0

        out: dict[str, Any] = {}
        for key, target in targets.items():
            kind = self.policy(key, target)
            if kind == "pass":
                out[key] = target
                continue
            if kind == "glide":
                t = float(target)
                v = self._glide_v.get(key)
                if v is None:
                    v = t  # first-seen snap
                else:
                    v += (t - v) * alpha
                    if abs(t - v) < CONVERGE_EPS:
                        v = t  # convergence snap (one-pole never arrives)
                    elif t == 0.0 and abs(v) < ZERO_FLOOR:
                        v = 0.0  # zero floor (the 512-PiB linspace guard)
                self._glide_v[key] = v
                out[key] = v
                continue
            # debounce
            t = float(target)
            state = self._deb.get(key)
            if state is None:
                self._deb[key] = (t, t, now)  # first-seen commit
                out[key] = t
                continue
            committed, pending, last_change = state
            if t != pending:
                pending, last_change = t, now  # re-arm the quiet timer
            if (committed != pending
                    and now - last_change >= self._debounce_s):
                committed = pending  # ONE commit per gesture
            self._deb[key] = (committed, pending, last_change)
            out[key] = committed

        # Drop state for keys that left the target set (disabled LoRA,
        # stripped key) so a re-add is first-seen, and stale state can't
        # accumulate across a long session.
        for d in (self._glide_v, self._deb):
            for gone in [k for k in d if k not in targets]:
                del d[gone]
        return out


class ThrottledSender:
    """Min-interval + epsilon + trailing-flush gate for one dedicated
    message stream (set_prompt_blend / set_timbre_strength).

    `poll(value, now)` returns True when the caller should send `value`
    now. Guarantees: at most one send per interval; insignificant
    (<EPSILON) wiggle is suppressed; the FINAL value of a gesture is
    always sent (trailing flush — the pacer keeps polling and the
    glide's convergence snap produces a settled value). `reset()`
    forgets the last-sent value so the next poll always fires — used
    for the on-ready re-assert after (re)connects.
    """

    def __init__(self, interval_ms: float = BLEND_SEND_INTERVAL_MS,
                 epsilon: float = EPSILON,
                 now: Callable[[], float] = time.monotonic):
        self._interval_s = interval_ms / 1000.0
        self._epsilon = epsilon
        self._now = now
        self._last_sent_v: float | None = None
        self._last_sent_t: float = -1e9

    def reset(self) -> None:
        self._last_sent_v = None
        self._last_sent_t = -1e9

    def poll(self, value: float, now: float | None = None) -> bool:
        if now is None:
            now = self._now()
        if self._last_sent_v is not None \
                and abs(value - self._last_sent_v) < self._epsilon:
            return False
        if now - self._last_sent_t < self._interval_s:
            return False
        self._last_sent_v = value
        self._last_sent_t = now
        return True
