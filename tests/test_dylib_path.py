"""Tests for demon_ext._portaudio_dylib_path.

Regression for the "could not load PortAudio binary: dlopen(
libportaudio.dylib...)" no-audio failure: the dylib path used to be
derived ONLY from the demon_ext DAT's file par — which the build clears
on every DAT (so no dev path bakes into shipped .tox files) — so any
freshly built .tox computed a garbage path and fell back to bare dlopen.
It now resolves from the discovered vendor/ root first.
"""

import os

import demon_ext


def _make_vendor(tmp_path, libname):
    pa_dir = tmp_path / "vendor" / "sounddevice" / "_sounddevice_data" \
        / "portaudio-binaries"
    pa_dir.mkdir(parents=True)
    lib = pa_dir / libname
    lib.write_bytes(b"\x00")
    return str(tmp_path / "vendor"), str(lib)


def test_resolves_from_vendor_root(tmp_path):
    vendor, lib = _make_vendor(tmp_path, "libportaudio.dylib")
    got = demon_ext._portaudio_dylib_path(vendor, dat_file=None,
                                          sysname="darwin")
    assert got == lib


def test_resolves_from_dat_file_fallback(tmp_path):
    vendor, lib = _make_vendor(tmp_path, "libportaudio.dylib")
    src = tmp_path / "src"
    src.mkdir()
    dat_file = str(src / "demon_ext.py")
    got = demon_ext._portaudio_dylib_path(None, dat_file=dat_file,
                                          sysname="darwin")
    assert got == lib


def test_vendor_root_wins_over_dat_file(tmp_path):
    vendor_a, lib_a = _make_vendor(tmp_path / "a", "libportaudio.dylib")
    vendor_b, lib_b = _make_vendor(tmp_path / "b", "libportaudio.dylib")
    dat_file = str(tmp_path / "b" / "src" / "demon_ext.py")
    os.makedirs(os.path.dirname(dat_file), exist_ok=True)
    got = demon_ext._portaudio_dylib_path(vendor_a, dat_file=dat_file,
                                          sysname="darwin")
    assert got == lib_a


def test_returns_none_when_binary_missing(tmp_path):
    empty_vendor = tmp_path / "vendor"
    empty_vendor.mkdir()
    assert demon_ext._portaudio_dylib_path(str(empty_vendor),
                                           sysname="darwin") is None
    assert demon_ext._portaudio_dylib_path(None, None,
                                           sysname="darwin") is None


def test_windows_libname(tmp_path):
    vendor, lib = _make_vendor(tmp_path, "libportaudio64bit.dll")
    got = demon_ext._portaudio_dylib_path(vendor, sysname="windows")
    assert got == lib


def test_repo_vendor_resolves_on_this_machine():
    """The actual repo vendor/ ships the macOS binary — the resolver
    must find it from the repo root (same shape as a bundle-zip user's
    .tox + vendor/ layout)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vendor = os.path.join(repo, "vendor")
    got = demon_ext._portaudio_dylib_path(vendor, sysname="darwin")
    assert got is not None
    assert got.endswith("libportaudio.dylib")
    assert os.path.isfile(got)
