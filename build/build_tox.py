"""
TD build script — generates dist/demonTD.tox from the schema in
src/params.py and the Python source in src/.

How to run
----------
TouchDesigner is GUI-first, so this runs from inside TD:

  1. Open TouchDesigner.
  2. Drop a Text DAT in the network.
  3. Set its File par to <repo>/build/build_tox.py and turn on `Sync to File`.
  4. Right-click the DAT → Run Script.
  5. Watch Alt+T (Textport) for `[build_tox] wrote ...`.

What it does
------------
1. Loads build/template.toe (an empty .toe with a single Base COMP `demon`).
   If no template exists yet, scaffolds one from scratch.
2. Ensures every internal operator listed in TOPOLOGY exists with correct
   wiring, callbacks, and parameter bindings.
3. Adds file-synced Text DATs for each src/*.py.
4. Generates the COMP's custom parameter pages from params.PARAMS.
5. Sets the COMP's Extension to point at the demon_ext Text DAT.
6. Saves the COMP as dist/demonTD.tox and exits.

This script is idempotent — re-running it on a previously built .toe just
updates whatever has drifted.
"""

# NOTE: When this script runs, TD has injected the standard globals:
#   project, op, ops, parent, me, tdu, ui, root, etc.
#
# We import sys.path bootstrap to load our own modules.

import os
import sys

# Resolve repo paths. Inside TD, this file runs from a Text DAT and __file__
# is unreliable, so prefer me.par.file (set on the DAT pointing at this .py)
# and fall back to __file__ when running outside TD.
def _resolve_here() -> str:
    try:
        path = me.par.file.eval()  # type: ignore[name-defined]  # noqa: F821
        if path:
            return os.path.dirname(os.path.abspath(path))
    except Exception:
        pass
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()

HERE = _resolve_here()
REPO_ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(REPO_ROOT, "src")
DIST_DIR = os.path.join(REPO_ROOT, "dist")
TEMPLATE_TOE = os.path.join(HERE, "template.toe")

print(f"[build_tox] HERE={HERE}")
print(f"[build_tox] SRC_DIR={SRC_DIR}")
print(f"[build_tox] DIST_DIR={DIST_DIR}")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if not os.path.isdir(SRC_DIR):
    raise SystemExit(
        f"[build_tox] src/ not found at {SRC_DIR}.\n"
        f"  The build script must live in <repo>/build/ alongside src/.\n"
        f"  If you're running it from a Text DAT, make sure the DAT's 'File'\n"
        f"  par points at <repo>/build/build_tox.py (not a copy elsewhere)."
    )

# Invalidate cached modules so re-running the build picks up edits to
# params.py / wire.py / etc. TD's Python keeps sys.modules across script
# runs, so without this we'd use a stale Param dataclass and AttributeError
# on any newly-added field.
for _modname in ("version", "params", "wire", "queue_client", "oauth",
                 "audio", "ws_client", "telemetry", "queue_worker",
                 "params_pacer", "binary_router", "param_glide"):
    sys.modules.pop(_modname, None)

import params as P  # noqa: E402  pylint: disable=wrong-import-position

# Pull the BUILD_MARKER constant out of demon_ext.py so both the build-time
# log and the runtime extension boot log show the same string. (We can't
# import demon_ext directly — it depends on TD globals — so just grep it.)
def _read_build_marker() -> str:
    try:
        with open(os.path.join(SRC_DIR, "demon_ext.py"), encoding="utf-8-sig") as fh:
            for line in fh:
                if line.startswith("BUILD_MARKER"):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return "unknown"

BUILD_MARKER = _read_build_marker()
print(f"[build_tox] BUILD={BUILD_MARKER}")


