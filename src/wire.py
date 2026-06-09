"""
DEMON WebSocket wire protocol — encoders and decoders.

Pure functions, no TouchDesigner dependencies. Fully unit-testable.

Mirrors the JS client at
  demon-public-demo/vendor/demon-ui/engine/protocol.ts
  demon-public-demo/types/protocol.ts

Three traffic shapes
--------------------
1. SessionConfig (JSON, sent once at WS open)
2. Audio frame (binary: 8-byte header + float32 interleaved PCM)
3. Continuous params message (JSON, sent on the 8ms tick)
4. Discrete control messages (JSON, on-demand)
5. Slice (binary, server→client): 23-byte header + raw or zstd-compressed float16

Server JSON responses (server→client) are passed through `decode_control` and
returned as plain dicts; the caller inspects `msg["type"]`.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

# Protocol constants — must match types/protocol.ts
SAMPLE_RATE: int = 48000
T: int = 1500  # 60s @ 25fps latents
CROSSFADE_SECONDS: float = 0.025
SLICE_HDR_SIZE: int = 23  # 1 + 4 + 4 + 2 + 4 + 4 + 4
SLICE_FLAG_RAW: int = 0
SLICE_FLAG_DELTA: int = 1


# -----------------------------------------------------------------------------
# Audio frame encoding (client → server)
# -----------------------------------------------------------------------------

def encode_audio_frame(pcm: np.ndarray, channels: int) -> bytes:
    """Encode a PCM array as the binary frame DEMON expects.

    Wire format:
        bytes 0..3  : uint32 LE channels
        bytes 4..7  : uint32 LE total samples per channel
        bytes 8..   : float32 LE, interleaved

    Parameters
    ----------
    pcm : np.ndarray
        Either shape (channels, samples) or (samples * channels,) interleaved
        already, or (samples, channels). We detect and normalize.
    channels : int
        Final channel count to emit. If `pcm` is mono and `channels==2`, the
        signal is duplicated L→R.

    Returns
    -------
    bytes
        Header + float32 PCM, ready for ws.send().
    """
    pcm = np.asarray(pcm, dtype=np.float32)

    if pcm.ndim == 1:
        # Treat as mono; tile if stereo requested.
        num_samples = pcm.shape[0]
        if channels == 1:
            interleaved = pcm
        else:
            interleaved = np.repeat(pcm, channels)
    elif pcm.ndim == 2:
        # Decide which axis is channels.
        # Convention: (channels, samples) when shape[0] is small and shape[1] large.
        if pcm.shape[0] <= 8 and pcm.shape[1] > pcm.shape[0]:
            chs, num_samples = pcm.shape
        else:
            num_samples, chs = pcm.shape
            pcm = pcm.T
        if chs == channels:
            interleaved = pcm.T.reshape(-1).astype(np.float32, copy=False)
        elif chs == 1 and channels == 2:
            mono = pcm[0]
            interleaved = np.repeat(mono, 2)
            num_samples = mono.shape[0]
        elif chs == 2 and channels == 1:
            interleaved = pcm.mean(axis=0).astype(np.float32, copy=False)
            num_samples = interleaved.shape[0]
        else:
            raise ValueError(f"Cannot reconcile pcm shape {pcm.shape} with channels={channels}")
    else:
        raise ValueError(f"PCM must be 1D or 2D, got {pcm.ndim}D")

    header = struct.pack("<II", int(channels), int(num_samples))
    return header + interleaved.astype(np.float32, copy=False).tobytes()


# -----------------------------------------------------------------------------
# JSON encoders (client → server)
# -----------------------------------------------------------------------------

def encode_config(cfg: dict[str, Any]) -> str:
    """Encode the initial SessionConfig.

    Drops keys whose value is None. Does NOT drop empty strings — DEMON
    expects `fixture_name: ""` to be present and stripping it makes the
    server close the WS immediately after our send.
    """
    clean = {k: v for k, v in cfg.items() if v is not None}
    return json.dumps(clean, separators=(",", ":"))


def encode_params(raw: dict[str, Any], playback_pos: float) -> str:
    """Continuous params message — sent on the 8ms tick.

    `playback_pos` is in SECONDS (matching demon-public-demo's
    useParamSync.ts which passes `session.player.positionSec`). The
    server uses it for absolute-time curve sampling.

    NaN/Inf floats are DROPPED, never serialized: json.dumps would
    happily emit `NaN`, which is not valid JSON — the server-side parse
    fails silently and, since this message is the keepalive, a sticky
    non-finite param would poison every keepalive until the pod gives
    up on us. This function must never raise (it runs on the pacer
    thread inside the keepalive loop), so we sanitize rather than
    reject; `allow_nan=False` stays as the backstop for anything the
    sweep misses (e.g. numpy scalars).
    """
    if any(isinstance(v, float) and not math.isfinite(v)
           for v in raw.values()):
        raw = {k: v for k, v in raw.items()
               if not (isinstance(v, float) and not math.isfinite(v))}
    pos = float(playback_pos)
    if not math.isfinite(pos):
        pos = 0.0
    return json.dumps(
        {"type": "params", "raw": raw, "playback_pos": pos},
        separators=(",", ":"),
        allow_nan=False,
    )


def encode_prompt(tags: str, key: str | None = None,
                  time_signature: str | None = None,
                  tags_b: str | None = None) -> str:
    msg: dict[str, Any] = {"type": "prompt", "tags": tags}
    if tags_b is not None:
        msg["tags_b"] = tags_b
    if key is not None and key != "auto":
        msg["key"] = key
    if time_signature is not None and time_signature != "auto":
        msg["time_signature"] = time_signature
    return json.dumps(msg, separators=(",", ":"))


def encode_set_prompt_blend(value: float) -> str:
    return json.dumps({"type": "set_prompt_blend", "value": float(value)},
                      separators=(",", ":"))


def encode_set_interp_method(path: str, method: str) -> str:
    """Discrete per-path interpolation-method control.

    `path` is one of prompt/timbre/structure/feedback; `method` is
    "slerp" (norm-preserving spherical blend, the server default) or
    "linear". Mirrors demon-public-demo's protocol.ts sendSetInterpMethod
    — the server applies it immediately, no smoothing/echo channel.
    """
    return json.dumps({"type": "set_interp_method", "path": path,
                       "method": method}, separators=(",", ":"))


def encode_enable_lora(id: str, strength: float | None = None) -> str:
    msg: dict[str, Any] = {"type": "enable_lora", "id": id}
    if strength is not None:
        msg["strength"] = float(strength)
    return json.dumps(msg, separators=(",", ":"))


def encode_disable_lora(id: str) -> str:
    return json.dumps({"type": "disable_lora", "id": id}, separators=(",", ":"))


def encode_set_timbre_strength(value: float) -> str:
    return json.dumps({"type": "set_timbre_strength", "value": float(value)},
                      separators=(",", ":"))


def encode_set_timbre_source(name: str) -> str:
    """Header JSON for a timbre-source upload. Followed by a binary audio frame."""
    return json.dumps({"type": "set_timbre_source", "name": name},
                      separators=(",", ":"))


def encode_set_timbre_fixture(name: str) -> str:
    return json.dumps({"type": "set_timbre_fixture", "name": name},
                      separators=(",", ":"))


def encode_clear_timbre_source() -> str:
    return json.dumps({"type": "clear_timbre_source"}, separators=(",", ":"))


def encode_set_structure_source(name: str) -> str:
    """Header JSON for a structure-source upload. Followed by binary audio frame."""
    return json.dumps({"type": "set_structure_source", "name": name},
                      separators=(",", ":"))


def encode_set_structure_fixture(name: str) -> str:
    return json.dumps({"type": "set_structure_fixture", "name": name},
                      separators=(",", ":"))


def encode_clear_structure_source() -> str:
    return json.dumps({"type": "clear_structure_source"}, separators=(",", ":"))


def encode_swap_source(tags: str | None = None,
                       key: str | None = None,
                       time_signature: str | None = None,
                       fixture_name: str | None = None) -> str:
    """Header JSON for a source swap. Followed by binary audio frame
    (unless fixture_name is set, in which case no audio follows)."""
    msg: dict[str, Any] = {"type": "swap_source"}
    if tags:
        msg["tags"] = tags
    if key and key != "auto":
        msg["key"] = key
    if time_signature and time_signature != "auto":
        msg["time_signature"] = time_signature
    if fixture_name:
        msg["fixture_name"] = fixture_name
    return json.dumps(msg, separators=(",", ":"))


# -----------------------------------------------------------------------------
# Server → client decoders
# -----------------------------------------------------------------------------

def decode_control(msg: str) -> dict[str, Any]:
    """Parse a JSON control message from the server. Never raises on schema
    mismatch — returns the dict as-is; caller inspects msg['type']."""
    parsed = json.loads(msg)
    if not isinstance(parsed, dict):
        return {"type": "_invalid", "raw": msg}
    return parsed


@dataclass
class SliceData:
    flags: int
    start_sample: int
    num_samples: int
    channels: int
    tick_ms: float
    dec_ms: float
    num_gens: int
    pcm: np.ndarray  # float32, interleaved


def _float16_to_float32(u16: np.ndarray) -> np.ndarray:
    """Reinterpret an array of uint16 as IEEE-754 float16, then upcast to float32."""
    return u16.view(np.float16).astype(np.float32)


def decode_slice(buf: bytes, zstd_dec=None) -> SliceData:
    """Parse a server-sent binary slice.

    Header (23 bytes, all little-endian):
        u8  flags         (0 = raw float16, 1 = zstd-compressed float16)
        u32 startSample
        u32 numSamples
        u16 channels
        f32 tickMs
        f32 decMs
        u32 numGens

    Payload: raw or zstd-compressed float16 PCM, interleaved.

    Parameters
    ----------
    buf : bytes
        Full WS binary message.
    zstd_dec : zstandard.ZstdDecompressor | None
        Required if any slice has flags == SLICE_FLAG_DELTA. Pass None to
        only support raw slices (encode_config with `compression: "none"`).
    """
    if len(buf) < SLICE_HDR_SIZE:
        raise ValueError(f"slice too short: {len(buf)} bytes")

    flags = buf[0]
    start_sample, num_samples = struct.unpack_from("<II", buf, 1)
    channels = struct.unpack_from("<H", buf, 9)[0]
    tick_ms, dec_ms = struct.unpack_from("<ff", buf, 11)
    num_gens = struct.unpack_from("<I", buf, 19)[0]

    payload = buf[SLICE_HDR_SIZE:]

    if flags == SLICE_FLAG_DELTA:
        if zstd_dec is None:
            raise RuntimeError(
                "Slice is zstd-compressed but no decompressor was provided. "
                "Either send 'compression: none' in SessionConfig or pass a "
                "zstandard.ZstdDecompressor."
            )
        payload = zstd_dec.decompress(payload)
    elif flags != SLICE_FLAG_RAW:
        raise ValueError(f"Unknown slice flags: {flags}")

    # Validate the (decompressed) payload against the header before
    # touching it: a truncated frame would otherwise yield a SHORT pcm
    # that gets silently patched into the loop (audible garbage), and
    # an overlong one would smuggle extra samples past the header.
    expected_bytes = num_samples * channels * 2  # float16
    if len(payload) != expected_bytes:
        raise ValueError(
            f"slice payload size mismatch: got {len(payload)}B, header "
            f"says {num_samples} samples x {channels}ch = {expected_bytes}B"
        )

    # Defensive copy so the underlying buffer is 2-byte aligned for view().
    u16 = np.frombuffer(bytes(payload), dtype=np.uint16)
    pcm = _float16_to_float32(u16)

    return SliceData(
        flags=flags,
        start_sample=start_sample,
        num_samples=num_samples,
        channels=channels,
        tick_ms=tick_ms,
        dec_ms=dec_ms,
        num_gens=num_gens,
        pcm=pcm,
    )
