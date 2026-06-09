"""Unit tests for src/wire.py — encode/decode round-trips."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

import wire


# -----------------------------------------------------------------------------
# Audio frame
# -----------------------------------------------------------------------------
def test_encode_audio_frame_mono_to_stereo():
    pcm = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    out = wire.encode_audio_frame(pcm, channels=2)
    channels, num_samples = struct.unpack_from("<II", out, 0)
    assert channels == 2
    assert num_samples == 4
    body = np.frombuffer(out[8:], dtype=np.float32)
    # Interleaved: L0,R0,L1,R1,...
    np.testing.assert_allclose(body, [0.1, 0.1, 0.2, 0.2, 0.3, 0.3, 0.4, 0.4],
                               rtol=1e-6)


def test_encode_audio_frame_stereo_passthrough():
    pcm = np.array([[1.0, 2.0, 3.0],
                    [4.0, 5.0, 6.0]], dtype=np.float32)  # (channels, samples)
    out = wire.encode_audio_frame(pcm, channels=2)
    channels, num_samples = struct.unpack_from("<II", out, 0)
    assert channels == 2
    assert num_samples == 3
    body = np.frombuffer(out[8:], dtype=np.float32)
    # Interleaved L,R per sample
    assert body.tolist() == [1.0, 4.0, 2.0, 5.0, 3.0, 6.0]


# -----------------------------------------------------------------------------
# JSON encoders
# -----------------------------------------------------------------------------
def test_encode_config_drops_none_keeps_empty_strings():
    # Empty strings (fixture_name: "") must survive — DEMON requires them.
    # None values are dropped (those are missing-optional keys).
    msg = json.loads(wire.encode_config({"steps": 8, "prompt": "", "depth": None,
                                         "sde": False}))
    assert msg == {"steps": 8, "prompt": "", "sde": False}


def test_encode_params_shape():
    # playback_pos is in SECONDS, matching demon-public-demo's
    # useParamSync (player.positionSec). Server uses absolute time.
    msg = json.loads(wire.encode_params({"denoise": 0.5, "seed": 0.1}, 24.0))
    assert msg == {"type": "params", "raw": {"denoise": 0.5, "seed": 0.1},
                   "playback_pos": 24.0}


def test_encode_prompt_omits_auto():
    msg = json.loads(wire.encode_prompt("dark ambient", key="auto", time_signature="auto"))
    assert msg == {"type": "prompt", "tags": "dark ambient"}


def test_encode_prompt_full():
    msg = json.loads(wire.encode_prompt("uplifting", key="A minor",
                                        time_signature="4", tags_b="aggressive"))
    assert msg == {"type": "prompt", "tags": "uplifting", "tags_b": "aggressive",
                   "key": "A minor", "time_signature": "4"}


def test_encode_prompt_omits_tags_b_when_none():
    # tags_b=None → no tags_b key in the message. Distinguishes "no B
    # side, server should treat as always-A" from "empty string B".
    msg = json.loads(wire.encode_prompt("ambient", key="auto",
                                        time_signature="auto", tags_b=None))
    assert "tags_b" not in msg


def test_encode_prompt_with_lora_prefix_in_both_tags():
    # End-to-end: pre-injected prefix on both tags and tags_b is what
    # actually goes on the wire. This is the shape SendPrompt produces
    # when LoRAs are enabled and Promptb is set.
    msg = json.loads(wire.encode_prompt(
        "acidcore, ambient", key="auto", time_signature="auto",
        tags_b="acidcore, techno",
    ))
    assert msg["tags"] == "acidcore, ambient"
    assert msg["tags_b"] == "acidcore, techno"


def test_encode_set_interp_method():
    for path in ("prompt", "timbre", "structure", "feedback"):
        msg = json.loads(wire.encode_set_interp_method(path, "slerp"))
        assert msg == {"type": "set_interp_method", "path": path,
                       "method": "slerp"}
    lin = json.loads(wire.encode_set_interp_method("prompt", "linear"))
    assert lin == {"type": "set_interp_method", "path": "prompt",
                   "method": "linear"}


def test_encode_enable_disable_lora():
    on = json.loads(wire.encode_enable_lora("vintage_synth", strength=0.6))
    assert on == {"type": "enable_lora", "id": "vintage_synth", "strength": 0.6}
    off = json.loads(wire.encode_disable_lora("vintage_synth"))
    assert off == {"type": "disable_lora", "id": "vintage_synth"}


def test_encode_set_timbre_strength():
    msg = json.loads(wire.encode_set_timbre_strength(0.75))
    assert msg == {"type": "set_timbre_strength", "value": 0.75}


def test_encode_swap_source_audio_path():
    msg = json.loads(wire.encode_swap_source(tags="techno", key="C minor",
                                             time_signature="4"))
    assert msg == {"type": "swap_source", "tags": "techno",
                   "key": "C minor", "time_signature": "4"}


def test_encode_swap_source_fixture_path():
    msg = json.loads(wire.encode_swap_source(fixture_name="warm_pad"))
    assert msg == {"type": "swap_source", "fixture_name": "warm_pad"}


# -----------------------------------------------------------------------------
# Slice decoder
# -----------------------------------------------------------------------------
def _build_slice_header(flags: int, start: int, num: int, channels: int,
                       tick_ms: float, dec_ms: float, num_gens: int) -> bytes:
    return (
        struct.pack("<B", flags)
        + struct.pack("<II", start, num)
        + struct.pack("<H", channels)
        + struct.pack("<ff", tick_ms, dec_ms)
        + struct.pack("<I", num_gens)
    )


def test_decode_slice_raw_round_trip():
    # 4 stereo samples of float16
    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0, 0.25, 0.75, -0.75],
                       dtype=np.float16)
    payload = samples.view(np.uint16).tobytes()
    header = _build_slice_header(0, 1000, 4, 2, 8.5, 2.1, 3)
    msg = header + payload

    s = wire.decode_slice(msg, zstd_dec=None)
    assert s.flags == 0
    assert s.start_sample == 1000
    assert s.num_samples == 4
    assert s.channels == 2
    assert pytest.approx(s.tick_ms, abs=1e-3) == 8.5
    assert pytest.approx(s.dec_ms, abs=1e-3) == 2.1
    assert s.num_gens == 3
    # float16 -> float32 roundtrip, within float16 precision
    np.testing.assert_allclose(s.pcm, samples.astype(np.float32), atol=1e-3)


def test_decode_slice_zstd_round_trip():
    zstd = pytest.importorskip("zstandard")
    samples = np.linspace(-1, 1, 16, dtype=np.float16)
    raw = samples.view(np.uint16).tobytes()
    compressed = zstd.ZstdCompressor().compress(raw)
    header = _build_slice_header(1, 0, 8, 2, 5.0, 1.0, 1)
    msg = header + compressed

    dec = zstd.ZstdDecompressor()
    s = wire.decode_slice(msg, zstd_dec=dec)
    assert s.flags == 1
    np.testing.assert_allclose(s.pcm, samples.astype(np.float32), atol=1e-3)


def test_decode_slice_rejects_zstd_without_decompressor():
    header = _build_slice_header(1, 0, 0, 2, 0, 0, 0)
    with pytest.raises(RuntimeError):
        wire.decode_slice(header, zstd_dec=None)


def test_decode_slice_unknown_flag():
    header = _build_slice_header(7, 0, 0, 2, 0, 0, 0)
    with pytest.raises(ValueError):
        wire.decode_slice(header, zstd_dec=None)


# -----------------------------------------------------------------------------
# Control message parsing
# -----------------------------------------------------------------------------
def test_decode_control_valid():
    msg = wire.decode_control('{"type":"ready","duration":60,"channels":2,"sample_rate":48000}')
    assert msg["type"] == "ready"
    assert msg["sample_rate"] == 48000


def test_decode_control_invalid_dict():
    msg = wire.decode_control('"not an object"')
    assert msg["type"] == "_invalid"