# -----------------------------------------------------------------------------
# Internal topology — operators inside the Base COMP `demon`
# -----------------------------------------------------------------------------
TOPOLOGY = [
    # (op_name, OPClass-string, init params dict, position tuple)
    ("extension1",     "textDAT",       {}, (-600, 400)),
    ("ws1",            "websocketDAT",  {}, (-600, 200)),
    ("param_exec1",    "parameterexecuteDAT", {}, (-600, 0)),
    ("frame_exec",     "executeDAT",    {}, (-600,-50)),
    ("tick8ms",        "timerCHOP",     {}, (-400, 0)),
    ("heartbeat",      "timerCHOP",     {}, (-400, -100)),
    ("audio_in",       "inCHOP",        {}, (-800, -200)),
    ("resample_in",    "resampleCHOP",  {}, (-600, -200)),
    ("script_send",    "scriptCHOP",    {}, (-400, -200)),
    # audio_clock is a Time-Slice'd WAVE CHOP wired as audio_out's input.
    # WHY: SpeakerOut plays via PortAudio reading the LoopBuffer directly,
    # so nothing in TD ever pulls audio_out at audio rate — it cooks at
    # frame rate (numSamples≈1), and any downstream Audio Analyze sees
    # nothing. A source-only Script CHOP can't self-promote to audio rate;
    # it needs an audio-rate INPUT to set its time-slice sample count.
    # A Wave CHOP (Time Slice on, rate=48k) natively emits an audio-rate
    # stream, dragging audio_out to audio-rate cooks. (A Constant CHOP was
    # tried and failed — it doesn't produce an audio-rate stream.) The
    # Wave's sample VALUES are ignored — OnCookRecv overwrites with the
    # LoopBuffer PCM; the carrier exists only to set the cook rate. It's
    # independent of audio_in, so the source snapshot is never touched.
    ("audio_clock",    "waveCHOP",      {}, (200, -250)),
    ("audio_out",      "scriptCHOP",    {}, (400, -200)),
    ("resample_out",   "resampleCHOP",  {}, (600, -200)),
    ("out_chop",       "outCHOP",       {}, (800, -200)),
    ("lora_catalog",   "tableDAT",      {}, (-200, 400)),
    ("state",          "tableDAT",      {}, (-200, 300)),
]


def ensure_demon_comp():
    """Return the Base COMP `demon` at /project1/demon, creating if needed."""
    root_comp = op("/project1") if op("/project1") else root
    demon = root_comp.op("demon")
    if demon is None:
        demon = root_comp.create(baseCOMP, "demon")
    return demon


def ensure_internal_ops(demon):
    for name, optype_str, init_pars, pos in TOPOLOGY:
        existing = demon.op(name)
        if existing is None:
            try:
                cls = OPCLASS_LOOKUP[optype_str]
            except KeyError:
                print(f"!! unknown OP class {optype_str}; skipping {name}")
                continue
            o = demon.create(cls, name)
        else:
            o = existing
        try:
            o.nodeX, o.nodeY = pos
        except Exception:
            pass
        for pname, pval in init_pars.items():
            try:
                setattr(o.par, pname, pval)
            except Exception:
                pass
    return demon


# We declare OPCLASS_LOOKUP after TD globals are present.
def get_opclass_lookup():
    return {
        "baseCOMP":            baseCOMP,
        "textDAT":             textDAT,
        "tableDAT":            tableDAT,
        "websocketDAT":        websocketDAT,
        "parameterexecuteDAT": parameterexecuteDAT,
        "executeDAT":          executeDAT,
        "timerCHOP":           timerCHOP,
        "inCHOP":              inCHOP,
        "outCHOP":             outCHOP,
        "scriptCHOP":          scriptCHOP,
        "resampleCHOP":        resampleCHOP,
        "constantCHOP":        constantCHOP,
        "waveCHOP":            waveCHOP,
        "audiodeviceoutCHOP":  audiodeviceoutCHOP,
    }


# -----------------------------------------------------------------------------
# Param-page generation
# -----------------------------------------------------------------------------
def regenerate_param_pages(demon):
    """Drop existing custom pages and rebuild from P.PARAMS.

    Wraps every per-param operation in try/except. A failure on any one
    parameter prints a `!!` line but does NOT break the loop, so the rest
    of the schema continues to populate.
    """
    for page in list(demon.customPages):
        try:
            page.destroy()
        except Exception as e:
            print(f"!! destroy page {page.name}: {e}")

    page_lookup = {}
    for page_name in P.PAGES:
        try:
            page_lookup[page_name] = demon.appendCustomPage(page_name)
        except Exception as e:
            print(f"!! appendCustomPage({page_name}): {e}")

    n_added = 0
    n_failed = 0
    n_hidden = 0
    for p in sorted(P.PARAMS, key=lambda x: (x.page, x.order)):
        # ui_hidden params stay in the schema (for _read_par + lookups) but
        # are never created as visible custom pars. TD has no programmatic
        # way to hide a custom par after creation (Par.hidden is read-only),
        # so "hidden" == "not generated".
        if getattr(p, "ui_hidden", False):
            n_hidden += 1
            continue
        try:
            ok = _add_one_param(demon, page_lookup, p)
        except Exception as e:
            ok = False
            print(f"!! UNCAUGHT exception adding {p.page}/{p.name} ({p.type}): "
                  f"{type(e).__name__}: {e}")
        if ok:
            n_added += 1
        else:
            n_failed += 1

    print(f"[build_tox]   pages: added {n_added} pars, {n_failed} failed, "
          f"{n_hidden} hidden (ui_hidden)")


