"""Tests for the connection-generation event filter (demon_ext).

Regression for the stale-WSClient-event bug: an old recv thread that
outlives WSClient.close()'s 2 s join could enqueue a late ("close", ...)
event which, processed against the NEW session, set _connected=False and
ran close handling (spurious teardown / failover loop). Events are now
stamped with the connection generation and dropped when stale.
"""

import demon_ext


def test_current_generation_events_are_processed():
    assert demon_ext.DemonExt.event_is_stale(3, 3) is False


def test_stale_generation_events_are_dropped():
    assert demon_ext.DemonExt.event_is_stale(2, 3) is True


def test_non_connection_events_always_processed():
    # gen=None marks events that aren't connection-scoped: heartbeat
    # results, failover ticks, loop-initialized.
    assert demon_ext.DemonExt.event_is_stale(None, 3) is False
    assert demon_ext.DemonExt.event_is_stale(None, 0) is False


def test_future_generation_also_dropped():
    # Shouldn't happen (gen only increments on the main thread), but
    # "matches current" is the only pass condition.
    assert demon_ext.DemonExt.event_is_stale(4, 3) is True
