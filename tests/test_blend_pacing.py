"""Tests for src/param_glide.py ThrottledSender — the dedicated blend
message gate (set_prompt_blend / set_timbre_strength pacing).

Mirrors the web client's usePromptBlendSync throttle semantics with the
VST's 40 ms interval: at most one send per interval, sub-epsilon wiggle
suppressed, the FINAL value of a gesture always lands (trailing flush),
and reset() forces a re-send for the post-(re)connect re-assert.
"""

from param_glide import ThrottledSender


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def test_first_poll_sends():
    s = ThrottledSender(now=Clock())
    assert s.poll(0.4) is True


def test_at_most_one_send_per_interval_during_ramp():
    clock = Clock()
    s = ThrottledSender(interval_ms=40.0, now=clock)
    sends = 0
    # 16 ms pacer ticks ramping 0→1 over ~0.5 s.
    for i in range(32):
        if s.poll(i / 31.0, now=clock.t):
            sends += 1
        clock.advance(0.016)
    # 32 ticks * 16 ms = 512 ms → at most ceil(512/40)+1 = 13 sends.
    assert sends <= 13
    assert sends >= 5  # but it IS streaming, not stalled


def test_trailing_flush_lands_final_value():
    clock = Clock()
    s = ThrottledSender(interval_ms=40.0, now=clock)
    assert s.poll(0.0, now=clock.t) is True
    # Big jump immediately after a send → interval-gated (no send) ...
    clock.advance(0.016)
    assert s.poll(1.0, now=clock.t) is False
    # ... but the pacer keeps polling; once the interval elapses the
    # settled value goes out.
    clock.advance(0.040)
    assert s.poll(1.0, now=clock.t) is True


def test_epsilon_suppresses_noise():
    clock = Clock()
    s = ThrottledSender(now=clock)
    assert s.poll(0.5, now=clock.t) is True
    for _ in range(20):
        clock.advance(0.1)  # interval long elapsed
        assert s.poll(0.5 + 1e-4, now=clock.t) is False  # < epsilon


def test_reset_forces_resend_of_same_value():
    clock = Clock()
    s = ThrottledSender(now=clock)
    assert s.poll(0.4, now=clock.t) is True
    clock.advance(1.0)
    assert s.poll(0.4, now=clock.t) is False  # same value, suppressed
    s.reset()  # reconnect re-assert
    assert s.poll(0.4, now=clock.t) is True