def _add_one_param(demon, page_lookup, p) -> bool:
    """Append one parameter from the schema. Returns True on success."""
    page = page_lookup.get(p.page)
    if page is None:
        try:
            page = demon.appendCustomPage(p.page)
            page_lookup[p.page] = page
        except Exception as e:
            print(f"!! couldn't create page {p.page}: {e}")
            return False

    label = p.label or p.name

    par = None
    try:
        if p.type == "Pulse":
            par = page.appendPulse(p.name, label=label)
        elif p.type == "Header":
            par = page.appendHeader(p.name, label=label)
        elif p.type == "Toggle":
            par = page.appendToggle(p.name, label=label)
        elif p.type == "Int":
            par = page.appendInt(p.name, label=label)
        elif p.type == "Float":
            par = page.appendFloat(p.name, label=label)
        elif p.type == "Str":
            par = page.appendStr(p.name, label=label)
        elif p.type == "Menu":
            par = page.appendMenu(p.name, label=label)
        elif p.type == "File":
            par = page.appendFile(p.name, label=label)
        else:
            print(f"!! unknown par type {p.type} for {p.name}")
            return False
    except Exception as e:
        print(f"!! append {p.type} {p.page}/{p.name}: {type(e).__name__}: {e}")
        return False

    if par is None:
        print(f"!! append returned None for {p.page}/{p.name}")
        return False

    try:
        p0 = par[0]
    except Exception as e:
        print(f"!! index par {p.name}: {e}")
        return False

    # Apply range FIRST so clamping doesn't squash a default-being-set later.
    # Use `min`/`max` not `normMin`/`normMax` — those are slider-display
    # only; the actual clamp uses min/max. Setting both makes the slider
    # and the clamp agree.
    if p.min is not None:
        for attr in ("min", "normMin"):
            try:
                setattr(p0, attr, p.min)
            except Exception:
                pass
        try:
            p0.clampMin = p.clamp_min
        except Exception:
            pass
    if p.max is not None:
        for attr in ("max", "normMax"):
            try:
                setattr(p0, attr, p.max)
            except Exception:
                pass
        try:
            p0.clampMax = p.clamp_max
        except Exception:
            pass
    # Now defaults + initial value.
    if p.default is not None and p.type not in ("Pulse", "Header"):
        try:
            p0.default = p.default
        except Exception:
            for alt in ("tupletDefaultValue", "defaultValue"):
                try:
                    setattr(p0, alt, p.default)
                    break
                except Exception:
                    continue
        try:
            p0.val = p.default
        except Exception as e:
            print(f"!! val on {p.name}: {e}")
    if p.help:
        try:
            p0.help = p.help
        except Exception:
            pass
    if p.menu_names:
        try:
            p0.menuNames = list(p.menu_names)
            p0.menuLabels = list(p.menu_labels or p.menu_names)
        except Exception as e:
            print(f"!! menu on {p.name}: {e}")
    if p.readonly:
        try:
            p0.readOnly = True
        except Exception:
            pass
    if not p.enable:
        try:
            for sub in par:
                try:
                    sub.enable = False
                except Exception:
                    pass
        except Exception as e:
            print(f"!! enable=False on {p.name}: {e}")
    if p.multiline:
        try:
            p0.style = "Str"
        except Exception:
            pass

    return True


# -----------------------------------------------------------------------------
# DAT sync
# -----------------------------------------------------------------------------
SRC_FILES = ["version.py", "params.py", "wire.py", "queue_client.py",
             "oauth.py", "audio.py", "ws_client.py", "lora_triggers.py",
             "telemetry.py", "queue_worker.py", "params_pacer.py",
             "binary_router.py", "param_glide.py", "demon_ext.py"]


def sync_text_dats(demon):
    """Ensure each src/*.py has a corresponding Text DAT.

    Each DAT gets:
      1. ``dat.text = <file content>`` — the canonical content,
         embedded directly. Travels inside the .tox at export time;
         users dropping the .tox into a fresh project don't need src/
         on disk for the operator to work.
      2. ``dat.par.file = ""`` + ``loadonstart = False`` +
         ``syncfile = False`` — **no external file reference baked
         in**. Earlier builds set par.file to the developer's absolute
         src/ path. Even with loadonstart=False, importing the .tox
         elsewhere triggered TD's "DAT did not load from <path> —
         overwrite anyway?" dialog because TD evaluated the path on
         first instantiation and the path didn't exist on the
         importing machine. Clearing par.file removes the trigger
         entirely.

    Dev hot-reload workflow (per session, since par.file is no longer
    baked in): in TD, open the DAT, click the file-path picker, point
    it at the local ``src/<file>.py``, set loadonstart back to True
    on THAT instance. The DAT will then reload on every project open.
    Don't commit that change to the .tox — it would re-introduce the
    import dialog for everyone else.
    """
    for fname in SRC_FILES:
        dat_name = fname.replace(".py", "")
        dat = demon.op(dat_name)
        if dat is None:
            dat = demon.create(textDAT, dat_name)
        abs_path = os.path.join(SRC_DIR, fname)
        try:
            # utf-8-sig strips a leading BOM if present. TD's tokenizer
            # rejects U+FEFF, and some editors / IDE auto-saves add one.
            with open(abs_path, "r", encoding="utf-8-sig") as fh:
                text = fh.read()
            # Defensive: also strip any in-text BOMs from accidental
            # multi-encode passes.
            if text.startswith("﻿"):
                text = text.lstrip("﻿")
            dat.text = text
            print(f"[build_tox]   loaded {fname} ({len(text)} chars)")
        except Exception as e:
            print(f"!! could not read {abs_path}: {e}")
            continue
        try:
            # Clear par.file so no developer-machine path is baked
            # into the exported .tox. The embedded `dat.text` above
            # is the canonical content; this DAT has no external file
            # reference, so TD has nothing to fail loading on import.
            dat.par.file = ""
            # Belt-and-braces: even with file empty, force both flags
            # off so any stale state from a previously-built DAT
            # (loadonstart=True, syncfile=True) is wiped.
            dat.par.loadonstart = False
            dat.par.syncfile = False
        except Exception as e:
            print(f"!! sync flags on {fname}: {e}")


def wire_extension(demon):
    """Point the COMP's extension at the demon_ext Text DAT.

    For the .module property to resolve, the DAT must contain valid Python
    AT THE TIME the extension expression is evaluated. We verify the
    sibling module loads cleanly before wiring it as an extension —
    that way `.module is None` errors surface here as a clear traceback
    rather than as a mystery NoneType later.
    """
    # ---- diagnostic: can we load demon_ext as a module right now? ----
    demon_ext_dat = demon.op("demon_ext")
    if demon_ext_dat is None:
        print("!! demon_ext DAT not found inside the COMP")
        return
    text_len = len(demon_ext_dat.text or "")
    print(f"[build_tox]   demon_ext DAT: {text_len} bytes of text")

    try:
        m = demon_ext_dat.module
    except Exception as e:
        print(f"!! demon_ext.module raised: {type(e).__name__}: {e}")
        m = None

    if m is None:
        print("!! demon_ext.module is None — the DAT failed to compile.")
        print("   Likely cause: sibling-module import inside demon_ext.py "
              "couldn't resolve. Check that params/wire/queue_client/oauth/"
              "audio Text DATs all exist and have text.")
        for sibling in ("params", "wire", "queue_client", "oauth", "audio"):
            d = demon.op(sibling)
            if d is None:
                print(f"     !! sibling '{sibling}' is missing!")
            else:
                blen = len(d.text or "")
                try:
                    sm = d.module
                except Exception as e:
                    sm = None
                    print(f"     !! sibling '{sibling}' ({blen}B): .module raised {type(e).__name__}: {e}")
                if sm is not None:
                    print(f"     ok '{sibling}' ({blen}B) -> module {sm.__name__}")
                elif d is not None:
                    print(f"     !! sibling '{sibling}' ({blen}B): .module is None")
        # Don't wire the extension if it's known broken — leave it unwired
        # so the COMP still saves and the user can manually fix.
        return

    if not hasattr(m, "DemonExt"):
        print(f"!! demon_ext.module loaded but has no DemonExt class. "
              f"Module dir: {sorted(d for d in dir(m) if not d.startswith('_'))}")
        return

    # ---- wire it ----
    try:
        try:
            demon.par.extension1 = ""
        except Exception:
            pass
        demon.par.extname1 = "DemonExt"
        demon.par.extension1 = "op('./demon_ext').module.DemonExt(me)"
        demon.par.promoteextension1 = True
        try:
            demon.par.reinitextensions.pulse()
        except Exception:
            pass

        # Wire the COMP-level Cleanup script. TD calls this when the COMP
        # is deleted (or the project shuts down), and it's our hook to
        # force-close the WS so DEMON frees its GPU session immediately.
        # If this fails, project.onExit (frame_exec) is the backup.
        for cleanup_par in ("cleanupdat", "cleanupscript"):
            try:
                setattr(demon.par, cleanup_par,
                        "op('./demon_ext').module.DemonExt.Cleanup(parent().ext.DemonExt)")
                break
            except Exception:
                pass

        # Verify
        try:
            ext_inst = demon.ext.DemonExt
            print(f"[build_tox]   extension wired: {type(ext_inst).__name__}")
        except Exception as e:
            print(f"!! extension verify failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"!! extension wire failed: {e}")


def wire_callbacks(demon):
    """Set up parexec/timer/ws callbacks.

    For most TD callback-bearing ops (WebSocket DAT, Timer CHOP, Script CHOP,
    Web Server DAT) you set `par.callbacks = "callbacks"` pointing at a
    shared Text DAT.

    Parameter Execute DAT is the exception — its callback functions live in
    its OWN text, not in an external DAT. So we write the parexec callbacks
    directly into param_exec1's text.
    """
    cb = demon.op("callbacks")
    if cb is None:
        cb = demon.create(textDAT, "callbacks")
        cb.nodeX, cb.nodeY = -400, 400
    cb.text = CALLBACKS_PY

    # Parameter Execute DAT — callbacks live in its own text.
    pe = demon.op("param_exec1")
    if pe is not None:
        pe.text = PARAM_EXEC_PY
        try:
            # Watch the parent COMP's custom pars. Inside a Base COMP, the
            # parexec DAT lives one level below the COMP itself, so `..` is
            # the right reference.
            pe.par.op = ".."
        except Exception as e:
            print(f"!! param_exec1.par.op: {e}")
        for par_name in ("pars",):
            try:
                setattr(pe.par, par_name, "*")
            except Exception:
                pass
        for par_name in ("valuechange", "onvaluechange"):
            try:
                setattr(pe.par, par_name, True)
                break
            except Exception:
                pass
        for par_name in ("pulse", "onpulse"):
            try:
                setattr(pe.par, par_name, True)
                break
            except Exception:
                pass
        try:
            pe.par.active = True
        except Exception:
            pass

    ws = demon.op("ws1")
    if ws is not None:
        try:
            ws.par.callbacks = "callbacks"
        except Exception as e:
            print(f"!! ws.par.callbacks: {e}")

        # Don't fight TD on format/binary settings — defaults route both text
        # and binary to their respective onReceive callbacks. Setting Format
        # explicitly was causing issues. Just dump what's available so we
        # know.
        try:
            print("[build_tox]   ws1 pars:")
            for p in ws.customPars:
                try:
                    print(f"     custom: {p.name} = {p.eval()!r}")
                except Exception:
                    pass
            for pname in ("active", "netaddress", "port", "timeout",
                          "callbacks", "executeloc", "fromop"):
                par = getattr(ws.par, pname, None)
                if par is not None:
                    try:
                        print(f"     {pname} = {par.eval()!r}")
                    except Exception:
                        print(f"     {pname} (uneval)")
        except Exception as e:
            print(f"!! ws par dump: {e}")

    # Timer CHOPs. Conservative: only set well-known pars, no pulses.
    for name in ("tick8ms", "heartbeat"):
        t = demon.op(name)
        if t is None:
            continue
        try:
            t.par.callbacks = "callbacks"
        except Exception:
            pass
        if name == "tick8ms":
            interval = 0.05  # 50 ms
        else:
            interval = 5.0
        try:
            t.par.length = interval
        except Exception:
            pass
        try:
            t.par.cycle = True
        except Exception:
            pass
        try:
            t.par.play = True
        except Exception:
            pass
        # Diagnostic dump.
        try:
            print(f"[build_tox]   {name} pars:")
            for pname in ("length", "cycle", "play", "callbacks"):
                par = getattr(t.par, pname, None)
                if par is not None:
                    try:
                        print(f"     {pname} = {par.eval()!r}")
                    except Exception:
                        pass
        except Exception:
            pass

    # Execute DAT: fires onFrameStart every frame on the main thread.
    # This is our guaranteed drain mechanism for the WS recv-thread queue.
    # Independent of the Timer CHOP, which has been flaky in TD 2025.
    fexec = demon.op("frame_exec")
    if fexec is not None:
        fexec.text = FRAME_EXEC_PY
        # Configure which callbacks fire. The Execute DAT has a separate
        # toggle par per callback, ALL OFF by default — defining the
        # function in the DAT text without enabling its toggle is a
        # silent no-op. We need:
        #   - framestart: per-frame drain of the inbound WS queue.
        #   - playstatechange: TD timeline pause/resume → pause audio.
        for pname, val in (
            ("framestart", True),
            ("playstatechange", True),
            ("active", True),
        ):
            try:
                setattr(fexec.par, pname, val)
            except Exception:
                pass
        try:
            print("[build_tox]   frame_exec pars:")
            for pn in ("framestart", "playstatechange", "active", "framerate"):
                par = getattr(fexec.par, pn, None)
                if par is not None:
                    try:
                        print(f"     {pn} = {par.eval()!r}")
                    except Exception:
                        pass
        except Exception:
            pass

    # Script CHOP callbacks live in a sibling DAT (same as WS/Timer pattern).
    # The Script CHOP's `callbacks` par holds the reference; the DAT contains
    # onCook(scriptOp) which dispatches into ext.DemonExt by op name.
    for name in ("script_send", "audio_out"):
        s = demon.op(name)
        if s is not None:
            try:
                s.par.callbacks = "callbacks"
            except Exception:
                pass


PARAM_EXEC_PY = '''# auto-generated by build_tox.py
# Parameter Execute DAT callbacks. Live in this DAT's OWN text (not in an
# external callbacks DAT) — that's TD's parexec convention.

def _ext():
    return parent().ext.DemonExt

def onValueChange(par, prev):
    try:
        _ext().OnParChange(par)
    except Exception as e:
        print(f"[param_exec onValueChange] {par.name}: {e}")

def onPulse(par):
    try:
        _ext().OnParChange(par)
    except Exception as e:
        print(f"[param_exec onPulse] {par.name}: {e}")

def onExpressionChange(par, val, prev): pass
def onExportChange(par, val, prev): pass
def onEnableChange(par, val, prev): pass
def onModeChange(par, val, prev): pass
'''


FRAME_EXEC_PY = '''# auto-generated by build_tox.py
# Execute DAT — fires once per frame on the MAIN TD thread. We use this
# as the canonical drain point for WS recv-thread events queued in
# DemonExt._inbound. Replaces / supplements the Timer CHOP, which has
# been flaky in TD 2025.

def onFrameStart(frame):
    try:
        parent().ext.DemonExt._drain_inbound()
    except AttributeError:
        # Extension not yet initialized — fine, will be on next frame.
        pass
    except Exception as e:
        print(f"[frame_exec] drain failed: {e}")
    # Belt-and-suspenders heartbeat fallback. frame_exec is the most
    # reliable TD hook we've got (callback name is correct + verified
    # firing). If the Timer CHOP ever stops dispatching again (as it
    # did silently through v0.2.5 because of the wrong callback name),
    # this keeps `/api/queue/status` heartbeats flowing so the server
    # doesn't evict our session. Cheap no-op when the Timer CHOP is
    # already feeding (the call internally throttles to 5 s and bails
    # if a recent OnHeartbeat already ran).
    try:
        parent().ext.DemonExt.MaybeHeartbeatFromFrame()
    except AttributeError:
        pass
    except Exception as e:
        print(f"[frame_exec] heartbeat-fallback failed: {e}")
    # OnTick fallback — drives the continuous-params stream when the
    # Timer CHOP is silent (which it has been). After `ready`, that
    # param stream is the ONLY thing keeping the pod's WS alive; without
    # it the pod idle-times-out and closes before streaming slices.
    try:
        parent().ext.DemonExt.MaybeTickFromFrame()
    except AttributeError:
        pass
    except Exception as e:
        print(f"[frame_exec] tick-fallback failed: {e}")
    # Force-cook audio_out every frame.
    #
    # WHY: Python Audio Out plays from the LoopBuffer via PortAudio, so
    # nothing in TD pulls audio_out. Worse, when a downstream Audio
    # Analyze CHOP IS wired to the COMP's out, the pull does NOT
    # propagate across the Base COMP boundary to audio_out (observed:
    # audio_out_cooks=0 even with Analyze connected). So we force the
    # cook here, every frame, on the main thread.
    #
    # The earlier force-cook (reverted in an interim build) produced
    # FRAME-rate cooks (numSamples=1) because audio_out had no audio-
    # rate input — useless for Analyze. The difference now: audio_out's
    # input is the audio_clock WAVE CHOP (Time Slice, 48 kHz), so each
    # forced cook spans one frame's worth of AUDIO samples (~800 at
    # 48k/60fps). OnCookRecv reads scriptOp.numSamples and fills that
    # many samples from the LoopBuffer, so Analyze downstream gets an
    # audio-rate stream. (Verify via the Debug numSamples diagnostic;
    # if it still reads frame-rate, this approach is wrong and we revert.)
    try:
        _ao = parent().op("audio_out")
        if _ao is not None:
            _ao.cook(force=True)
    except Exception as e:
        print(f"[frame_exec] audio_out force-cook failed: {e}")

def onFrameEnd(frame): pass

def onPlayStateChange(state):
    # TD timeline paused/resumed — pause SpeakerOut so the audio thread
    # emits silence + the LoopBuffer playhead freezes. WS stays open so
    # heartbeats keep the hosted session alive while paused.
    try:
        parent().ext.DemonExt.OnPlayStateChange(state)
    except AttributeError:
        pass
    except Exception as e:
        print(f"[frame_exec onPlayStateChange] failed: {e}")

def onDeviceChange(): pass
def onProjectPreSave(): pass
def onProjectPostSave(): pass
def onLayoutChange(): pass
def onPreSave(): pass
def onPostSave(): pass
def onStart(): pass
def onCreate(): pass

def onExit():
    # Project shutdown — force a clean WS close so the DEMON pod frees
    # its GPU session immediately instead of waiting for TCP timeout.
    try:
        parent().ext.DemonExt.Cleanup()
    except Exception as e:
        print(f"[frame_exec onExit] cleanup failed: {e}")
'''


CALLBACKS_PY = '''# auto-generated by build_tox.py
# Routes TD callbacks into the DemonExt extension.
#
# TD's various op types each call fixed-name functions. We dispatch by
# op name so one callbacks DAT serves the whole COMP.

def _ext():
    return me.parent().ext.DemonExt

def onValueChange(par, prev):
    _ext().OnParChange(par)

def onPulse(par):
    _ext().OnParChange(par)

# NOTE: TD Timer CHOP callbacks are:
#   onTimerStart / onTimerPulse / onTimerCycle / onTimerSegment /
#   onTimerComplete
# Up through v0.2.5 this DAT defined `onTimer`, which TD silently
# ignored — so OnTick + OnHeartbeat never fired. That broke heartbeat-
# driven session keep-alive and the params batch flush. Use
# `onTimerPulse` (canonical "every cycle" hook for cycle=True timers).
def onTimerPulse(timerOp, segment):
    name = timerOp.name
    ext = _ext()
    if name == "tick8ms":
        ext.OnTick()
    elif name == "heartbeat":
        ext.OnHeartbeat()

def onReceiveText(dat, rowIndex, message):
    # The TD WebSocket DAT is vestigial — we use ws_client.py instead, which
    # bypasses these callbacks entirely. Kept here so any future direct-DAT
    # usage still dispatches to the extension.
    _ext().OnReceive(dat, rowIndex=rowIndex, message=message)

def onReceiveBinary(dat, contents):
    _ext().OnReceive(dat, contents=contents)

def onConnect(dat):
    try:
        _ext().OnWsConnect(dat)
    except Exception as e:
        print(f"[ws onConnect] OnWsConnect failed: {e}")

def onDisconnect(dat):
    pass

# Script CHOP cook hook. TD calls onCook(scriptOp) on the configured DAT.
# We dispatch by the calling op's name.
def onCook(scriptOp):
    # When the .tox is first dropped, TD starts cook chains BEFORE it
    # instantiates the extension object — audio_out cooks once in that
    # window and ext.DemonExt isn't there yet. Same guard frame_exec uses;
    # the next cook resolves cleanly.
    try:
        ext = _ext()
    except AttributeError:
        return
    name = scriptOp.name
    if name == "script_send":
        ext.OnCookSend(scriptOp)
    elif name == "audio_out":
        ext.OnCookRecv(scriptOp)
'''


# -----------------------------------------------------------------------------
# Audio wiring
# -----------------------------------------------------------------------------
def wire_audio(demon):
    """Wire the audio-OUT chain.

    Topology:
        audio_clock (waveCHOP, Time Slice, 48 kHz audio-rate carrier)
            └─► audio_out (scriptCHOP, callbacks, timeslice=True)
                  └─► out_chop (outCHOP, timeslice=True)

    audio_clock is a cook-CARRIER, not the audio. SpeakerOut plays the
    generated audio from the LoopBuffer via PortAudio directly, so
    nothing in TD pulls audio_out at audio rate — left alone, a source-
    only Script CHOP cooks at frame rate (numSamples≈1) and a downstream
    Audio Analyze CHOP sees nothing. A Wave CHOP with Time Slice on emits
    a genuine audio-rate stream; feeding it into audio_out sets the
    Script CHOP's time-slice sample count to audio rate. OnCookRecv
    IGNORES the carrier's sample values and writes the LoopBuffer PCM —
    the carrier exists only to fix the cook rate. Independent of audio_in,
    so the one-shot source snapshot is never touched.

    NOTE: source upload is a one-shot snapshot of the COMP's wired CHOP
    input (audio_in) at Connect time — not a stream through this chain.
    """
    audio_clock = demon.op("audio_clock")
    audio_out = demon.op("audio_out")
    resample_out = demon.op("resample_out")
    out_chop = demon.op("out_chop")

    # Destroy genuinely-stale ops. audio_clock is NO LONGER stale (it's
    # the Wave CHOP carrier now), so it's kept + configured below.
    for stale in ("audiodevout",):
        op_ = demon.op(stale)
        if op_ is not None:
            try:
                op_.destroy()
                print(f"[build_tox] destroyed stale op: {stale}")
            except Exception as e:
                print(f"!! destroy {stale}: {e}")

    # Configure the Wave CHOP audio-rate carrier. The waveform values are
    # irrelevant (OnCookRecv overwrites). What matters: Time Slice ON +
    # an audio sample rate so TD treats the chain as audio-rate.
    if audio_clock is not None:
        for pname, pval in (
            ("timeslice", True),
            ("rate", 48000),       # output sample rate (samples/sec)
            ("channelname", "clock"),
            ("frequency", 1.0),    # arbitrary; values are discarded
        ):
            try:
                setattr(audio_clock.par, pname, pval)
            except Exception:
                pass

    if audio_out is not None:
        try:
            audio_out.par.callbacks = "callbacks"
        except Exception:
            pass
        try:
            audio_out.par.timeslice = True
        except Exception:
            pass
        # Detach any stale input, then wire audio_clock → audio_out.
        try:
            for c in audio_out.inputConnectors:
                if c.connections:
                    c.disconnect()
        except Exception:
            pass
        if audio_clock is not None:
            try:
                audio_out.inputConnectors[0].connect(audio_clock)
            except Exception as e:
                print(f"!! wire audio_clock → audio_out: {e}")
        try:
            n_in = sum(1 for c in audio_out.inputConnectors if c.connections)
            print(f"[build_tox] BUILD={BUILD_MARKER} audio_out inputs="
                  f"{n_in} (waveCHOP audio-rate carrier)")
        except Exception:
            pass

    # Connect audio_out → out_chop DIRECTLY.
    # resample_out remains in the topology for back-compat but is
    # disconnected — Audio Device Out handles its own resampling.
    if resample_out is not None:
        try:
            # Detach any input it might have from a previous build run.
            for c in resample_out.inputConnectors:
                if c.connections:
                    c.disconnect()
        except Exception:
            pass

    try:
        if audio_out is not None and out_chop is not None:
            for c in out_chop.inputConnectors:
                if c.connections:
                    c.disconnect()
            out_chop.inputConnectors[0].connect(audio_out)
            try:
                out_chop.par.timeslice = True
            except Exception:
                pass
            # Explicitly CLEAR selectchop. Having both a wired input AND a
            # selectchop reference can produce undefined / select-mode
            # behavior that mangles audio-rate signals. We want pure
            # wired-input propagation.
            try:
                out_chop.par.selectchop = ""
            except Exception:
                pass
    except Exception as e:
        print(f"audio wiring out_chop: {e}")


    # script_send is a vestigial no-op; still hook up its callbacks so it doesn't error.
    script_send = demon.op("script_send")
    if script_send is not None:
        try:
            script_send.par.callbacks = "callbacks"
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    """Build the demon COMP into the currently-open TD project.

    This function is NON-DESTRUCTIVE:
      - Never closes / saves / quits the host TD project.
      - Only creates the `demon` COMP and its children.
      - Saves out the .tox file to dist/.
      - Leaves TD running with the COMP visible so the user can inspect.

    Safe to re-run; ops are upserted, not duplicated.
    """
    global OPCLASS_LOOKUP
    OPCLASS_LOOKUP = get_opclass_lookup()

    os.makedirs(DIST_DIR, exist_ok=True)

    print("[build_tox] creating demon COMP...")
    demon = ensure_demon_comp()
    print(f"[build_tox]   COMP at {demon.path}")

    print("[build_tox] ensuring internal ops...")
    ensure_internal_ops(demon)

    print("[build_tox] syncing source DATs...")
    sync_text_dats(demon)

    print("[build_tox] wiring callbacks...")
    wire_callbacks(demon)

    print("[build_tox] wiring audio...")
    wire_audio(demon)

    print("[build_tox] regenerating parameter pages...")
    regenerate_param_pages(demon)

    print("[build_tox] wiring extension...")
    wire_extension(demon)

    out_path = os.path.join(DIST_DIR, "demonTD.tox")
    print(f"[build_tox] saving {out_path}")
    demon.save(out_path)

    print(f"[build_tox] DONE — wrote {out_path}")
    print(f"[build_tox] Inspect /project1/demon, or drag {out_path} into a fresh .toe.")


# Entry: always run when executed inside TD.
# Inside TD this file is usually loaded into a Text DAT and run via the DAT's
# `Run Script` action. We don't gate on __name__ because TD doesn't set it
# to "__main__" in that path.
try:
    main()
except Exception as exc:
    print(f"[build_tox] FAILED: {exc}")
    import traceback
    traceback.print_exc()
