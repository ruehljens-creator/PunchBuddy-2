#!/usr/bin/env python3
"""
PunchBuddy – Pro Tools Punch-In Automation
==========================================

Ablauf START (Hotkey):
  1. Ziel-Spuren in der Session finden
  2. Spuren Record-Enable AN
  3. Input Monitor AUS  (PTSL)
  4. Pre-Roll        EIN (CGEvent, non-blocking)
  5. Record starten  (PTSL toggle_play_state)

Ablauf STOP (Space oder Punch-Out):
  6. Warten bis Transport wirklich gestoppt (inkl. Post-Roll)
  7a. Pre-Roll   AUS  (CGEvent, non-blocking, Hintergrund-Thread)
  7b. Input Monitor EIN  (PTSL, parallel zu 7a)
"""

import ptsl
import time
import threading
import logging
import json
import os
import subprocess
import sys
import atexit
import gc
import select
import shutil
import glob
import copy

# ─────────────────────────────────────────────────────────────────────────────
# Dock-Name + Prozessname SOFORT setzen (VOR import rumps / AppKit)
# rumps importiert intern AppKit und cached den Bundle-Namen.
# Deshalb muss CFBundleName VORHER überschrieben werden.
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Foundation import NSBundle, NSProcessInfo
    _bundle = NSBundle.mainBundle()
    _info = _bundle.infoDictionary()
    if _info is not None:
        _info['CFBundleName'] = 'PunchBuddy'
        _info['CFBundleDisplayName'] = 'PunchBuddy'
    NSProcessInfo.processInfo().setProcessName_('PunchBuddy')
except Exception:
    pass

try:
    import ctypes, ctypes.util
    _libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('c'))
    _libc.setprogname(b'PunchBuddy')
except Exception:
    pass

import rumps
import http.server
import socketserver

# ─────────────────────────────────────────────────────────────────────────────
# pyobjc (für AppleScript Pre-Roll-Erkennung)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Foundation import NSAppleScript, NSAutoreleasePool
    APPLESCRIPT_OK = True
except ImportError:
    NSAppleScript = NSAutoreleasePool = None
    APPLESCRIPT_OK = False

try:
    import AppKit
    import objc
    APPKIT_OK = True
except ImportError:
    APPKIT_OK = False

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
# Logging/Pfade ausgelagert nach punchbuddy/log.py
from punchbuddy.log import _setup_log_dir, _LOG_DIR, LOG_PATH, _trim_log

# ─────────────────────────────────────────────────────────────────────────────
# Einstellungen
# ─────────────────────────────────────────────────────────────────────────────
# Einstellungen ausgelagert nach punchbuddy/config.py
from punchbuddy.config import (
    SETTINGS_PATH, DEFAULT_SETTINGS, _deep_merge, _PRESET_TEMPLATE,
    load_settings, save_settings, _migrate_settings,
    _OLD_SETTINGS_DIR, _NEW_SETTINGS_DIR,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mehrsprachigkeit / Localization
# ─────────────────────────────────────────────────────────────────────────────
# i18n (Übersetzungen + Sprachauswahl) ausgelagert nach punchbuddy/i18n.py
from punchbuddy.i18n import (
    TRANSLATIONS, t, load_app_language, set_language, get_language,
)



def init_runtime():
    """Einmalige Laufzeit-Initialisierung mit Seiteneffekten (Sprache laden,
    Settings migrieren). Wird aus dem __main__-Block aufgerufen, NICHT beim
    Modul-Import – so bleibt `import auto_punch_in` nebenwirkungsfrei (testbar).
    Idempotent."""
    load_app_language(SETTINGS_PATH)
    _migrate_settings()



def webtrigger_token_ok(expected: str, supplied: str) -> bool:
    """True, wenn der Webtrigger-Request autorisiert ist. Leeres `expected`
    bedeutet kein Schutz (nur sinnvoll bei Bind auf 127.0.0.1). Sonst
    Konstantzeit-Vergleich gegen Timing-Angriffe."""
    expected = expected or ""
    if not expected:
        return True
    import hmac
    return hmac.compare_digest(supplied or "", expected)

# ─────────────────────────────────────────────────────────────────────────────
# CGEvent – Tastendruck direkt an Pro Tools (non-blocking)
# ─────────────────────────────────────────────────────────────────────────────
# macOS Virtual Key Codes
_VK_K     = 40   # 'k'
_VK_F12   = 111  # F12
_VK_SPACE = 49   # Space
_VK_R     = 15   # 'r'
_VK_A     = 0    # 'a'
_VK_F2    = 120  # F2
_VK_C     = 8    # 'c'
_VK_ESC   = 53   # Escape
_VK_P     = 35   # 'p'
_VK_V     = 9    # 'v'

_cached_pid = None

def _pt_pid():
    """Pro Tools Prozess-ID ermitteln (mit Caching).

    Der Cache wird vor jeder Rückgabe gegen den laufenden Prozess validiert:
    Startet Pro Tools neu (Crash/Update), ist die gecachte PID tot und würde
    sonst Tastendrücke ins Leere senden (CGEventPostToPid an tote PID).
    """
    global _cached_pid
    if _cached_pid is not None:
        try:
            os.kill(_cached_pid, 0)  # Signal 0: prüft nur Existenz, sendet nichts
            return _cached_pid
        except OSError:
            logging.info(f"Pro Tools PID {_cached_pid} nicht mehr aktiv – Cache geleert.")
            _cached_pid = None

    try:
        out = subprocess.run(
            ["pgrep", "-x", "Pro Tools"],
            capture_output=True, text=True, timeout=2
        ).stdout.strip()
        if out:
            _cached_pid = int(out)
            return _cached_pid
        return None
    except Exception:
        return None

def _send_key(vk: int, cmd: bool = False) -> bool:
    """
    Sendet Tastendruck direkt an Pro Tools via CGEventPostToPid.
    Non-blocking: kehrt sofort zurück, kein Warten auf PT's Main-Thread.
    Fallback auf AppleScript wenn Quartz nicht verfügbar.
    """
    try:
        import Quartz
        pid = _pt_pid()
        if not pid:
            logging.warning("send_key: Pro Tools PID nicht gefunden.")
            return False
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        ev_dn = Quartz.CGEventCreateKeyboardEvent(src, vk, True)
        ev_up = Quartz.CGEventCreateKeyboardEvent(src, vk, False)
        if cmd:
            flags = Quartz.kCGEventFlagMaskCommand
            Quartz.CGEventSetFlags(ev_dn, flags)
            Quartz.CGEventSetFlags(ev_up, flags)
        Quartz.CGEventPostToPid(pid, ev_dn)
        Quartz.CGEventPostToPid(pid, ev_up)
        return True
    except ImportError:
        logging.debug("Quartz nicht verfügbar – AppleScript Fallback")
        if APPLESCRIPT_OK:
            char = {_VK_K: "k", _VK_F12: "", _VK_SPACE: " "}.get(vk, "")
            if char:
                mod = "{command down}" if cmd else ""
                script = (
                    f'tell application "System Events" to tell process "Pro Tools" to '
                    f'keystroke "{char}"' + (f" using {mod}" if mod else "")
                )
                s = NSAppleScript.alloc().initWithSource_(script)
                s.executeAndReturnError_(None)
        return False
    except Exception as e:
        logging.error(f"send_key vk={vk} cmd={cmd}: {e}")
        return False

# Validierter PID-Cache pro App-Name. Spart auf Hot-Paths (Key-Sends an PT/
# Interplay während Export) die NSWorkspace-Iteration + bis zu zwei pgrep-Forks.
# Vor jeder Rückgabe wird die PID mit os.kill(pid, 0) gegen den laufenden Prozess
# geprüft – nach App-Neustart wird der Eintrag automatisch verworfen.
_app_pid_cache: dict = {}


def _app_pid(app_name: str):
    """Ermittelt die PID einer App anhand ihres Prozessnamens (mit validiertem
    Cache). Nutzt NSWorkspace (leerzeichentolerant) als primäre Methode,
    pgrep als Fallback."""
    cached = _app_pid_cache.get(app_name)
    if cached is not None:
        try:
            os.kill(cached, 0)
            return cached
        except OSError:
            _app_pid_cache.pop(app_name, None)

    found = None
    # NSWorkspace: normalisiert Leerzeichen, damit "interplayAccess" == "Interplay Access"
    try:
        from AppKit import NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        name_norm = app_name.lower().replace(" ", "")
        for app in ws.runningApplications():
            ln = app.localizedName() or ""
            if name_norm in ln.lower().replace(" ", ""):
                found = app.processIdentifier()
                break
    except Exception:
        pass
    # Fallback: pgrep exakt, dann ohne -x
    if found is None:
        try:
            out = subprocess.run(
                ["pgrep", "-xi", app_name],
                capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if out:
                found = int(out.split("\n")[0])
            else:
                out = subprocess.run(
                    ["pgrep", "-i", app_name],
                    capture_output=True, text=True, timeout=2
                ).stdout.strip()
                if out:
                    found = int(out.split("\n")[0])
        except Exception:
            pass

    if found is not None:
        _app_pid_cache[app_name] = found
    return found

def _send_key_to_app(vk: int, app_name: str, cmd: bool = False,
                      shift: bool = False, ctrl: bool = False) -> bool:
    """
    Sendet Tastendruck an eine beliebige App via CGEventPostToPid.
    Umgeht die osascript Accessibility-Einschränkung.
    """
    try:
        import Quartz
        pid = _app_pid(app_name)
        if not pid:
            logging.warning(f"send_key_to_app: '{app_name}' PID nicht gefunden.")
            return False
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        ev_dn = Quartz.CGEventCreateKeyboardEvent(src, vk, True)
        ev_up = Quartz.CGEventCreateKeyboardEvent(src, vk, False)
        flags = 0
        if cmd:
            flags |= Quartz.kCGEventFlagMaskCommand
        if shift:
            flags |= Quartz.kCGEventFlagMaskShift
        if ctrl:
            flags |= Quartz.kCGEventFlagMaskControl
        if flags:
            Quartz.CGEventSetFlags(ev_dn, flags)
            Quartz.CGEventSetFlags(ev_up, flags)
        Quartz.CGEventPostToPid(pid, ev_dn)
        Quartz.CGEventPostToPid(pid, ev_up)
        return True
    except Exception as e:
        logging.error(f"send_key_to_app vk={vk} app={app_name}: {e}")
        return False

def _activate_app(app_name: str) -> bool:
    """Aktiviert eine App (bringt sie in den Vordergrund) via NSWorkspace.
    Normalisiert Leerzeichen im Namen, damit z.B. "interplayAccess" == "Interplay Access".
    """
    try:
        from AppKit import NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        apps = ws.runningApplications()
        name_norm = app_name.lower().replace(" ", "")
        for app in apps:
            ln = app.localizedName() or ""
            if name_norm in ln.lower().replace(" ", ""):
                app.activateWithOptions_(0x01)  # NSApplicationActivateIgnoringOtherApps
                return True
        # Fallback: via open command
        subprocess.run(["open", "-a", app_name], timeout=5, capture_output=True)
        return True
    except Exception as e:
        logging.warning(f"activate_app '{app_name}': {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# Pre-Roll Management (via PTSL – get/set_timeline_selection)
# ─────────────────────────────────────────────────────────────────────────────
# Pro Tools PTSL liefert pre_roll_enabled als Feld in GetTimelineSelection.
# Setzen erfolgt über set_timeline_selection(pre_roll_enabled=TB_True/TB_False).
# ─────────────────────────────────────────────────────────────────────────────

def _get_preroll_status(engine):
    """Liest den Pre-Roll-Status direkt aus Pro Tools via PTSL.
    Gibt (ok, enabled) zurück. ok=False bei Kommunikationsfehler."""
    try:
        import ptsl.ops as ops
        import ptsl.PTSL_pb2 as pt
        op = ops.CId_GetTimelineSelection(
            location_type=pt.TLType_TimeCode)
        ok, _ = _ptsl_call(
            lambda: engine.client.run(op) or True,
            label="GetPreRoll", timeout=8.0
        )
        if ok:
            return True, bool(op.response.pre_roll_enabled)
        return False, None
    except Exception as e:
        logging.warning(f"[GetPreRoll] Exception: {e}")
        return False, None

def _set_preroll_state(engine, enabled: bool):
    """Setzt Pre-Roll ein/aus via PTSL.
    Liest zuerst die aktuelle Timeline-Selection, damit in_time/out_time
    beim Setzen erhalten bleiben (sonst springt der Cursor zum Anfang).
    Gibt True bei Erfolg zurück."""
    try:
        import ptsl.ops as ops
        import ptsl.PTSL_pb2 as pt
        tb_val = pt.TB_True if enabled else pt.TB_False

        # 1. Aktuelle Timeline-Selection lesen (in_time benötigt!)
        get_op = ops.CId_GetTimelineSelection(location_type=pt.TLType_TimeCode)
        ok_get, _ = _ptsl_call(
            lambda: engine.client.run(get_op) or True,
            label="GetTimeline", timeout=8.0
        )
        if ok_get and get_op.response.in_time:
            # in_time zurückgeben damit Cursor-Position erhalten bleibt
            ok, _ = _ptsl_call(
                lambda: engine.set_timeline_selection(
                    in_time=get_op.response.in_time,
                    pre_roll_enabled=tb_val
                ),
                label=f"SetPreRoll({'EIN' if enabled else 'AUS'})",
                timeout=8.0
            )
        else:
            # Fallback: ohne in_time (nur pre_roll_enabled)
            set_op = ops.CId_SetTimelineSelection(pre_roll_enabled=tb_val)
            ok, _ = _ptsl_call(
                lambda: engine.client.run(set_op),
                label=f"SetPreRoll({'EIN' if enabled else 'AUS'})",
                timeout=8.0
            )
        return ok
    except Exception as e:
        logging.warning(f"[SetPreRoll] Exception: {e}")
        return False

def ensure_preroll_on(engine=None):
    """Stellt sicher, dass Pre-Roll VOR der Aufnahme AN ist (PTSL, 3 Versuche).
    Optimiert für Geschwindigkeit – keine Verifikation beim ersten Versuch.
    WICHTIG: Schließt NIE die Engine (shared Singleton mit run_punch_in)."""
    _own_engine = engine is None
    if _own_engine:
        engine = _get_engine()
    if engine is None:
        logging.warning("Pre-Roll EIN: Engine nicht verfügbar – Fallback cmd+k")
        _send_key(_VK_K, cmd=True)
        time.sleep(0.15)
        return

    for attempt in range(3):
        ok, is_on = _get_preroll_status(engine)
        if ok and is_on:
            logging.info("Pre-Roll ist bereits AN – keine Änderung.")
            return

        label = "AUS" if (ok and not is_on) else "unbekannt"
        logging.info(f"Pre-Roll ist {label} – schalte ein (PTSL, Versuch {attempt+1}/3)...")

        success = _set_preroll_state(engine, True)
        if success:
            logging.info("Pre-Roll EIN gesetzt (PTSL).")
            return

        logging.warning(f"Pre-Roll EIN FEHLER (Versuch {attempt+1}/3)")
        time.sleep(0.3)

    logging.error("⚠️  Pre-Roll EIN FEHLGESCHLAGEN nach 3 PTSL-Versuchen – Fallback cmd+k")
    _send_key(_VK_K, cmd=True)
    time.sleep(0.15)

def restore_preroll(engine=None):
    """Stellt sicher, dass Pre-Roll NACH der Aufnahme AUS ist (PTSL, 3 Versuche).
    WICHTIG: Schließt NIE die Engine (shared Singleton mit run_punch_in)."""
    _own_engine = engine is None
    if _own_engine:
        engine = _get_engine()
    if engine is None:
        logging.warning("Pre-Roll AUS: Engine nicht verfügbar – Fallback cmd+k")
        _send_key(_VK_K, cmd=True)
        time.sleep(0.15)
        return

    for attempt in range(3):
        ok, is_on = _get_preroll_status(engine)
        if ok and not is_on:
            logging.info("Pre-Roll ist bereits AUS – keine Änderung.")
            return

        label = "AN" if (ok and is_on) else "unbekannt"
        logging.info(f"Pre-Roll ist {label} – schalte aus (PTSL, Versuch {attempt+1}/3)...")

        success = _set_preroll_state(engine, False)
        if success:
            logging.info("Pre-Roll AUS gesetzt (PTSL).")
            return

        logging.warning(f"Pre-Roll AUS FEHLER (Versuch {attempt+1}/3)")
        time.sleep(0.3)

    logging.error("⚠️  Pre-Roll AUS FEHLGESCHLAGEN nach 3 PTSL-Versuchen – Fallback cmd+k")
    _send_key(_VK_K, cmd=True)
    time.sleep(0.15)



# ─────────────────────────────────────────────────────────────────────────────
# PTSL Helper
# ─────────────────────────────────────────────────────────────────────────────

# ── Singleton Engine (verhindert Verbindungslecks) ───────────────────────
_engine_instance = None
_engine_lock = threading.Lock()

# Harte gRPC-Deadline pro PTSL-Befehl. py-ptsl reicht selbst kein timeout an den
# gRPC-Stub durch, wodurch ein hängender Call (z. B. NEXIS-Cold-Start) ewig
# blockieren konnte. _install_grpc_deadline() injiziert deshalb eine echte
# Deadline; _ptsl_call setzt den konkreten Wert pro Aufruf via Thread-Local.
_GRPC_CALL_DEADLINE = 15.0
_grpc_deadline_tls = threading.local()


def _current_grpc_deadline() -> float:
    return getattr(_grpc_deadline_tls, "value", _GRPC_CALL_DEADLINE)


def _install_grpc_deadline(engine):
    """Hüllt SendGrpcRequest des gRPC-Stubs so, dass jeder Call eine echte
    Deadline erhält (DEADLINE_EXCEEDED statt unbegrenztem Hängen)."""
    try:
        raw = engine.client.raw_client
        orig = raw.SendGrpcRequest
        if getattr(orig, "_pb_deadline_wrapped", False):
            return

        def _with_deadline(request, *args, **kwargs):
            kwargs.setdefault("timeout", _current_grpc_deadline())
            return orig(request, *args, **kwargs)

        _with_deadline._pb_deadline_wrapped = True
        raw.SendGrpcRequest = _with_deadline
    except Exception as e:
        logging.warning(f"gRPC-Deadline-Wrapper nicht installiert: {e}")


def _get_engine():
    """Wiederverwendbare PTSL-Engine (Singleton). Verbindet bei Bedarf neu.

    Kein proaktiver Health-Check mehr: Da jeder Call eine harte gRPC-Deadline
    hat (_install_grpc_deadline), werden tote/blockierte Verbindungen reaktiv
    erkannt – ein fehlgeschlagener _ptsl_call verwirft die Instanz via
    _reset_engine(), der nächste _get_engine() verbindet neu. Der Keep-Alive
    hält die Verbindung zusätzlich warm.
    """
    global _engine_instance
    with _engine_lock:
        if _engine_instance is not None:
            return _engine_instance
        try:
            eng = ptsl.Engine(company_name="PunchBuddy", application_name="PunchBuddy")
            _install_grpc_deadline(eng)
            _engine_instance = eng
            logging.info("PTSL verbunden.")
            return eng
        except Exception as e:
            logging.error(f"PTSL Verbindung fehlgeschlagen: {e}")
            return None


def _safe_close(eng):
    try:
        eng.close()
    except Exception:
        pass


def _reset_engine():
    """Verwirft die aktuelle Engine-Instanz → nächster _get_engine() verbindet
    neu. Wird bei Verbindungsfehlern (gRPC-Deadline/RpcError) aufgerufen.
    Das close() läuft im Hintergrund, damit ein toter Channel den Aufrufer
    nicht blockiert."""
    global _engine_instance
    with _engine_lock:
        dead = _engine_instance
        _engine_instance = None
    if dead is not None:
        threading.Thread(target=_safe_close, args=(dead,), daemon=True,
                         name="EngineClose").start()


# Rückwärtskompatibler Alias: frühere Aufrufer riefen _invalidate_engine_health.
_invalidate_engine_health = _reset_engine


def _close_engine():
    """Schliesst die Singleton-Engine explizit (Session-Wechsel/Import/Quit)."""
    global _engine_instance
    with _engine_lock:
        dead = _engine_instance
        _engine_instance = None
    if dead is not None:
        _safe_close(dead)
        logging.info("PTSL Engine geschlossen.")
    _invalidate_session_tracks()

_ptsl_lock = threading.Lock()

# ── Session-Track-Cache ───────────────────────────────────────────────────
# Track-Namen ändern sich nicht während einer Session – einmalig lesen und
# cachen. Invalidiert wenn die Engine geschlossen wird (Session-Wechsel,
# Import, Reconnect). Run_play() liest Track-Listen weiterhin live,
# da dort die Selection in Echtzeit benötigt wird.
_cached_track_names: list | None = None
_track_cache_lock = threading.Lock()

def _invalidate_session_tracks():
    global _cached_track_names
    with _track_cache_lock:
        _cached_track_names = None

def refresh_session_tracks(engine) -> bool:
    """Liest Track-Namen frisch aus PT und befüllt den Cache.
    Gibt True zurück bei Erfolg."""
    global _cached_track_names
    ok, tracks = _ptsl_call(engine.track_list, label="TrackListCache", timeout=10.0)
    if ok and tracks is not None:
        names = [t.name for t in tracks]
        with _track_cache_lock:
            _cached_track_names = names
        logging.info(f"Track-Cache befüllt: {len(names)} Spuren.")
        return True
    logging.warning("Track-Cache: track_list() fehlgeschlagen – Cache bleibt leer.")
    return False

def _get_cached_track_names(engine) -> list | None:
    """Gibt gecachte Track-Namen zurück. Befüllt den Cache wenn leer."""
    with _track_cache_lock:
        if _cached_track_names is not None:
            return list(_cached_track_names)
    # Cache leer → einmalig laden
    if refresh_session_tracks(engine):
        with _track_cache_lock:
            return list(_cached_track_names) if _cached_track_names is not None else None
    return None

def _ptsl_call(fn, *args, label: str = "", timeout: float = 15.0):
    """Führt einen PTSL-Aufruf serialisiert (über _ptsl_lock) und mit harter
    gRPC-Deadline aus. Gibt (ok, result) zurück.

    `timeout` ist jetzt die echte gRPC-Deadline des Calls: ein hängender Befehl
    wird vom gRPC-Stack mit DEADLINE_EXCEEDED abgebrochen, statt – wie früher –
    einen verwaisten Thread und gehaltenen Lock zu hinterlassen.

    Nur echte Verbindungsfehler (grpc.RpcError) verwerfen die Engine; ein
    fachlicher CommandError lässt die Verbindung bestehen.
    """
    if not _ptsl_lock.acquire(timeout=6.0):  # 6s – NEXIS kann vorherigen Befehl verzögern
        logging.warning(f"[{label}] PTSL-Lock nicht erhalten (6s)")
        return False, None

    _grpc_deadline_tls.value = timeout
    try:
        return True, fn(*args)
    except Exception as e:
        is_rpc = False
        is_timeout = False
        try:
            import grpc
            if isinstance(e, grpc.RpcError):
                is_rpc = True
                is_timeout = (e.code() == grpc.StatusCode.DEADLINE_EXCEEDED)
        except Exception:
            pass
        if is_timeout:
            logging.warning(f"[{label}] gRPC-Deadline ({timeout}s) überschritten")
        else:
            logging.error(f"[{label}] Fehler: {e}")
        if is_rpc:
            _reset_engine()  # Verbindung tot/blockiert → nächster Call reconnectet
        return False, (None if is_timeout else e)
    finally:
        _grpc_deadline_tls.value = _GRPC_CALL_DEADLINE
        _ptsl_lock.release()

def _show_error(title: str, msg: str):
    """Zeigt Fehler-Dialog (non-blocking)."""
    try:
        subprocess.Popen(["osascript", "-e",
            f'display alert "{title}" message "{msg}" as critical'])
    except Exception:
        pass

def _detect_video_track(engine, settings=None):
    """Erkennt die Videospur automatisch via PTSL (type=4).
    Fallback auf settings['video_track'] wenn nichts gefunden."""
    fallback = (settings or {}).get("video_track", "Video 1")
    try:
        tracks = engine.track_list()
        video_tracks = [t.name for t in tracks if t.type == 4]
        if video_tracks:
            name = video_tracks[0]
            logging.info(f"Video-Spur auto-erkannt: '{name}'")
            return name
        else:
            logging.warning(f"Keine Video-Spur (type=4) gefunden – Fallback: '{fallback}'")
            return fallback
    except Exception as e:
        logging.warning(f"Video-Spur Erkennung fehlgeschlagen: {e} – Fallback: '{fallback}'")
        return fallback

# ─────────────────────────────────────────────────────────────────────────────
# Hauptautomatisierung
# ─────────────────────────────────────────────────────────────────────────────
_running      = False
_running_lock = threading.Lock()
_stop_lock    = threading.Lock()  # verhindert gleichzeitige Stop-Aufrufe

def run_punch_in(target_tracks: list, monitor_tracks: list = None):
    global _running
    with _running_lock:
        if _running:
            logging.warning("Läuft bereits – Trigger ignoriert.")
            return
        _running = True
    _set_busy(True)

    # Fallback: wenn keine Monitor-Spuren angegeben, alle Record-Spuren nehmen
    if monitor_tracks is None:
        monitor_tracks = list(target_tracks)

    try:
        logging.info("=== PunchBuddy START ===")
        logging.info(f"Record-Spuren: {target_tracks}")
        logging.info(f"Monitor-Spuren: {monitor_tracks}")

        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfügbar – Abbruch.")
            return

        # ── 1. Session-Spuren aus Cache – kein PTSL-Call wenn bereits bekannt ─
        pt_names = _get_cached_track_names(engine)
        if pt_names is None:
            logging.error("Konnte Session-Spuren nicht lesen (PTSL Timeout) – Abbruch.")
            return

        rec_want     = set(t for t in target_tracks if t in pt_names)
        mon_want     = set(t for t in monitor_tracks if t in pt_names)

        logging.info(f"Session-Spuren (gecacht): {pt_names}")
        logging.info(f"Record SOLL:    {sorted(rec_want)}")
        logging.info(f"Monitor SOLL:   {sorted(mon_want)}")

        if not rec_want:
            logging.warning("Keine Record-Spuren in der Session gefunden – Abbruch.")
            return

        # ══════════════════════════════════════════════════════════════════
        # VOR AUFNAHME – strikt sequentiell, mit Settling-Delays
        # ══════════════════════════════════════════════════════════════════

        # ── Schritt 1: Pre-Roll EIN ──────────────────────────────────────
        ensure_preroll_on(engine)
        time.sleep(0.2)  # CHANGE: Settling nach Pre-Roll setzen

        # ── Schritt 2: Record Enable (3 Versuche) ───────────────────────
        # Timeout 15s: NEXIS-Schreiblast kann set_track_record_enable_state 10-11s verzögern
        rec_ok = False
        for attempt in range(3):
            ok, _ = _ptsl_call(
                engine.set_track_record_enable_state, sorted(rec_want), True,
                label=f"RecEnable#{attempt+1}", timeout=15.0
            )
            if ok:
                logging.info(f"Record Enable AN: {sorted(rec_want)}")
                rec_ok = True
                break
            logging.warning(f"Record Enable FEHLER (Versuch {attempt+1}/3)")
            time.sleep(0.5 * (attempt + 1))
        if not rec_ok:
            logging.warning("Record Enable fehlgeschlagen – mache trotzdem weiter.")

        time.sleep(0.3)  # CHANGE: Settling – PT muss Rec-Enable verarbeiten bevor Monitor-State geändert wird

        # ── Schritt 3: Input Monitor AUS (3 Versuche) ────────────────────
        # AUSKOMMENTIERT AUF WUNSCH (kann später bei Bedarf wieder aktiviert werden)
        """
        if mon_want:
            logging.info(f"Input Monitor AUS für: {sorted(mon_want)}...")
            for attempt in range(3):
                ok, _ = _ptsl_call(
                    engine.set_track_input_monitor_state, sorted(mon_want), False,
                    label=f"MonOff#{attempt+1}", timeout=5.0
                )
                if ok:
                    logging.info("Input Monitor AUS OK")
                    break
                logging.warning(f"Input Monitor AUS FEHLER (Versuch {attempt+1}/3)")
                time.sleep(0.5 * (attempt + 1))
        """

        time.sleep(0.5)  # CHANGE: Settling – Monitor-State-Wechsel muss stabilisieren bevor F12.
                         # Das ist der wahrscheinliche Fix gegen Digital-Null an Clip-Boundaries.

        # CHANGE: Defensives Logging der Timeline-Selection vor F12.
        # Falls das Problem nochmal auftritt, sehen wir genau was PT hatte.
        try:
            import ptsl.ops as ops
            import ptsl.PTSL_pb2 as pt_pb
            sel_op = ops.CId_GetTimelineSelection(location_type=pt_pb.TLType_TimeCode)
            ok_sel, _ = _ptsl_call(
                lambda: engine.client.run(sel_op) or True,
                label="GetSelPreRecord", timeout=6.0
            )
            if ok_sel:
                logging.info(
                    f"[Selection vor F12] in='{sel_op.response.in_time}' "
                    f"out='{sel_op.response.out_time}' "
                    f"preroll_start='{sel_op.response.pre_roll_start_time}' "
                    f"preroll_enabled={sel_op.response.pre_roll_enabled}"
                )
        except Exception as _e:
            logging.debug(f"Selection-Log fehlgeschlagen (unkritisch): {_e}")

        # ── Schritt 4: Transport-Arm EIN + Aufnahme starten (PTSL) ─────────
        # toggle_record_enable (Rec-Arm) + toggle_play_state (Play) = Record+Play
        # Komplett via PTSL – kein F12 / CGEvent nötig, funktioniert unabhängig von Fokus.
        _transport_pre_armed = False
        ok_arm_chk, _pre_arm_state = _ptsl_call(
            engine.transport_armed, label="TransportArmedPre", timeout=5.0)
        if ok_arm_chk:
            _transport_pre_armed = bool(_pre_arm_state)
        if not _transport_pre_armed:
            _ptsl_call(engine.toggle_record_enable, label="RecordArm", timeout=5.0)
            logging.info("Transport Record-Arm: EIN gesetzt (PTSL).")
        else:
            logging.info("Transport war bereits record-armed.")
        time.sleep(0.1)
        _ptsl_call(engine.toggle_play_state, label="RecordStart", timeout=5.0)
        logging.info("Record gestartet (PTSL: record_arm + play).")

        # ── Schritt 5: Warten bis Transport läuft (max 5s) ──────────────
        _transport_started = False
        for _ in range(100):
            ok_ts, ts_r = _ptsl_call(engine.transport_state, label="WaitStart", timeout=6.0)
            if not ok_ts:
                time.sleep(0.05)
                continue
            s = str(ts_r)
            if s in ("TS_TransportRecording", "TS_TransportPlaying",
                     "TS_TransportIsCued", "TS_TransportIsCuedForPreview"):
                logging.info(f"Transport aktiv: {s}")
                _transport_started = True
                break
            time.sleep(0.05)
        if not _transport_started:
            logging.warning("Transport nie gestartet!")

        # ── Schritt 6: Warten bis Transport gestoppt ────────────────────
        last = ""
        consecutive_failures = 0
        _transport_confirmed_stopped = False
        if _transport_started:
            for _ in range(7500):  # max 10 Minuten
                ok_ts, ts_r = _ptsl_call(engine.transport_state, label="WaitStop", timeout=8.0)
                # NEXIS kann transport_state verzögern – 8s Tolerance, Schwelle auf 10
                if not ok_ts:
                    consecutive_failures += 1
                    if consecutive_failures >= 10:
                        logging.warning("Transport-Polling: 10x Timeout – breche ab (Anti-Stau).")
                        _close_engine()
                        time.sleep(2.0)
                        engine = _get_engine()
                        break
                    time.sleep(0.5)
                    continue
                consecutive_failures = 0
                s = str(ts_r)
                if s != last:
                    logging.info(f"Transport: {s}")
                    last = s
                if s == "TS_TransportStopped":
                    logging.info("Transport gestoppt.")
                    _transport_confirmed_stopped = True
                    break
                if s == "TS_TransportIsStopping":
                    # PT schreibt Aufnahme auf NEXIS – PTSL-Last jetzt drastisch reduzieren.
                    # 80ms-Polling würde ~12 Anfragen/s erzeugen genau wenn PT die Dateien
                    # finalisiert, was zu Beachball/Crash führen kann.
                    time.sleep(3.0)
                else:
                    time.sleep(0.08)
            else:
                logging.warning("Transport-Timeout (10 Min).")

        # ══════════════════════════════════════════════════════════════════
        # NACH AUFNAHME – strikt sequentiell, mit Settling-Delays
        # CHANGE: Reihenfolge angepasst – erst Rec-Disable damit PT den Record-
        # Pfad sauber schliesst, dann Pre-Roll AUS, dann Input EIN.
        # ══════════════════════════════════════════════════════════════════

        if _transport_confirmed_stopped:

            time.sleep(0.2)  # CHANGE: Settling nach Stopp, PT muss Record-Pfad finalisieren

            # Schritt 7 (Track RecordDisable) entfernt:
            # Tracks bleiben nach der Aufnahme armed (blau/punch-ready).
            # Das entspricht dem normalen TrackPunch-Workflow und verhindert
            # den NEXIS Cold-Start beim nächsten Record-Trigger nach Idle.

            time.sleep(0.2)  # Settling vor Transport-Arm-Änderung

            # ── Schritt 7b: Transport-Arm AUS (nur wenn wir ihn aktiviert haben) ──
            if not _transport_pre_armed:
                ok_arm2, still_armed = _ptsl_call(
                    engine.transport_armed, label="TransportArmedPost", timeout=5.0)
                if ok_arm2 and still_armed:
                    _ptsl_call(engine.toggle_record_enable, label="RecordDisarm", timeout=5.0)
                    logging.info("Transport Record-Arm: AUS gesetzt.")

            # ── Schritt 8: Pre-Roll AUS ──────────────────────────────────
            restore_preroll(engine)
            time.sleep(0.2)  # CHANGE: Settling nach Pre-Roll-Aus

            # ── Schritt 9: Input Monitor EIN (3 Versuche) ────────────────
            # AUSKOMMENTIERT AUF WUNSCH (kann später bei Bedarf wieder aktiviert werden)
            """
            if mon_want:
                logging.info(f"Input Monitor EIN für: {sorted(mon_want)}...")
                for attempt in range(3):
                    ok, _ = _ptsl_call(
                        engine.set_track_input_monitor_state, sorted(mon_want), True,
                        label=f"MonOn#{attempt+1}", timeout=5.0
                    )
                    if ok:
                        logging.info("Input Monitor EIN OK")
                        break
                    logging.warning(f"Input Monitor EIN FEHLER (Versuch {attempt+1}/3)")
                    time.sleep(0.5 * (attempt + 1))
            """
        else:
            logging.warning("Transport nicht bestätigt gestoppt – State-Wiederherstellung übersprungen.")
            # Pre-Roll trotzdem zurücksetzen (gefahrlos)
            restore_preroll(engine)

        logging.info("Punch-In Durchlauf abgeschlossen.")

    except Exception as e:
        logging.error(f"Fehler: {e}", exc_info=True)
    finally:
        gc.collect()
        with _running_lock:
            _running = False
        _set_busy(False)
        logging.info("=== PunchBuddy ENDE ===")

# ─────────────────────────────────────────────────────────────────────────────
# Play (Monitor Auto) Workflow
# ─────────────────────────────────────────────────────────────────────────────
_play_monitor_tracks = []
_play_custom_active = False

def run_play_custom():
    """Play/Stop-Toggle mit speziellen Mute-States für KH2 und ST Abh.
    Stop-Pfad: delegiert an run_stop() – funktioniert auch während einer Aufnahme.
    Start-Pfad: mutet KH2, entmutet ST Abh und startet Playback."""
    global _running, _play_custom_active
    import ptsl.PTSL_pb2 as pt

    engine = _get_engine()
    if engine is None:
        logging.error("PTSL Engine nicht verfügbar – Abbruch (Play Custom).")
        return

    ok_ts, ts_r = _ptsl_call(engine.transport_state, label="PlayCustomTransportState", timeout=6.0)
    if not ok_ts:
        logging.warning("Play Custom: Konnte Transport-State nicht lesen.")
        return

    state_str = str(ts_r)
    logging.info(f"Play Custom: Transport-State: {state_str}")

    # ── STOP-Pfad ────────────────────────────────────────────────────────────
    if state_str in ("TS_TransportRecording", "TS_TransportPlaying",
                     "TS_TransportIsCued", "TS_TransportIsCuedForPreview",
                     "TS_TransportIsStopping"):
        logging.info("Play Custom: Transport active – stopping (via run_stop)...")
        run_stop()
        return

    # ── START-Pfad ───────────────────────────────────────────────────────────
    with _running_lock:
        if _running:
            logging.warning("Play Custom: Script running – start ignored.")
            return
        _running = True
    _set_busy(True)
    try:
        cfg = _app_ref.settings if _app_ref is not None else load_settings()
        ch1 = cfg.get("play_custom_ch1_track", "")
        ch1_mute_start = cfg.get("play_custom_ch1_mute_start", True)
        ch2 = cfg.get("play_custom_ch2_track", "")
        ch2_mute_start = cfg.get("play_custom_ch2_mute_start", False)

        logging.info("Play Custom Start: setting track mute states...")
        if ch1:
            _ptsl_call(engine.set_track_mute_state, [ch1], ch1_mute_start,
                       label="PlayCustomMuteCh1", timeout=5.0)
        if ch2:
            _ptsl_call(engine.set_track_mute_state, [ch2], ch2_mute_start,
                       label="PlayCustomMuteCh2", timeout=5.0)

        _play_custom_active = True

        time.sleep(0.3)
        logging.info("Play Custom Start: starting playback...")
        _ptsl_call(engine.toggle_play_state, label="PlayCustomToggle", timeout=6.0)

    except Exception as e:
        logging.error(f"Error in run_play_custom: {e}", exc_info=True)
        _play_custom_active = False
    finally:
        with _running_lock:
            _running = False
        _set_busy(False)


def run_play():
    """Play/Stop-Toggle.
    Stop-Pfad: delegiert an run_stop() – funktioniert auch während einer Aufnahme.
    Start-Pfad: setzt Input Monitor EIN und startet Playback."""
    global _running, _play_monitor_tracks
    import ptsl.PTSL_pb2 as pt

    # Transport-State zuerst lesen – ohne _running_lock, damit Stop auch während
    # einer laufenden Aufnahme (run_punch_in) ausgelöst werden kann.
    engine = _get_engine()
    if engine is None:
        logging.error("PTSL Engine nicht verfügbar – Abbruch (Play).")
        return

    ok_ts, ts_r = _ptsl_call(engine.transport_state, label="PlayTransportState", timeout=6.0)
    if not ok_ts:
        logging.warning("Play: Konnte Transport-State nicht lesen.")
        return

    state_str = str(ts_r)
    logging.info(f"Play: Transport-State: {state_str}")

    # ── STOP-Pfad ────────────────────────────────────────────────────────────
    if state_str in ("TS_TransportRecording", "TS_TransportPlaying",
                     "TS_TransportIsCued", "TS_TransportIsCuedForPreview",
                     "TS_TransportIsStopping"):
        logging.info("Play: Transport aktiv – stoppe (via run_stop)...")
        run_stop()
        return

    # ── START-Pfad ───────────────────────────────────────────────────────────
    with _running_lock:
        if _running:
            logging.warning("Play: Script läuft bereits – Play-Start ignoriert.")
            return
        _running = True
    _set_busy(True)
    try:
        settings = load_settings()
        configured_play = settings.get("play_monitor_tracks", [])

        if configured_play:
            logging.info(f"Play: Konfigurierte Play-Spuren: {sorted(configured_play)}")
            tracks_to_mon = list(configured_play)
        else:
            logging.info("Play: Keine Play-Spuren konfiguriert – ermittle selektierte Spuren...")
            tracks_to_mon = []

            # Methode 1: Track-Filter "Selected"
            try:
                sel_filter = pt.TrackListInvertibleFilter(filter=pt.Selected, is_inverted=False)
                ok_sel, selected_tracks = _ptsl_call(
                    lambda: engine.track_list(filters=[sel_filter]),
                    label="TrackListSelected", timeout=5.0
                )
                if ok_sel and selected_tracks:
                    logging.info(f"Play: {len(selected_tracks)} selektierte Spur(en) via Filter.")
                    for t in selected_tracks:
                        if t.type == 1:
                            tracks_to_mon.append(t.name)
                        else:
                            logging.debug(f"Play: '{t.name}' (type={t.type}) kein AudioTrack – übersprungen.")
            except Exception as e:
                logging.warning(f"Play: Filter-Methode fehlgeschlagen ({e}) – Fallback.")

            # Methode 2 (Fallback): alle Tracks iterieren
            if not tracks_to_mon:
                logging.info("Play: Fallback – prüfe is_selected aller Tracks...")
                ok_all, all_tracks = _ptsl_call(engine.track_list, label="TrackListAll", timeout=5.0)
                if ok_all and all_tracks:
                    for t in all_tracks:
                        try:
                            if int(t.track_attributes.is_selected) == 0:
                                continue
                            if t.type != 1:
                                continue
                            tracks_to_mon.append(t.name)
                        except Exception as _e:
                            logging.debug(f"Play: Track '{getattr(t, 'name', '?')}' übersprungen: {_e}")

        logging.info(f"Play: {len(tracks_to_mon)} Monitor-Spuren: {sorted(tracks_to_mon)}")

        if tracks_to_mon:
            mon_ok = False
            for attempt in range(3):
                ok, _ = _ptsl_call(
                    engine.set_track_input_monitor_state,
                    sorted(tracks_to_mon), True,
                    label=f"PlayMonOn#{attempt+1}", timeout=5.0
                )
                if ok:
                    logging.info("Play: Input Monitor EIN OK")
                    mon_ok = True
                    break
                logging.warning(f"Play: Input Monitor EIN FEHLER (Versuch {attempt+1}/3)")
                time.sleep(0.5 * (attempt + 1))
            _play_monitor_tracks = list(tracks_to_mon) if mon_ok else []
            if not mon_ok:
                logging.warning("Play: Monitor-EIN fehlgeschlagen – keine Spuren gemerkt.")
        else:
            _play_monitor_tracks = []
            logging.info("Play: Keine Spur selektiert – starte ohne Monitor-Änderung.")

        time.sleep(0.3)
        logging.info("Play: Starte Playback...")
        _ptsl_call(engine.toggle_play_state, label="PlayToggle", timeout=6.0)

    except Exception as e:
        logging.error(f"Fehler in run_play: {e}", exc_info=True)
    finally:
        with _running_lock:
            _running = False
        _set_busy(False)


def run_stop():
    """Dedizierter Stop: entspricht der Leertaste in Pro Tools.
    Stoppt sowohl Play als auch Recording — UNABHÄNGIG von _running,
    damit ein laufender Punch-In jederzeit unterbrochen werden kann."""
    global _play_monitor_tracks

    # Eigener Lock verhindert Doppel-Stop, blockiert aber keine Aufnahme
    if not _stop_lock.acquire(blocking=False):
        logging.info("Stop: bereits in Ausführung – ignoriert.")
        return
    try:
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfügbar – Abbruch (Stop).")
            return

        ok_ts, ts_r = _ptsl_call(engine.transport_state, label="StopTransportState", timeout=6.0)
        if not ok_ts:
            logging.warning("Stop: Konnte Transport-State nicht lesen.")
            return

        state_str = str(ts_r)
        logging.info(f"Stop: Transport-State: {state_str}")

        if state_str in ("TS_TransportStopped", "TS_TransportIsStopping"):
            logging.info("Stop: Transport steht bereits – nichts zu tun.")
            return

        # toggle_play_state = Leertaste: stoppt Play UND Recording
        logging.info("Stop: Sende Stop (toggle_play_state)...")
        _ptsl_call(engine.toggle_play_state, label="StopToggle", timeout=6.0)

        # Kurze Pause: PT braucht Zeit für den NEXIS-Write ohne PTSL-Unterbrechung
        time.sleep(1.0)
        # Warten bis PT gestoppt – bei 2 aufeinanderfolgenden Fehlern Abbruch (PT hängt)
        consecutive_errors = 0
        for _ in range(20):
            time.sleep(0.5)
            ok2, ts2 = _ptsl_call(engine.transport_state, label="StopWait", timeout=3.0)
            if ok2:
                consecutive_errors = 0
                if str(ts2) in ("TS_TransportStopped", "TS_TransportIsStopping"):
                    break
            else:
                err_str = str(ts2).lower() if ts2 else ""
                if "closed channel" in err_str or "connection refused" in err_str:
                    logging.warning("Stop: PTSL-Kanal geschlossen – PT nicht mehr erreichbar.")
                    break
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    logging.warning("Stop: 2x PTSL-Fehler – PT möglicherweise hängend, Abbruch.")
                    break

        # Play-Monitor zurücksetzen falls ein /play aktiv war
        if _play_monitor_tracks:
            time.sleep(0.3)
            logging.info(f"Stop: Input Monitor AUS für {sorted(_play_monitor_tracks)}...")
            for attempt in range(3):
                ok, _ = _ptsl_call(
                    engine.set_track_input_monitor_state,
                    sorted(_play_monitor_tracks), False,
                    label=f"StopMonOff#{attempt+1}", timeout=5.0
                )
                if ok:
                    logging.info("Stop: Input Monitor AUS OK")
                    break
                logging.warning(f"Stop: Input Monitor AUS FEHLER (Versuch {attempt+1}/3)")
                time.sleep(0.5 * (attempt + 1))
            _play_monitor_tracks = []

        # Play-Custom-Mutes zurücksetzen falls ein /play_custom aktiv war
        global _play_custom_active
        if _play_custom_active:
            time.sleep(0.3)
            cfg = _app_ref.settings if _app_ref is not None else load_settings()
            ch1 = cfg.get("play_custom_ch1_track", "")
            ch1_mute_stop = cfg.get("play_custom_ch1_mute_stop", False)
            ch2 = cfg.get("play_custom_ch2_track", "")
            ch2_mute_stop = cfg.get("play_custom_ch2_mute_stop", True)
            logging.info(f"Stop: Custom Play war aktiv – Mute-States wiederherstellen "
                         f"({ch1}→{'mute' if ch1_mute_stop else 'unmute'}, "
                         f"{ch2}→{'mute' if ch2_mute_stop else 'unmute'})...")
            ok1, ok2 = True, True
            if ch1:
                ok1, _ = _ptsl_call(engine.set_track_mute_state, [ch1], ch1_mute_stop,
                                    label="StopRestoreCh1", timeout=5.0)
            if ch2:
                ok2, _ = _ptsl_call(engine.set_track_mute_state, [ch2], ch2_mute_stop,
                                    label="StopRestoreCh2", timeout=5.0)
            if ok1 and ok2:
                logging.info("Stop: Mute-States wiederherstellen OK")
            else:
                if not ok1: logging.warning(f"Stop: Mute-Restore '{ch1}' FEHLER (Track ggf. nicht in Session)")
                if not ok2: logging.warning(f"Stop: Mute-Restore '{ch2}' FEHLER (Track ggf. nicht in Session)")
            _play_custom_active = False

        logging.info("Stop: abgeschlossen.")

    except Exception as e:
        logging.error(f"Fehler in run_stop: {e}", exc_info=True)
    finally:
        _stop_lock.release()

def run_goto_start():
    """Setzt den Pro Tools Cursor auf den in den Einstellungen definierten Start-Timecode."""
    engine = _get_engine()
    if engine is None:
        logging.error("PTSL Engine nicht verfügbar – Abbruch (GotoStart).")
        return
    tc = load_settings().get("export_start_tc", "10:00:00:00") + ".00"
    logging.info(f">>> GOTO START: {tc} <<<")
    ok, _ = _ptsl_call(
        lambda: engine.set_timeline_selection(in_time=tc, out_time=tc),
        label="GotoStart", timeout=5.0
    )
    if ok:
        logging.info(f"GotoStart: Cursor auf {tc} gesetzt.")
    else:
        logging.warning(f"GotoStart: set_timeline_selection fehlgeschlagen.")

# ─────────────────────────────────────────────────────────────────────────────
# Menüleisten-Status (Idle / Busy)
# ─────────────────────────────────────────────────────────────────────────────
_app_ref = None          # wird in PunchBuddyApp.__init__ gesetzt
_ICON_IDLE = "⏺"        # normaler Zustand
_ICON_BUSY = "🔴"        # Script arbeitet

def _set_busy(busy: bool):
    """Setzt den Menüleisten-Indikator auf rot (busy) oder normal (idle)."""
    global _app_ref
    if _app_ref is not None:
        try:
            _app_ref.title = _ICON_BUSY if busy else _ICON_IDLE
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Export-Workflow
# ─────────────────────────────────────────────────────────────────────────────
_export_lock = threading.Lock()
_export_running = False
_VK_OE = 41  # Ö auf QWERTZ-Tastatur (= Semicolon-Position auf US)
_EXTEND_COUNT = 7  # Anzahl Spuren unter V1 die ausgedehnt werden

def _send_shift_oe(count=1):
    """Sendet Shift+Ö an Pro Tools um die Edit-Selection nach unten auszudehnen."""
    try:
        import Quartz
        pid = _pt_pid()
        if not pid:
            logging.warning("Shift+Ö: PT PID nicht gefunden")
            return False
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for _ in range(count):
            ev_dn = Quartz.CGEventCreateKeyboardEvent(src, _VK_OE, True)
            ev_up = Quartz.CGEventCreateKeyboardEvent(src, _VK_OE, False)
            Quartz.CGEventSetFlags(ev_dn, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventSetFlags(ev_up, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventPostToPid(pid, ev_dn)
            Quartz.CGEventPostToPid(pid, ev_up)
            time.sleep(0.05)
        return True
    except Exception as e:
        logging.error(f"Shift+Ö senden: {e}")
        return False

_VK_F13   = 105   # F13
_VK_RETURN = 36   # Enter/Return
_VK_TAB    = 48   # Tab
_VK_DOWN   = 125  # Arrow Down


# ─────────────────────────────────────────────────────────────────────────────
# Interplay Export (UI-Automation)
# ─────────────────────────────────────────────────────────────────────────────

def _find_newest_pt_log():
    """Returns path to the newest Pro Tools log file, or None."""
    import glob as _glob
    files = _glob.glob(os.path.expanduser("~/Library/Logs/Avid/Pro_Tools_*.txt"))
    return max(files, key=os.path.getmtime) if files else None


def _poll_pt_log_for_export(log_path, start_pos, timeout=90):
    """
    Watches PT log for MediaCentral export result.
    Uses kqueue (instant file-write notification on macOS) with 0.3s polling as fallback.
    Returns (success: True/False/None, message: str).
    """
    SUCCESS_MARKER = "The selected tracks were successfully placed into the Pro Tools sequence"
    ERROR_MARKER   = "SLnk_MachineMgr::SendDialogOnScreen: 108846091"

    if not log_path:
        return None, "PT Log nicht verfügbar"

    def _check(chunk):
        if SUCCESS_MARKER in chunk:
            return True, "Export erfolgreich (PT Log)"
        if ERROR_MARKER in chunk:
            idx = chunk.find(ERROR_MARKER)
            end = chunk.find("\n", idx)
            line = chunk[idx : end if end > idx else idx + 300]
            if "successfully" in line.lower():
                return True, "Export erfolgreich (PT Log)"
            return False, f"Export-Dialog: {line.strip()[:120]}"
        return None, None

    deadline = time.time() + timeout
    active_log, cur_pos = log_path, start_pos

    # ── kqueue: sofortige Datei-Event-Benachrichtigung (macOS) ───────────────
    try:
        fd = os.open(active_log, os.O_RDONLY | os.O_NONBLOCK)
        kq = select.kqueue()
        try:
            ke = select.kevent(fd,
                               filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
            while time.time() < deadline:
                remaining = deadline - time.time()
                events = kq.control([ke], 4, min(remaining, 2.0))

                # Log-Rotation prüfen (PT startet neues Log)
                newest = _find_newest_pt_log()
                if newest and newest != active_log:
                    logging.debug(f"  kqueue: neues PT-Log → {newest}")
                    os.close(fd); kq.close()
                    active_log, cur_pos = newest, 0
                    fd = os.open(active_log, os.O_RDONLY | os.O_NONBLOCK)
                    kq = select.kqueue()
                    ke = select.kevent(fd,
                                       filter=select.KQ_FILTER_VNODE,
                                       flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                                       fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
                    continue

                if not events:
                    continue

                try:
                    sz = os.path.getsize(active_log)
                    if sz > cur_pos:
                        with open(active_log, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(cur_pos)
                            chunk = f.read()
                            cur_pos = f.tell()
                        ok, msg = _check(chunk)
                        if ok is not None:
                            return ok, msg
                except Exception as e:
                    logging.debug(f"  kqueue read: {e}")
        finally:
            try: os.close(fd)
            except: pass
            try: kq.close()
            except: pass
        return None, "PT Log Timeout – Ergebnis unbekannt"

    except Exception as e:
        logging.debug(f"  kqueue nicht verfügbar ({e}) – nutze Polling")

    # ── Polling-Fallback (0.3s Intervall) ────────────────────────────────────
    while time.time() < deadline:
        try:
            newest = _find_newest_pt_log()
            if newest and newest != active_log:
                active_log, cur_pos = newest, 0
            if active_log and os.path.exists(active_log) and os.path.getsize(active_log) > cur_pos:
                with open(active_log, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(cur_pos)
                    chunk = f.read()
                    cur_pos = f.tell()
                ok, msg = _check(chunk)
                if ok is not None:
                    return ok, msg
        except Exception as e:
            logging.debug(f"  PT Log Poll: {e}")
        time.sleep(0.3)

    return None, "PT Log Timeout – Ergebnis unbekannt"


def _wait_for_pt_modal_dismissed(log_path, log_pos, timeout=5.0):
    """
    Wartet via kqueue bis PT den Modal-Dialog vollständig verarbeitet hat.
    PT schreibt 'SLnk_MachineMgr::SendDialogOnScreen: 108846044' ins Log
    genau dann wenn der Modal-Dialog geschlossen und der Event-Loop wieder frei ist.
    Zuverlässiger als CGWindowList (funktioniert auch bei Sheets/Panel-Dialogen).
    """
    DISMISS_MARKER = "SLnk_MachineMgr::SendDialogOnScreen: 108846044"
    if not log_path or not os.path.exists(log_path):
        time.sleep(1.0)
        return

    deadline = time.time() + timeout
    cur_pos = log_pos
    try:
        fd = os.open(log_path, os.O_RDONLY | os.O_NONBLOCK)
        kq = select.kqueue()
        try:
            ke = select.kevent(fd,
                               filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
            while time.time() < deadline:
                remaining = deadline - time.time()
                events = kq.control([ke], 4, min(remaining, 1.0))
                if events:
                    sz = os.path.getsize(log_path)
                    if sz > cur_pos:
                        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(cur_pos)
                            chunk = f.read()
                            cur_pos = f.tell()
                        if DISMISS_MARKER in chunk:
                            logging.info("  Interplay: Modal geschlossen (PT Log)")
                            time.sleep(0.1)
                            return
        finally:
            try: os.close(fd)
            except: pass
            try: kq.close()
            except: pass
    except Exception as e:
        logging.debug(f"  PT Modal-Dismiss kqueue: {e}")

    logging.debug("  Interplay: Modal-Dismiss Timeout – weiter")
    time.sleep(0.3)
def _run_applescript(script):
    """Fuehrt ein AppleScript aus und gibt (returncode, stdout, stderr) zurueck."""
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

def _read_pt_export_result(timeout=12, settings=None):
    """Liest den Inhalt des Pro Tools Bestätigungsfensters nach dem Export.
    Überspringt Hauptfenster (Edit/Mix/Session).
    Gibt (window_title, window_text, success) zurück.
    success=True  → Erfolgs-Keywords gefunden
    success=False → Fehler-Keywords gefunden
    success=None  → kein eindeutiges Ergebnis (Fenster nicht lesbar / kein Keyword)"""

    script = '''
        tell application "System Events"
            tell process "Pro Tools"
                repeat with w in windows
                    try
                        set wTitle to name of w
                        if wTitle does not contain "Edit:" and ¬
                           wTitle does not contain "Mix:" and ¬
                           wTitle does not contain "Transport" and ¬
                           wTitle is not "" then
                            set textContent to ""
                            try
                                repeat with st in (every static text of w)
                                    try
                                        set v to value of st
                                        if v is not missing value and v is not "" then
                                            set textContent to textContent & v & " | "
                                        end if
                                    end try
                                end repeat
                            end try
                            return wTitle & "|||" & textContent
                        end if
                    end try
                end repeat
                return "NO_DIALOG|||"
            end tell
        end tell
    '''

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = _run_applescript(script)
            if rc == 0 and "|||" in out:
                parts = out.split("|||", 1)
                title = parts[0].strip()
                text  = parts[1].strip() if len(parts) > 1 else ""
                if title and title != "NO_DIALOG":
                    combined = (title + " " + text).lower()
                    # Keywords aus Settings laden, Fallback auf Defaults
                    _s = settings or {}
                    _err_raw = _s.get("export_error_keywords",
                        "error,fail,fehler,unsuccessful,could not,unable,problem,warning,aborted,abgebrochen")
                    _suc_raw = _s.get("export_success_keywords",
                        "success,complete,finished,done,exported,erfolgreich,abgeschlossen,fertig")
                    error_kw   = [k.strip().lower() for k in _err_raw.split(",") if k.strip()]
                    success_kw = [k.strip().lower() for k in _suc_raw.split(",") if k.strip()]
                    if any(k in combined for k in error_kw):
                        return title, text, False
                    if any(k in combined for k in success_kw):
                        return title, text, True
                    return title, text, None   # Fenster gefunden, Ergebnis unklar
        except Exception:
            pass
        time.sleep(0.1)
    return "", "", None   # Kein Fenster gefunden


def _wait_for_pt_window(title_contains, timeout=30, gone=False):
    """
    Wartet bis ein Pro Tools Fenster mit dem Titel erscheint (gone=False)
    oder verschwindet (gone=True). Gibt True zurueck bei Erfolg.
    """
    script = f'''
        tell application "System Events"
            tell process "Pro Tools"
                set wNames to name of every window
            end tell
        end tell
        return wNames as text
    '''
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = _run_applescript(script)
            if rc == 0:
                found = title_contains.lower() in out.lower()
                if (not gone and found) or (gone and not found):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_for_consolidate_window_gone(timeout=60):
    """
    Wartet bis das Consolidate-Fenster ("Zusammenführen", "Consolidating" etc.) verschwindet.
    """
    time.sleep(0.5)  # Kurz warten, damit das Fenster Zeit hat sich zu öffnen
    
    script = '''
        tell application "System Events"
            tell process "Pro Tools"
                set wNames to name of every window
            end tell
        end tell
        return wNames as text
    '''
    
    keywords = ["consolidat", "zusammenführ", "consolida", "consolidando"]
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = _run_applescript(script)
            if rc == 0:
                found = False
                out_lower = out.lower()
                for kw in keywords:
                    if kw in out_lower:
                        found = True
                        break
                if not found:
                    logging.info("  Consolidate-Fenster nicht mehr aktiv.")
                    return True
                logging.info("  Warte auf Abschluss des Consolidate-Vorgangs in Pro Tools...")
        except Exception as e:
            logging.debug(f"Fehler bei _wait_for_consolidate_window_gone: {e}")
        time.sleep(0.5)
    logging.warning("  Timeout beim Warten auf das Consolidate-Fenster.")
    return False

def _wait_for_consolidated_files(session_dir, track_names, timeout=10):
    """
    Wartet bis für jeden Spurnamen eine .wav-Datei im "Audio Files"-Ordner existiert,
    die eine Größe > 0 hat und deren Größe sich über einen Zeitraum von 0.2s nicht mehr ändert.
    """
    audio_dir = os.path.join(session_dir, "Audio Files")
    if not os.path.isdir(audio_dir):
        return False
        
    logging.info("  Warte auf Stabilität der exportierten Audiodateien...")
    deadline = time.time() + timeout
    
    # Letzte bekannte Modifikationszeiten und Dateigrößen
    last_states = {}
    
    while time.time() < deadline:
        stable_count = 0
        
        for track_name in track_names:
            latest_file = None
            latest_mtime = -1.0
            
            try:
                for f in os.listdir(audio_dir):
                    base = os.path.splitext(f)[0]
                    if (base == track_name or 
                        base.startswith(track_name + "_") or 
                        base.startswith(track_name + ".") or 
                        base.startswith(track_name + "-")) and f.lower().endswith(".wav"):
                        full = os.path.join(audio_dir, f)
                        mtime = os.path.getmtime(full)
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                            latest_file = full
            except Exception:
                continue
                
            if latest_file:
                try:
                    size = os.path.getsize(latest_file)
                except Exception:
                    size = 0
                
                prev_size, prev_time = last_states.get(track_name, (None, None))
                now = time.time()
                
                if size > 0 and prev_size == size and (now - prev_time) >= 0.2:
                    stable_count += 1
                else:
                    last_states[track_name] = (size, now)
            else:
                pass
                
        if stable_count == len(track_names):
            logging.info("  Alle exportierten Audiodateien sind stabil und vollständig.")
            return True
            
        time.sleep(0.1)
        
    logging.warning("  Erreichte Timeout beim Warten auf stabile Audiodateien – fahre fort.")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Interplay Import (Avid Interplay Access → Pro Tools)
# ─────────────────────────────────────────────────────────────────────────────
_import_running = False
_import_lock = threading.Lock()

def _run_applescript_safe(script, timeout=30, label=""):
    """Führt AppleScript aus, fängt Timeouts sauber ab. Gibt (ok, stdout) zurück."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        out = proc.stdout.strip()
        if proc.returncode != 0:
            err = proc.stderr.strip()
            logging.warning(f"  [{label}] AppleScript returncode {proc.returncode}: {err}")
        return True, out
    except subprocess.TimeoutExpired:
        logging.warning(f"  [{label}] AppleScript Timeout nach {timeout}s")
        return False, ""
    except Exception as e:
        logging.error(f"  [{label}] AppleScript Fehler: {e}")
        return False, ""

def run_interplay_import():
    """Führt den Interplay Import (Avid Interplay Access → Pro Tools) schrittweise aus."""
    global _import_running
    with _import_lock:
        if _import_running:
            logging.warning("Import laeuft bereits – ignoriert.")
            return
        _import_running = True
    prog = None
    _set_busy(True)

    try:
        logging.info("=== INTERPLAY IMPORT START ===")
        prog = _show_progress_win(t("prog_title_import"))
        prog["update"](0.03, t("prog_check_session"))

        # ── Schritt 0: Offene Session schließen (falls aktiviert) ─────
        settings = load_settings()
        if settings.get("import_close_session", True):
            logging.info("  Import Schritt 0: Prüfe ob eine Session offen ist...")
            try:
                engine = _get_engine()
                if engine is not None:
                    try:
                        sess_name = engine.session_name()
                        if sess_name:
                            prog["update"](0.08, t("prog_close_session").format(sess_name))
                            logging.info(f"  Session '{sess_name}' ist offen – wird gespeichert und geschlossen...")
                            engine.save_session()
                            logging.info(f"  Session '{sess_name}' gespeichert.")
                            engine.close_session(save_on_close=True)
                            logging.info(f"  Session '{sess_name}' geschlossen.")
                            # Singleton zurücksetzen: PTSL-Verbindung zur geschlossenen
                            # Session ist ungültig – neue Session braucht neue Verbindung
                            _close_engine()
                            time.sleep(1.5)  # Pro Tools braucht kurz zum Schließen
                        else:
                            logging.info("  Keine Session offen – überspringe.")
                    except Exception as e:
                        # session_name() wirft Exception wenn keine Session offen
                        logging.info(f"  Keine Session offen (oder Fehler: {e}) – fahre fort.")
                else:
                    logging.info("  PTSL Engine nicht verfügbar – überspringe Session-Check.")
            except Exception as e:
                logging.warning(f"  Session-Check fehlgeschlagen: {e} – fahre trotzdem fort.")

        # ── Schritt 1: Interplay Access – Sequenzname kopieren ────────
        # Nutzt CGEvent statt AppleScript System Events (keine Accessibility-Probleme)
        _VK_RETURN = 36   # Return (lokal, da nur hier gebraucht)
        _VK_1      = 18   # '1'

        prog["update"](0.15, t("prog_copy_seq_name"))
        logging.info("  Import Schritt 1: Interplay Access – Sequenzname kopieren...")
        _activate_app("interplayAccess")
        try:
            _run_applescript(
                'tell application "System Events" to '
                'set frontmost of (first process whose name contains "Interplay") to true'
            )
        except Exception:
            pass
        time.sleep(0.8)

        # F2 → Cmd+A → Cmd+C (via AppleScript System Events, damit Java-Clipboard
        # mit macOS-Systemclipboard synchronisiert wird) → ESC
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to key code 120'  # F2
        )
        time.sleep(0.6)
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to keystroke "a" using command down'
        )
        time.sleep(0.3)
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to keystroke "c" using command down'
        )
        time.sleep(0.5)
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to key code 53'  # ESC
        )
        time.sleep(0.3)

        try:
            seq_name = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if seq_name:
                logging.info(f"  Import Schritt 1: Sequenzname: '{seq_name}'")
            else:
                logging.warning("  Import Schritt 1: Clipboard leer – F2/Cmd+C hat nicht funktioniert.")
        except Exception:
            seq_name = ""

        # Cmd+Shift+P via AppleScript: Java-Menüaktion wird so zuverlässig ausgelöst
        prog["update"](0.35, t("prog_send_seq_to_pt"))
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to keystroke "p" using {command down, shift down}'
        )
        logging.info("  Import Schritt 1 OK.")
        time.sleep(1.0)

        # ── Schritt 2: Auf "… (offline)"-Fenster warten, Name einfügen ──
        # Pro Tools öffnet nach Cmd+Shift+P aus Interplay ein Fenster dessen
        # Titel sich mit jedem PT-Update ändert ("ProTools Ultimate 2026.4 (offline)").
        # "(offline)" ist der versionsstabile Substring – darauf warten statt blind schlafen.
        prog["update"](0.43, t("prog_wait_pt_dialog"))
        logging.info("  Import Schritt 2: Warte auf Pro Tools '(offline)'-Dialog...")
        _activate_app("Pro Tools")
        if _wait_for_pt_window("offline", timeout=30):
            logging.info("  Import Schritt 2: Fenster gefunden.")
        else:
            logging.warning("  Import Schritt 2: '(offline)'-Fenster nach 30s nicht erschienen – versuche trotzdem.")
        prog["update"](0.55, t("prog_paste_session_path"))
        time.sleep(0.3)
        _send_key(_VK_V, cmd=True)  # Sequenzname einfügen
        time.sleep(0.3)
        _send_key(_VK_RETURN)       # Bestätigen / Fenster schließen
        logging.info("  Import Schritt 2 OK.")

        # ── Schritt 3: Auf "Import Session Data" Fenster warten ───────
        prog["update"](0.62, t("prog_wait_import_session_data"))
        logging.info("  Import Schritt 3: Warte auf 'Import Session Data'...")
        if not _wait_for_pt_window("Import Session Data", timeout=60):
            logging.error("  Import Schritt 3 FEHLER: 'Import Session Data' nicht erschienen.")
            return
        logging.info("  Import Schritt 3 OK – Fenster gefunden.")
        time.sleep(0.5)

        # ── Schritt 4: Match All + Playlist-Option setzen ─────────────
        # (Button-Clicks via AppleScript – braucht KEIN keystroke-Permission)
        prog["update"](0.70, t("prog_set_import_options"))
        logging.info("  Import Schritt 4: Match All + Playlist-Option...")
        ok, out = _run_applescript_safe('''
            try
                tell application "System Events"
                    tell process "Pro Tools"
                        set frontmost to true
                        set importWin to window "Import Session Data"

                        -- Match All
                        try
                            click button "Match All" of importWin
                        end try
                        delay 0.5

                        -- Playlist-Option: Import - Overlay New On Existing Playlists
                        set foundOption to false
                        try
                            click pop up button "Main Playlist Options" of importWin
                            delay 0.2
                            click menu item "Import - Overlay New On Existing Playlists" of menu 1 of pop up button "Main Playlist Options" of importWin
                            set foundOption to true
                        on error
                            try
                                click radio button "Import - Overlay New On Existing Playlists" of importWin
                                set foundOption to true
                            on error
                                try
                                    click button "Import - Overlay New On Existing Playlists" of importWin
                                    set foundOption to true
                                end try
                            end try
                        end try

                        if not foundOption then
                            set allPopUps to pop up buttons of importWin
                            repeat with p in allPopUps
                                try
                                    click p
                                    delay 0.2
                                    if exists menu item "Import - Overlay New On Existing Playlists" of menu 1 of p then
                                        click menu item "Import - Overlay New On Existing Playlists" of menu 1 of p
                                        set foundOption to true
                                        exit repeat
                                    else
                                        key code 53
                                        delay 0.1
                                    end if
                                end try
                            end repeat
                        end if

                        if not foundOption then
                            set allGroups to groups of importWin
                            repeat with g in allGroups
                                set groupPopUps to pop up buttons of g
                                repeat with p in groupPopUps
                                    try
                                        click p
                                        delay 0.2
                                        if exists menu item "Import - Overlay New On Existing Playlists" of menu 1 of p then
                                            click menu item "Import - Overlay New On Existing Playlists" of menu 1 of p
                                            set foundOption to true
                                            exit repeat
                                        else
                                            key code 53
                                            delay 0.1
                                        end if
                                    end try
                                end repeat
                                if foundOption then exit repeat
                                try
                                    click radio button "Import - Overlay New On Existing Playlists" of g
                                    set foundOption to true
                                    exit repeat
                                end try
                                try
                                    click button "Import - Overlay New On Existing Playlists" of g
                                    set foundOption to true
                                    exit repeat
                                end try
                            end repeat
                        end if
                    end tell
                end tell
                if foundOption then
                    return "OK"
                else
                    return "WARN:Playlist-Option nicht gefunden"
                end if
            on error errMsg
                return "ERROR:" & errMsg
            end try
        ''', timeout=20, label="MatchAll")
        if "ERROR" in out:
            logging.error(f"  Import Schritt 4 FEHLER: {out}")
            return
        if "WARN" in out:
            logging.warning(f"  Import Schritt 4: {out}")
        else:
            logging.info("  Import Schritt 4 OK.")
        time.sleep(0.3)

        # ── Schritt 5: Choose + Track Data Preset ─────────────────────
        # (Button-Clicks via AppleScript, Preset-Shortcut via CGEvent)
        logging.info("  Import Schritt 5: Choose + Track Data Preset...")
        ok, out = _run_applescript_safe('''
            try
                tell application "System Events"
                    tell process "Pro Tools"
                        set frontmost to true
                        set importWin to window "Import Session Data"

                        -- Choose... Button
                        try
                            click button "Choose..." of importWin
                        on error
                            set allGroups to groups of importWin
                            repeat with g in allGroups
                                try
                                    click button "Choose..." of g
                                    exit repeat
                                end try
                            end repeat
                        end try
                    end tell
                end tell
                return "OK"
            on error errMsg
                return "ERROR:" & errMsg
            end try
        ''', timeout=10, label="ChooseBtn")
        if "ERROR" in out:
            logging.warning(f"  Import Schritt 5a (Choose): {out}")

        # Warte auf Track Data Fenster
        prog["update"](0.80, t("prog_choose_preset"))
        logging.info("  Import Schritt 5: Warte auf Track Data Fenster...")
        if _wait_for_pt_window("Track Data to Import", timeout=15):
            time.sleep(0.3)
            # Preset 1 via Ctrl+1 (CGEvent)
            _send_key_to_app(_VK_1, "Pro Tools", ctrl=True)
            time.sleep(0.3)
            _send_key(_VK_RETURN)  # Confirm
            logging.info("  Import Schritt 5 OK.")
        else:
            logging.warning("  Import Schritt 5: Track Data Fenster nicht gefunden.")
        time.sleep(1.0)

        # ── Schritt 6: Import bestätigen (CGEvent) ────────────────────
        prog["update"](0.87, t("prog_confirm_import"))
        logging.info("  Import Schritt 6: Import bestätigen...")
        time.sleep(0.3)
        _send_key(_VK_RETURN)
        logging.info("  Import Schritt 6 OK – Import gestartet.")

        # ── Schritt 7: Warte bis Import Session Data Fenster weg ──────
        prog["update"](0.90, t("prog_import_running"))
        logging.info("  Import Schritt 7: Warte auf Abschluss (max 120s)...")
        if _wait_for_pt_window("Import Session Data", timeout=120, gone=True):
            logging.info("  Import Schritt 7 OK – Import Session Data geschlossen.")
        else:
            logging.warning("  Import Schritt 7: Fenster nach 120s noch offen.")

        # ── Schritt 8: Warte bis Pro Tools fertig geladen hat ─────────
        prog["update"](0.95, t("prog_pt_loading"))
        logging.info("  Import Schritt 8: Warte bis Pro Tools bereit ist...")
        time.sleep(2.0)
        for i in range(180):
            ok, out = _run_applescript_safe('''
                try
                    tell application "System Events"
                        tell process "Pro Tools"
                            if exists window 1 then
                                set winTitle to name of window 1
                                if winTitle is not missing value then
                                    if winTitle contains "Importing" or winTitle contains "Processing" or winTitle contains "Task" or winTitle contains "Missing" or winTitle contains "Restoring" then
                                        return "BUSY"
                                    end if
                                end if
                                set progIndicators to (every UI element of window 1 whose role is "AXProgressIndicator")
                                if (count of progIndicators) > 0 then
                                    return "BUSY"
                                end if
                            end if
                        end tell
                    end tell
                    return "READY"
                on error
                    return "READY"
                end try
            ''', timeout=5, label="WaitReady")
            if not ok or out != "BUSY":
                break
            if i % 10 == 0 and i > 0:
                logging.info(f"  Import Schritt 8: Pro Tools noch beschäftigt ({i}s)...")
            time.sleep(1.0)
        logging.info("  Import Schritt 8 OK – Pro Tools bereit.")

        prog["update"](1.0, t("prog_import_done"))
        time.sleep(0.8)

        # ── Post-Import: Record-Tracks armen (NEXIS warm halten) ─────────
        # Engine reset + frisch verbinden damit wir die neue Session sehen.
        # Tracks armed lassen → NEXIS hält Dateien offen → kein Cold-Start
        # beim nächsten Record-Trigger, auch nach längerer Idle-Zeit.
        try:
            _close_engine()
            _eng_post = _get_engine()
            if _eng_post is not None:
                _cfg_post = _app_ref.settings if _app_ref is not None else load_settings()
                _arm_tracks = []
                for _key in ("tracks", "tracks_b"):
                    for _tr in _cfg_post.get(_key, []):
                        if _tr and _tr not in _arm_tracks:
                            _arm_tracks.append(_tr)
                _pt_post = _get_cached_track_names(_eng_post)
                _arm_want = sorted(tr for tr in _arm_tracks if _pt_post and tr in _pt_post)
                if _arm_want:
                    _ok_arm, _ = _ptsl_call(
                        _eng_post.set_track_record_enable_state, _arm_want, True,
                        label="PostImportArm", timeout=20.0
                    )
                    if _ok_arm:
                        logging.info(f"Post-Import: Tracks armed (NEXIS warm): {_arm_want}")
                    else:
                        logging.warning("Post-Import: Track arm fehlgeschlagen (unkritisch).")
                else:
                    logging.info("Post-Import: Keine passenden Record-Tracks zum Armen gefunden.")
        except Exception as _e_arm:
            logging.warning(f"Post-Import Arm fehlgeschlagen (unkritisch): {_e_arm}")

        logging.info("=== INTERPLAY IMPORT ENDE ===")
    except Exception as e:
        logging.error(f"Import Fehler: {e}", exc_info=True)
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        # Engine nach Import schließen: Nächster Aufruf verbindet sich frisch.
        # Tracks bleiben in PT armed – NEXIS-Dateien bleiben offen.
        _close_engine()
        with _import_lock:
            _import_running = False
        _set_busy(False)


def run_interplay_export(export_tracks, settings, workspace_steps=17):
    """
    Interplay Export mit vorgelagertem Consolidate + Loudness:
    1. Consolidate + Trim + Loudness (wie WAV/AAF)
    2. F13 → "Selected Tracks to Sequence in Production Management..."
    3. "Export Comment" → Enter
    4. "Export Options" → Select Workspace + Pfeil-Hoch × N → Enter
    5. "Processing Audio..." abwarten
    6. Bestaetigungsmeldung → Enter
    """
    _VK_UP = 126  # Arrow Up
    _VK_RETURN = 36

    prog = None
    _set_busy(True)
    try:
        logging.info("=== INTERPLAY EXPORT START ===")
        prog = _show_progress_win(t("prog_title_interplay"))
        prog["update"](0.03, t("prog_connect_pt"))

        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar – Abbruch.")
            return False

        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)
        # PT liefert nach PTSL-Reconnect manchmal den Backup-Pfad
        if os.path.basename(session_dir) == "Session File Backups":
            session_dir = os.path.dirname(session_dir)
            logging.debug(f"  session_dir korrigiert (Session File Backups entfernt): {session_dir}")

        # ── Versteckte Spuren einblenden ─────────────────────────────
        prog["update"](0.06, t("prog_prep_tracks"))
        logging.info(f"  Interplay: Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # ── Vorbereitung: Consolidate + Trim + Loudness ──────────────
        video_track = _detect_video_track(engine, settings)
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"

        # Video-Ende ermitteln
        prog["update"](0.10, t("prog_get_video_end"))
        logging.info(f"  Interplay: Spuren selektieren: {export_tracks}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Interplay: Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Interplay: Fehler Videospur '{video_track}': {e}")

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Interplay: Video-Ende nicht ermittelt – überspringe Consolidate.")
        else:
            # Pro Tools Selection State vorbereiten und absichern
            with _protools_selection_context(engine):
                # Überhänge trimmen (vor dem Consolidate, damit der Clip danach ein physischer Haupt-Clip bleibt)
                prog["update"](0.15, t("prog_trim_overhangs"))
                _trim_overhangs(engine, export_tracks, in_time, video_end)

                # Export-Spuren selektieren und Timeline setzen für Consolidate
                engine.select_tracks_by_name(export_tracks)
                time.sleep(0.25)
                logging.info(f"  Interplay: Timeline: {in_time} -> {video_end}")
                engine.set_timeline_selection(in_time=in_time, out_time=video_end)
                time.sleep(0.25)
                # Auswahl auf die Export-Spuren ausdehnen
                engine.extend_selection_to_target_tracks(export_tracks)
                time.sleep(0.3)

                # Consolidate
                prog["update"](0.25, t("prog_consolidate"))
                logging.info("  Interplay: Consolidate...")
                engine.consolidate_clip()
                _wait_for_consolidate_window_gone(timeout=60)
                _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
                logging.info("  Interplay: Consolidate OK")

                # Loudness-Korrektur
                if settings.get("loudness_enabled", True):
                    prog["update"](0.30, t("prog_loudness"))
                    loud_tracks = settings.get("loudness_tracks", ["ST"])
                    target_lufs = settings.get("target_lufs", -23.0)
                    max_tp = settings.get("max_truepeak", -3.0)
                    _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)

            logging.info("  Interplay: Vorbereitung (Consolidate+Loudness) abgeschlossen.")

        # ── DMGs auswerfen (verschieben den Workspace-Count) ─────────
        prog["update"](0.62, t("prog_eject_dmgs"))
        try:
            result = subprocess.run(
                ["hdiutil", "info", "-plist"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import plistlib
                info = plistlib.loads(result.stdout.encode())
                ejected = []
                for img in info.get("images", []):
                    img_path = img.get("image-path", "")
                    for entity in img.get("system-entities", []):
                        mount_point = entity.get("mount-point", "")
                        if mount_point:
                            try:
                                subprocess.run(
                                    ["hdiutil", "detach", mount_point, "-force"],
                                    capture_output=True, timeout=10
                                )
                                ejected.append(os.path.basename(img_path))
                            except Exception:
                                pass
                if ejected:
                    logging.info(f"  Interplay: {len(ejected)} DMG(s) ausgeworfen: {ejected}")
                    time.sleep(1.0)  # Pro Tools braucht kurz um Volumes zu aktualisieren
        except Exception as e:
            logging.warning(f"  Interplay: DMG-Auswurf fehlgeschlagen: {e}")

        # Export-Spuren selektieren (nach Consolidate/Loudness)
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # PT-Log Position merken – wird nach Export für Ergebnis-Erkennung genutzt
        pt_log_path  = _find_newest_pt_log()
        pt_log_start = os.path.getsize(pt_log_path) if pt_log_path and os.path.exists(pt_log_path) else 0
        logging.info(f"  Interplay: PT Log Snapshot: {pt_log_path} @ {pt_log_start:,} Bytes")

        prog["update"](0.67, t("prog_start_export"))
        logging.info("  Interplay: F13 senden...")
        _send_key(_VK_F13)

        # ── 1. Export Comment Fenster ─────────────────────────────────
        prog["update"](0.70, t("prog_wait_export_comment"))
        logging.info("  Interplay: Warte auf 'Export Comment'...")
        if not _wait_for_pt_window("Export Comment", timeout=10):
            logging.warning("  Interplay: 'Export Comment' nicht erschienen – Abbruch.")
            return False
        time.sleep(0.5)
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Export Comment bestaetigt.")

        # ── 2. Export Options Fenster ─────────────────────────────────
        prog["update"](0.73, t("prog_export_workspace"))
        logging.info("  Interplay: Warte auf 'Export Options'...")
        if not _wait_for_pt_window("Export Options", timeout=10):
            logging.warning("  Interplay: 'Export Options' nicht erschienen – Abbruch.")
            return False
        time.sleep(1.0)

        # "Select Workspace" Radio Button per AppleScript klicken
        radio_script = '''
            tell application "System Events"
                tell process "Pro Tools"
                    set frontmost to true
                    tell window 1
                        set allRadios to every radio button
                        repeat with r in allRadios
                            set rName to name of r
                            if rName contains "Select Workspace" or rName contains "Workspace" then
                                click r
                                delay 0.5
                                return "OK: " & rName
                            end if
                        end repeat
                        return "RADIO NOT FOUND"
                    end tell
                end tell
            end tell
        '''
        rc, out, err = _run_applescript(radio_script)
        if "OK" in out:
            logging.info(f"  Interplay: Radio Button geklickt: {out}")
        else:
            logging.warning(f"  Interplay: Radio Button nicht gefunden: {out} {err}")

        time.sleep(0.5)

        # Popup-Position ermitteln fuer Mausklick (sucht das Popup neben dem Radio-Button)
        popup_script = '''
            tell application "System Events"
                tell process "Pro Tools"
                    tell window 1
                        set radioX to -1
                        set radioY to -1
                        set allRadios to every radio button
                        repeat with r in allRadios
                            if (name of r) contains "Workspace" then
                                set pos to position of r
                                set radioX to item 1 of pos
                                set radioY to item 2 of pos
                                exit repeat
                            end if
                        end repeat
                        
                        if radioY is not -1 then
                            set allPopups to every pop up button
                            repeat with p in allPopups
                                try
                                    set py to item 2 of (position of p)
                                    -- Gleiche Y-Hoehe (Toleranz 15 Pixel)
                                    if py - radioY < 15 and radioY - py < 15 then
                                        set pos to position of p
                                        set sz to size of p
                                        set cx to (item 1 of pos) + (item 1 of sz) / 2
                                        set cy to (item 2 of pos) + (item 2 of sz) / 2
                                        return "POPUP_OPEN:" & cx & "," & cy
                                    end if
                                end try
                            end repeat
                            
                            -- Fallback: Geometrisch rechts vom Radio-Button
                            set fallback_cx to radioX + 150
                            set fallback_cy to radioY + 8
                            return "POPUP_OPEN:" & fallback_cx & "," & fallback_cy
                        end if
                        return "NO_POPUP"
                    end tell
                end tell
            end tell
        '''
        rc, out, err = _run_applescript(popup_script)
        popup_x, popup_y = None, None
        if "POPUP_OPEN" in out:
            logging.info(f"  Interplay: Popup-Koordinaten gefunden: {out}")
            try:
                coords = out.split(":")[1].strip()
                popup_x = float(coords.split(",")[0])
                popup_y = float(coords.split(",")[1])
            except Exception:
                pass
        else:
            logging.warning(f"  Interplay: Kein Popup oder Radiobutton gefunden: {out} {err}")

        time.sleep(0.5)

        # Mausklick auf das geoeffnete Popup-Menue
        import Quartz
        if popup_x is not None and popup_y is not None:
            logging.info(f"  Interplay: Mausklick auf Popup ({popup_x:.0f}, {popup_y:.0f})...")
            point = Quartz.CGPointMake(popup_x, popup_y)
            click_down = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft
            )
            click_up = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, click_down)
            time.sleep(0.05)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, click_up)
            time.sleep(0.5)
        else:
            logging.warning("  Interplay: Popup-Position unbekannt – ueberspringe Mausklick.")

        # Workspace per Pfeiltasten auswaehlen
        logging.info(f"  Interplay: {workspace_steps}x Pfeil-Hoch...")
        pid = _pt_pid()
        if pid:
            src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
            for _ in range(workspace_steps):
                ev_dn = Quartz.CGEventCreateKeyboardEvent(src, _VK_UP, True)
                ev_up = Quartz.CGEventCreateKeyboardEvent(src, _VK_UP, False)
                Quartz.CGEventPostToPid(pid, ev_dn)
                Quartz.CGEventPostToPid(pid, ev_up)
                time.sleep(0.05)
        time.sleep(0.3)
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Workspace ausgewaehlt.")

        time.sleep(0.5)
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Export Options bestaetigt.")

        # ── 3. Export-Ergebnis aus PT Log lesen, dann Bestätigung sofort senden ──
        prog["update"](0.80, t("prog_export_running"))
        logging.info("  Interplay: Warte auf Export-Ergebnis im PT Log...")
        export_ok, log_msg = _poll_pt_log_for_export(pt_log_path, pt_log_start, timeout=90)

        if export_ok is True:
            logging.info(f"  Interplay: Export ERFOLGREICH – {log_msg}")
            prog["update"](0.93, t("prog_export_success_confirm"))
        elif export_ok is False:
            logging.error(f"  Interplay: Export FEHLGESCHLAGEN – {log_msg}")
            prog["update"](0.93, t("prog_export_failed"))
        else:
            logging.warning(f"  Interplay: Export-Ergebnis unklar – {log_msg}")
            prog["update"](0.93, t("prog_export_done_ellipsis"))

        pt_dismiss_pos = os.path.getsize(pt_log_path) if pt_log_path and os.path.exists(pt_log_path) else 0
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Bestaetigungsfenster bestaetigt.")
        _wait_for_pt_modal_dismissed(pt_log_path, pt_dismiss_pos)

        # ── 5. Sequence umbenennen – nur bei Erfolg oder unklarem Ergebnis ──
        if settings.get("interplay_rename_enabled", False):
            if export_ok is False:
                logging.warning("  Interplay: Umbenennung uebersprungen – Export war fehlerhaft.")
            else:
                prog["update"](0.97, t("prog_rename_seq"))
                logging.info("  Interplay: Starte Sequence-Umbenennung...")
                _run_applescript('tell application "interplayAccess" to activate')
                time.sleep(0.3)
                _rename_sequence_in_interplay(settings)

        prog["update"](1.0, t("prog_export_done"))
        time.sleep(0.8)
        logging.info("=== INTERPLAY EXPORT ENDE ===")
        return export_ok is not False   # False nur bei erkanntem Fehler

    except Exception as e:
        logging.error(f"Interplay Export Fehler: {e}", exc_info=True)
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
        return False
    finally:
        if prog: prog["close"]()
        _set_busy(False)


def _rename_sequence_in_interplay(settings):
    """Benennt die Sequenz in Interplay Access um.
    Vollständig CGEvent-basiert (kein AppleScript, kein System Events).
    Blockiert nicht auf PT's Accessibility-Layer – kann sofort nach Export starten.
    Typing via CGEventKeyboardSetUnicodeString umgeht Cmd+V-Versagen bei Java-Apps."""
    import Quartz as _Q
    trim_start = max(0, int(settings.get("interplay_rename_trim_start", 0)))
    trim_end   = max(0, int(settings.get("interplay_rename_trim_end", 0)))
    prefix     = settings.get("interplay_rename_prefix", "")
    suffix     = settings.get("interplay_rename_suffix", "")

    if trim_start == 0 and trim_end == 0 and not prefix and not suffix:
        logging.info("  Rename: Keine Änderungen konfiguriert – überspringe.")
        return

    pid = _app_pid("interplayAccess")
    if not pid:
        logging.warning("  Rename: Interplay Access nicht gefunden.")
        return

    src = _Q.CGEventSourceCreate(_Q.kCGEventSourceStateHIDSystemState)

    def _key(vk, cmd=False):
        ev_dn = _Q.CGEventCreateKeyboardEvent(src, vk, True)
        ev_up = _Q.CGEventCreateKeyboardEvent(src, vk, False)
        if cmd:
            _Q.CGEventSetFlags(ev_dn, _Q.kCGEventFlagMaskCommand)
            _Q.CGEventSetFlags(ev_up, _Q.kCGEventFlagMaskCommand)
        _Q.CGEventPostToPid(pid, ev_dn)
        _Q.CGEventPostToPid(pid, ev_up)

    # ── Schritt 1: F2 → Rename-Feld öffnen, Namen per Clipboard lesen ──────
    _key(_VK_F2)
    time.sleep(0.7)
    # Cmd+A + Cmd+C in einem einzigen osascript-Aufruf (halbe Subprocess-Overhead)
    _run_applescript(
        'tell application "System Events" to tell process "interplayAccess"\n'
        '  keystroke "a" using command down\n'
        '  delay 0.15\n'
        '  keystroke "c" using command down\n'
        'end tell'
    )
    time.sleep(0.3)

    try:
        current_name = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
    except Exception as e:
        logging.warning(f"  Rename: pbpaste fehlgeschlagen: {e}")
        _key(_VK_ESC)
        return

    if not current_name:
        logging.warning("  Rename: Clipboard leer – Rename-Feld nicht erreichbar.")
        _key(_VK_ESC)
        return

    logging.info(f"  Rename: Aktueller Name: '{current_name}'")

    # ── Schritt 2: neuen Namen berechnen ──────────────────────────────────
    end_idx = max(trim_start, len(current_name) - trim_end if trim_end > 0 else len(current_name))
    new_name = prefix + current_name[trim_start:end_idx] + suffix

    if not new_name:
        logging.warning("  Rename: Berechneter Name ist leer – überspringe.")
        _key(_VK_ESC)
        return

    if new_name == current_name:
        logging.info("  Rename: Name unverändert – überspringe.")
        _key(_VK_ESC)
        return

    logging.info(f"  Rename: Neuer Name: '{new_name}'")

    # ── Schritt 3: Cmd+A (alten Text löschen), dann Namen Zeichen für Zeichen tippen ──
    _run_applescript(
        'tell application "System Events" to tell process "interplayAccess" to keystroke "a" using command down'
    )
    time.sleep(0.1)

    for ch in new_name:
        ev_dn = _Q.CGEventCreateKeyboardEvent(src, 0, True)
        ev_up = _Q.CGEventCreateKeyboardEvent(src, 0, False)
        _Q.CGEventKeyboardSetUnicodeString(ev_dn, 1, ch)
        _Q.CGEventKeyboardSetUnicodeString(ev_up, 1, ch)
        _Q.CGEventPostToPid(pid, ev_dn)
        _Q.CGEventPostToPid(pid, ev_up)
        time.sleep(0.008)   # 8 ms zwischen Tastenanschlägen

    time.sleep(0.05)
    _key(_VK_RETURN)
    logging.info("  Rename: Umbenennung abgeschlossen.")


def run_export(export_tracks, video_track=None, settings=None):
    """
    Export-Workflow (Keyboard-basiert, ~5s):
      1. DoE und AD einblenden
      2. Video-Ende von V1 ermitteln
      3. Timeline setzen + Shift+Ö zum Ausdehnen auf alle Spuren
      4. Consolidate (alle Spuren auf einmal)
      5. Pre/Post-Material loeschen
      6. Export-Spuren am Spurenkopf markieren
    """
    global _export_running
    with _export_lock:
        if _export_running:
            logging.warning("Export laeuft bereits – ignoriert.")
            return
        _export_running = True
    _set_busy(True)
    export_start_time = time.time()

    if settings is None:
        settings = load_settings()

    try:
        logging.info("=== EXPORT START ===")
        logging.info(f"  Export-Spuren: {export_tracks}")

        # Pro Tools in den Vordergrund (fuer Keyboard-Befehle)
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Pro Tools" to activate'],
                timeout=3, capture_output=True
            )
            time.sleep(0.3)
        except Exception:
            pass

        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfügbar – Abbruch.")
            return

        # ── 1. Versteckte Spuren einblenden ──────────────────────────
        logging.info(f"Schritt 1: Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # ── 2. Video-Ende ermitteln ──────────────────────────────────
        # Auto-detect oder Fallback auf Parameter/Settings
        video_track = video_track or _detect_video_track(engine, settings)
        logging.info(f"Schritt 2: Video-Ende ermitteln (Spur: '{video_track}')...")
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")
            return

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"

        # Pro Tools Selection State vorbereiten und absichern
        with _protools_selection_context(engine):
            # ── 3. Ueberhaenge pro Spur loeschen (vor dem Consolidate) ────
            logging.info("Schritt 3: Überhänge trimmen…")
            _trim_overhangs(engine, export_tracks, in_time, video_end)

            # ── 4. Timeline + Selection ausdehnen für Consolidate ────────
            logging.info(f"Schritt 4: Timeline {in_time} -> {video_end}")
            engine.select_tracks_by_name(export_tracks)
            time.sleep(0.25)
            engine.set_timeline_selection(in_time=in_time, out_time=video_end)
            time.sleep(0.25)
            engine.extend_selection_to_target_tracks(export_tracks)
            time.sleep(0.3)

            # ── 5. Consolidate (alle Spuren auf einmal) ──────────────────
            logging.info("Schritt 5: Consolidate...")
            engine.consolidate_clip()
            
            session_path = engine.session_path()
            session_dir = os.path.dirname(session_path)
            if os.path.basename(session_dir) == "Session File Backups":
                session_dir = os.path.dirname(session_dir)

            _wait_for_consolidate_window_gone(timeout=60)
            _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
            logging.info("  Consolidate OK")

            # ── 7. Loudness-Korrektur (EBU R128) ─────────────────────
            if settings.get("loudness_enabled", True):
                loud_tracks = settings.get("loudness_tracks", ["ST"])
                logging.info(f"Schritt 7: Loudness-Korrektur fuer {loud_tracks}...")
                try:
                    target_lufs = settings.get("target_lufs", -23.0)
                    max_tp = settings.get("max_truepeak", -3.0)
                    _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)
                except Exception as e:
                    logging.error(f"  Normalisierung fehlgeschlagen: {e}")
            else:
                logging.info("Schritt 7: ST-Spur normalisieren uebersprungen (deaktiviert).")

        # -- 8a. WAV Export (optional) --
        if settings.get("wav_export_enabled", False):
            logging.info("Schritt 8a: WAV Export...")
            try:
                session_path = engine.session_path()
                s_dir = os.path.dirname(session_path)
                _do_wav_export(export_tracks, s_dir, min_mtime=export_start_time - 10.0)
            except Exception as e:
                logging.error(f"  WAV Export Fehler: {e}")
        else:
            logging.info("Schritt 8a: WAV Export uebersprungen (deaktiviert).")

        # -- 8b. AAF Export (optional) --
        if settings.get("aaf_export_enabled", False):
            logging.info("Schritt 8b: AAF Export...")
            try:
                session_path = engine.session_path()
                s_dir = os.path.dirname(session_path)
                s_name = os.path.splitext(os.path.basename(session_path))[0]
                _do_aaf_export(s_name, s_dir)
            except Exception as e:
                logging.error(f"  AAF Export Fehler: {e}")
        else:
            logging.info("Schritt 8b: AAF Export uebersprungen (deaktiviert).")

        # -- 8c. Interplay Export (optional) --
        if settings.get("interplay_enabled", False):
            logging.info("Schritt 8c: Interplay Export...")
            ws_steps = settings.get("interplay_workspace_steps", 17)
            try:
                run_interplay_export(export_tracks, settings, workspace_steps=ws_steps)
            except Exception as e:
                logging.error(f"  Interplay Export Fehler: {e}")
        else:
            logging.info("Schritt 8c: Interplay Export uebersprungen (deaktiviert).")

        # -- 9. Abschluss: Spuren markieren, eingeblendet lassen --
        logging.info("Schritt 9: Export-Spuren markieren...")
        engine.set_timeline_selection(in_time=in_time, out_time=video_end)
        engine.select_tracks_by_name(export_tracks)

        logging.info("=== EXPORT ABGESCHLOSSEN ===")
        logging.info(f"  {len(export_tracks)} Spuren konsolidiert und bereinigt.")

    except Exception as e:
        logging.error(f"Export Fehler: {e}", exc_info=True)
    finally:
        with _export_lock:
            _export_running = False
        _set_busy(False)


from contextlib import contextmanager

@contextmanager
def _protools_selection_context(engine):
    """
    Sichert die originalen Edit-Mode-Optionen und das Edit-Tool,
    setzt sie für die Export-Operationen auf Selector-Tool und Links aktiv,
    und stellt sie am Ende wieder her.
    """
    import ptsl.PTSL_pb2 as pt
    orig_tool = None
    orig_options = None
    
    # 1. Edit-Tool sichern und auf Selector setzen
    try:
        tool_res = engine.get_edit_tool()
        if tool_res is not None:
            if hasattr(tool_res, "current_setting"):
                orig_tool = tool_res.current_setting
            elif isinstance(tool_res, dict) and "current_setting" in tool_res:
                orig_tool = tool_res["current_setting"]
        logging.info("  PT: Selector-Tool aktivieren...")
        engine.set_edit_tool(pt.ETool_Selector)
    except Exception as e:
        logging.warning(f"  Selector-Tool konnte nicht gesetzt/gesichert werden: {e}")
        
    # 2. Edit-Mode-Optionen sichern und Links aktivieren
    try:
        _, opt_res = _ptsl_call(
            lambda: engine.client.run_command(pt.GetEditModeOptions, {}),
            label="GetEditModeOptions", timeout=5.0
        )
        if opt_res and "edit_mode_options" in opt_res:
            orig_options = dict(opt_res["edit_mode_options"])

        new_opts = dict(orig_options) if orig_options else {}
        new_opts["link_timeline_and_edit_selection"] = True
        new_opts["link_track_and_edit_selection"] = True

        logging.info("  PT: Link Timeline & Link Track aktivieren...")
        _ptsl_call(
            lambda: engine.client.run_command(pt.SetEditModeOptions, {"edit_mode_options": new_opts}),
            label="SetEditModeOptions", timeout=5.0
        )
    except Exception as e:
        logging.warning(f"  Edit-Mode-Optionen konnten nicht gesetzt/gesichert werden: {e}")

    try:
        yield
    finally:
        # 3. Originale Einstellungen wiederherstellen
        logging.info("  PT: Originale Edit-Optionen und Tools wiederherstellen...")
        try:
            if orig_options:
                _ptsl_call(
                    lambda: engine.client.run_command(pt.SetEditModeOptions, {"edit_mode_options": orig_options}),
                    label="RestoreEditModeOptions", timeout=5.0
                )
        except Exception as e:
            logging.warning(f"  Edit-Mode-Optionen Wiederherstellung fehlgeschlagen: {e}")
        try:
            if orig_tool is not None:
                engine.set_edit_tool(orig_tool)
        except Exception as e:
            logging.warning(f"  Edit-Tool Wiederherstellung fehlgeschlagen: {e}")


# -----------------------------------------------------------------------------
# WAV Export - Konsolidierte Dateien in Export-Ordner kopieren
# -----------------------------------------------------------------------------
def _trim_overhangs(engine, export_tracks, in_time, video_end):
    """Löscht Überhänge (Pre/Post-Material) pro Spur außerhalb der Export-Range."""
    logging.info(f"  Ueberhaenge loeschen ({len(export_tracks)} Spuren)...")
    for track_name in export_tracks:
        # Pre-Material (vor Start-TC) löschen
        engine.select_all_clips_on_track(track_name)
        time.sleep(0.1)
        engine.set_timeline_selection(
            in_time="00:00:00:00.00",
            out_time=in_time
        )
        time.sleep(0.1)
        try:
            engine.clear()
        except Exception:
            pass

        # Post-Material (nach Video-Ende) löschen
        engine.select_all_clips_on_track(track_name)
        time.sleep(0.1)
        engine.set_timeline_selection(
            in_time=video_end,
            out_time="23:59:59:24.00"
        )
        time.sleep(0.1)
        try:
            engine.clear()
        except Exception:
            pass

    logging.info("  Ueberhaenge geloescht")


# -----------------------------------------------------------------------------
def _do_wav_export(export_tracks, session_dir, min_mtime=None):
    """Kopiert die konsolidierten WAV-Dateien in den Export-Ordner.
    Unterstützt sowohl Stereo-Interleaved als auch Split-Mono (.L.wav/.R.wav).
    Bei Split-Mono werden L+R zu einer Stereo-Interleaved-Datei zusammengeführt.
    """
    audio_dir = os.path.join(session_dir, "Audio Files")
    export_dir = os.path.join(session_dir, "export")
    os.makedirs(export_dir, exist_ok=True)

    if not os.path.isdir(audio_dir):
        logging.error(f"  Audio-Ordner nicht gefunden: {audio_dir}")
        return

    def _matches_track(base_name, track_name):
        """Prüft ob ein Dateiname (ohne Extension) zu einem Track gehört."""
        return (base_name == track_name
                or base_name.startswith(track_name + "_")
                or base_name.startswith(track_name + ".")
                or base_name.startswith(track_name + "-"))

    copied = 0
    for track_name in export_tracks:
        # Alle WAV-Dateien für diesen Track sammeln – getrennt nach Typ
        interleaved = []   # Normale Stereo-Interleaved oder Mono WAVs
        split_L = []       # Split-Mono Left-Kanal (.L.wav)
        split_R = []       # Split-Mono Right-Kanal (.R.wav)

        for f in os.listdir(audio_dir):
            if not f.lower().endswith(".wav"):
                continue
            full = os.path.join(audio_dir, f)
            try:
                mtime = os.path.getmtime(full)
            except Exception:
                continue
            if min_mtime is not None and mtime < min_mtime:
                continue

            # Split-Mono erkennen: Dateiname endet auf .L.wav oder .R.wav
            if f.lower().endswith(".l.wav"):
                # Basis ohne .L.wav
                stem = f[:-len(".L.wav")]
                if _matches_track(stem, track_name):
                    split_L.append((os.path.getmtime(full), full, f, stem))
                continue
            elif f.lower().endswith(".r.wav"):
                stem = f[:-len(".R.wav")]
                if _matches_track(stem, track_name):
                    split_R.append((os.path.getmtime(full), full, f, stem))
                continue

            # Normale (interleaved) Datei
            base = os.path.splitext(f)[0]
            if _matches_track(base, track_name):
                interleaved.append((os.path.getmtime(full), full, f))

        dst = os.path.join(export_dir, f"{track_name}.wav")

        # Strategie 1: Interleaved-Datei vorhanden → direkt kopieren (bevorzugt)
        if interleaved:
            interleaved.sort(reverse=True)
            src = interleaved[0][1]
            shutil.copy2(src, dst)
            logging.info(f"  WAV: {interleaved[0][2]} -> {track_name}.wav")
            copied += 1

        # Strategie 2: Split-Mono L+R → zu Stereo zusammenführen
        elif split_L and split_R:
            split_L.sort(reverse=True)
            split_R.sort(reverse=True)
            l_file = split_L[0][1]
            r_file = split_R[0][1]
            try:
                import soundfile as sf
                import numpy as np
                data_l, rate_l = sf.read(l_file)
                data_r, rate_r = sf.read(r_file)
                # Auf gleiche Länge bringen (falls nötig)
                min_len = min(len(data_l), len(data_r))
                data_l = data_l[:min_len]
                data_r = data_r[:min_len]
                # Mono-Arrays zu Stereo zusammenführen
                if data_l.ndim == 1 and data_r.ndim == 1:
                    stereo = np.column_stack((data_l, data_r))
                else:
                    stereo = np.column_stack((data_l.flatten()[:min_len],
                                              data_r.flatten()[:min_len]))
                sf.write(dst, stereo, rate_l)
                logging.info(f"  WAV: Zusammenfuehren von {split_L[0][2]} und {split_R[0][2]} zu Stereo -> {track_name}.wav")
                copied += 1
            except Exception as e:
                logging.error(f"  WAV: Split-Mono Zusammenfuehrung fehlgeschlagen fuer '{track_name}': {e}")
        else:
            logging.warning(f"  WAV: Keine Datei fuer Spur '{track_name}' in Audio Files")

    logging.info(f"  WAV Export: {copied}/{len(export_tracks)} Dateien kopiert -> {export_dir}")


def run_wav_export_standalone(export_tracks, settings):
    """Standalone WAV Export – mit vorgelagertem Consolidate."""
    prog = None
    try:
        logging.info("=== WAV EXPORT (Standalone) START ===")
        _set_busy(True)
        export_start_time = time.time()
        prog = _show_progress_win(t("prog_title_wav"))
        prog["update"](0.03, t("prog_connect_pt"))
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar.")
            return
        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)

        # Spuren einblenden
        prog["update"](0.06, t("prog_prep_tracks"))
        logging.info(f"  Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # Spuren selektieren
        prog["update"](0.08, t("prog_prep_tracks"))
        logging.info(f"  Spuren selektieren: {export_tracks}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # Video-Ende ermitteln (wie im Haupt-Export)
        prog["update"](0.13, t("prog_get_video_end"))
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"
        video_track = _detect_video_track(engine, settings)
        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        # Pro Tools Selection State vorbereiten und absichern
        with _protools_selection_context(engine):
            # Überhänge pro Spur löschen (Pre/Post-Material abschneiden vor Consolidate)
            prog["update"](0.20, t("prog_trim_overhangs"))
            _trim_overhangs(engine, export_tracks, in_time, video_end)

            # Export-Spuren selektieren und Range für Consolidate setzen
            engine.select_tracks_by_name(export_tracks)
            time.sleep(0.25)
            logging.info(f"  Timeline: {in_time} -> {video_end}")
            engine.set_timeline_selection(in_time=in_time, out_time=video_end)
            time.sleep(0.25)
            # Auswahl auf die Export-Spuren ausdehnen
            engine.extend_selection_to_target_tracks(export_tracks)
            time.sleep(0.3)

            # Consolidate
            prog["update"](0.32, t("prog_consolidate"))
            logging.info("  Consolidate...")
            engine.consolidate_clip()
            if os.path.basename(session_dir) == "Session File Backups":
                session_dir = os.path.dirname(session_dir)
            _wait_for_consolidate_window_gone(timeout=60)
            _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
            logging.info("  Consolidate OK")

            # Loudness-Korrektur
            if settings.get("loudness_enabled", True):
                prog["update"](0.40, t("prog_loudness"))
                loud_tracks = settings.get("loudness_tracks", ["ST"])
                target_lufs = settings.get("target_lufs", -23.0)
                max_tp = settings.get("max_truepeak", -3.0)
                _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)

        prog["update"](0.88, t("prog_copy_wav"))
        _do_wav_export(export_tracks, session_dir, min_mtime=export_start_time - 10.0)
        prog["update"](1.0, t("prog_wav_done"))
        time.sleep(0.8)
        logging.info("=== WAV EXPORT (Standalone) ENDE ===")
    except Exception as e:
        logging.error(f"WAV Export Fehler: {e}", exc_info=True)
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        _set_busy(False)


# -----------------------------------------------------------------------------
# AAF Export - Pro Tools UI-Automation
# -----------------------------------------------------------------------------
_AAF_EXPORT_SCRIPT_TEMPLATE = '''
set exportPath to "{export_path}"

tell application "Pro Tools" to activate
delay 0.5

tell application "System Events"
    tell process "Pro Tools"
        set frontmost to true
        delay 0.3

        -- Datei > Export > Selected Tracks as New AAF/OMF...
        try
            click menu item "Selected Tracks as New AAF/OMF..." of menu of menu item "Export" of menu "File" of menu bar 1
        on error
            try
                click menu item "Selected Tracks as New AAF/OMF…" of menu of menu item "Export" of menu "File" of menu bar 1
            on error
                return "ERROR:Menu item not found"
            end try
        end try

        -- 1. Warte auf "Export to OMF/AAF" Fenster
        delay 1.5
        set exportWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Export to OMF" or name of w contains "Export to AAF" or name of w contains "OMF/AAF" then
                        set exportWinFound to true
                        exit repeat
                    end if
                end repeat
                if exportWinFound then exit repeat
            end try
            delay 0.5
        end repeat

        if not exportWinFound then
            return "ERROR:Export to OMF/AAF dialog not found"
        end if
        delay 0.5

        -- 2. Audio Media Options: Format-Dropdown gezielt auf "Embedded" setzen
        -- Group 3 = "Audio Media Options", Popup 3 darin = "Format"
        try
            set audioGroup to group 3 of window 1
            set formatPopup to pop up button 3 of audioGroup
            set currentFormat to value of formatPopup
            
            if currentFormat is not "Embedded" then
                -- Format-Dropdown klicken und "Embedded" auswaehlen
                click formatPopup
                delay 0.3
                -- 4x Pfeiltaste nach unten = "Embedded"
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 36 -- Enter
                delay 0.3
            end if
        on error errMsg
            -- Fallback: versuche ueber die Gruppe mit dem Titel
            try
                set allGroups to every group of window 1
                repeat with g in allGroups
                    try
                        if title of g contains "Audio" then
                            set formatPopup to pop up button 3 of g
                            click formatPopup
                            delay 0.3
                            key code 125
                            delay 0.1
                            key code 125
                            delay 0.1
                            key code 125
                            delay 0.1
                            key code 125
                            delay 0.1
                            key code 36
                            exit repeat
                        end if
                    end try
                end repeat
            end try
        end try


        delay 0.3

        -- 3. Dialog mit Enter bestaetigen
        key code 36
        delay 1.5

        -- 4. Warte auf "Publishing Options" Fenster
        set pubWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Publishing" then
                        set pubWinFound to true
                        exit repeat
                    end if
                end repeat
                if pubWinFound then exit repeat
            end try
            delay 0.5
        end repeat

        if not pubWinFound then
            return "ERROR:Publishing Options dialog not found"
        end if
        delay 0.5

        -- 5. Pfeiltaste rechts + "_Audio" tippen
        key code 124
        delay 0.1
        keystroke "_Audio"
        delay 0.3

        -- 6. Enter zum Bestaetigen
        key code 36
        delay 1.5

        -- 7. Warte auf "Save" Dialog
        set saveWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Save" or name of w contains "Speichern" then
                        set saveWinFound to true
                        exit repeat
                    end if
                end repeat
                if saveWinFound then exit repeat
            end try
            try
                if exists sheet 1 of window 1 then
                    set saveWinFound to true
                    exit repeat
                end if
            end try
            delay 0.5
        end repeat

        if not saveWinFound then
            return "ERROR:Save dialog not found"
        end if
        delay 0.5

        -- 8. Zum Export-Ordner navigieren via Cmd+Shift+G
        keystroke "g" using {{command down, shift down}}
        delay 0.8

        keystroke "a" using command down
        delay 0.1
        keystroke exportPath
        delay 0.3

        key code 36
        delay 0.8

        -- 9. Save mit Enter
        key code 36
        delay 1.5

        -- 9b. Replace-Dialog abfangen (falls Datei bereits existiert)
        -- Der Dialog hat einen LEEREN Fensternamen, daher direkt Yes/Ja Button suchen
        try
            if exists button "Yes" of window 1 then
                click button "Yes" of window 1
                delay 0.5
            end if
        end try
        try
            if exists button "Ja" of window 1 then
                click button "Ja" of window 1
                delay 0.5
            end if
        end try

        -- 10. Warte bis Export abgeschlossen
        repeat 180 times
            set stillBusy to false
            
            -- Zuerst: Replace/Yes/Ja Dialoge sofort bestaetigen
            try
                if exists button "Yes" of window 1 then
                    click button "Yes" of window 1
                    delay 0.5
                end if
            end try
            try
                if exists button "Ja" of window 1 then
                    click button "Ja" of window 1
                    delay 0.5
                end if
            end try
            
            -- Dann: Pruefen ob noch Export-Fenster offen sind
            try
                set allWins to every window
                repeat with w in allWins
                    set wName to name of w
                    if wName contains "Export" or wName contains "Save" or wName contains "Publishing" or wName contains "Bouncing" or wName contains "Writing" then
                        set stillBusy to true
                        exit repeat
                    end if
                    -- Fenster ohne Namen mit Yes/No = Replace-Dialog
                    if wName is "" then
                        try
                            if exists button "Yes" of w then
                                click button "Yes" of w
                                set stillBusy to true
                                exit repeat
                            end if
                        end try
                        try
                            if exists button "Ja" of w then
                                click button "Ja" of w
                                set stillBusy to true
                                exit repeat
                            end if
                        end try
                    end if
                end repeat
            end try
            if not stillBusy then exit repeat
            delay 1.0
        end repeat

        delay 1.0

    end tell
end tell
return "OK"
'''


def _do_aaf_export(session_name, session_dir):
    """Exportiert die selektierten Spuren als AAF mit Embedded Audio."""
    export_dir = os.path.join(session_dir, "export")
    os.makedirs(export_dir, exist_ok=True)

    script = _AAF_EXPORT_SCRIPT_TEMPLATE.format(
        export_path=export_dir.replace('"', '\\"')
    )

    logging.info(f"  AAF: Exportiere nach {export_dir}")
    rc, out, err = _run_applescript(script)
    if "ERROR" in out:
        logging.error(f"  AAF AppleScript Fehler: {out}")
    else:
        logging.info(f"  AAF Export abgeschlossen: {out}")




def run_aaf_export_standalone(export_tracks, settings):
    """Standalone AAF Export – mit vorgelagertem Consolidate."""
    prog = None
    try:
        logging.info("=== AAF EXPORT (Standalone) START ===")
        _set_busy(True)
        prog = _show_progress_win(t("prog_title_aaf"))
        prog["update"](0.03, t("prog_connect_pt"))
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar.")
            return
        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)
        session_name = os.path.splitext(os.path.basename(session_path))[0]

        # Spuren einblenden
        prog["update"](0.06, t("prog_prep_tracks"))
        logging.info(f"  Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # Spuren selektieren
        prog["update"](0.08, t("prog_prep_tracks"))
        logging.info(f"  Spuren selektieren: {export_tracks}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # Video-Ende ermitteln (wie im Haupt-Export)
        prog["update"](0.13, t("prog_get_video_end"))
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"
        video_track = _detect_video_track(engine, settings)
        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        # Pro Tools Selection State vorbereiten und absichern
        with _protools_selection_context(engine):
            # Überhänge pro Spur löschen (Pre/Post-Material abschneiden vor Consolidate)
            prog["update"](0.20, t("prog_trim_overhangs"))
            _trim_overhangs(engine, export_tracks, in_time, video_end)

            # Export-Spuren selektieren und Range für Consolidate setzen
            engine.select_tracks_by_name(export_tracks)
            time.sleep(0.25)
            logging.info(f"  Timeline: {in_time} -> {video_end}")
            engine.set_timeline_selection(in_time=in_time, out_time=video_end)
            time.sleep(0.25)
            # Auswahl auf die Export-Spuren ausdehnen
            engine.extend_selection_to_target_tracks(export_tracks)
            time.sleep(0.3)

            # Consolidate
            prog["update"](0.32, t("prog_consolidate"))
            logging.info("  Consolidate...")
            engine.consolidate_clip()
            if os.path.basename(session_dir) == "Session File Backups":
                session_dir = os.path.dirname(session_dir)
            _wait_for_consolidate_window_gone(timeout=60)
            _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
            logging.info("  Consolidate OK")

            # Loudness-Korrektur
            if settings.get("loudness_enabled", True):
                prog["update"](0.40, t("prog_loudness"))
                loud_tracks = settings.get("loudness_tracks", ["ST"])
                target_lufs = settings.get("target_lufs", -23.0)
                max_tp = settings.get("max_truepeak", -3.0)
                _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)

        # Spuren erneut selektieren für AAF-Export
        prog["update"](0.85, t("prog_aaf_export"))
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.2)
        engine.set_timeline_selection(in_time=in_time, out_time=video_end)
        time.sleep(0.2)

        _do_aaf_export(session_name, session_dir)
        prog["update"](1.0, t("prog_aaf_done"))
        time.sleep(0.8)
        logging.info("=== AAF EXPORT (Standalone) ENDE ===")
    except Exception as e:
        logging.error(f"AAF Export Fehler: {e}", exc_info=True)
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        _set_busy(False)


def normalize_track(engine, session_dir, track_name="ST", target_lufs=-23.0, max_truepeak=-3.0, progress_cb=None):
    """
    Normalisiert die konsolidierte Audiodatei einer Spur nach EBU R128.
    1. Findet die neueste '<track_name>*' .wav im Audio Files Ordner
    2. Misst integrierte Lautheit (LUFS) und True Peak
    3. Wendet Gain-Korrektur an (mit True-Peak-Limiter)
    4. Ueberschreibt die Datei
    5. Aktualisiert Pro Tools (refresh)
    """
    def _prog(frac, msg):
        if progress_cb:
            try: progress_cb(frac, msg)
            except Exception: pass

    logging.info(f"  Loudness-Korrektur fuer Spur '{track_name}'...")
    _prog(0.05, t("prog_track_search").format(track_name))
    try:
        import soundfile as sf
        import pyloudnorm as pyln
        import numpy as np
    except ImportError as e:
        logging.error(f"Normalisierung: fehlende Bibliothek: {e}")
        logging.error("  pip3 install pyloudnorm soundfile")
        return

    audio_dir = os.path.join(session_dir, "Audio Files")
    if not os.path.isdir(audio_dir):
        logging.error(f"  Audio-Ordner nicht gefunden: {audio_dir}")
        return

    # Neueste konsolidierte Datei fuer diese Spur finden
    st_files = []
    for f in os.listdir(audio_dir):
        base = os.path.splitext(f)[0]
        if (base == track_name or base.startswith(track_name + "_") or base.startswith(track_name + ".") or base.startswith(track_name + "-")) and f.lower().endswith(".wav"):
            full = os.path.join(audio_dir, f)
            mtime = os.path.getmtime(full)
            size = os.path.getsize(full)
            st_files.append((mtime, size, full, f))

    if not st_files:
        logging.warning(f"  Keine {track_name}*.wav Dateien gefunden – Normalisierung uebersprungen.")
        return

    st_files.sort(reverse=True)  # Neueste zuerst (nach mtime)
    target_file = st_files[0][2]
    target_name = st_files[0][3]
    logging.info(f"  Datei: {target_name} ({st_files[0][1] / 1024 / 1024:.1f} MB)")

    # Audio lesen
    _prog(0.15, t("prog_track_read").format(target_name))
    data, rate = sf.read(target_file)
    logging.info(f"  Sample-Rate: {rate} Hz, Dauer: {len(data)/rate:.1f}s, Kanaele: {data.ndim}")

    # Lautheit messen
    _prog(0.35, t("prog_track_measure").format(target_name))
    meter = pyln.Meter(rate)
    current_lufs = meter.integrated_loudness(data)
    logging.info(f"  Aktuelle Lautheit: {current_lufs:.1f} LUFS (Ziel: {target_lufs} LUFS)")

    if current_lufs == float('-inf'):
        logging.warning("  Stille erkannt – Normalisierung uebersprungen.")
        return

    # Gain berechnen
    gain_db = target_lufs - current_lufs
    gain_linear = 10 ** (gain_db / 20.0)
    logging.info(f"  Gain-Korrektur: {gain_db:+.1f} dB")

    # Gain anwenden
    _prog(0.55, t("prog_track_gain").format(target_name, gain_db))
    normalized = data * gain_linear

    # True Peak pruefen und limitieren
    peak_linear = np.max(np.abs(normalized))
    peak_db = 20 * np.log10(peak_linear) if peak_linear > 0 else -120.0
    logging.info(f"  True Peak nach Gain: {peak_db:.1f} dB (Max: {max_truepeak} dB)")

    if peak_db > max_truepeak:
        # Limitieren: Gain so reduzieren dass True Peak eingehalten wird
        reduction_db = peak_db - max_truepeak
        reduction_linear = 10 ** (-reduction_db / 20.0)
        normalized *= reduction_linear
        final_lufs = current_lufs + gain_db - reduction_db
        logging.info(f"  True Peak Limiter: -{reduction_db:.1f} dB angewendet")
        logging.info(f"  Endgueltige Lautheit: {final_lufs:.1f} LUFS")
    else:
        logging.info(f"  True Peak OK – kein Limiting noetig")

    # Datei ueberschreiben
    _prog(0.70, t("prog_track_write").format(target_name))
    sf.write(target_file, normalized, rate)
    logging.info(f"  Datei ueberschrieben: {target_name}")

    # ── Loudness Correction Metadata schreiben ───────────────────────
    final_peak = np.max(np.abs(normalized))
    final_peak_db = 20 * np.log10(final_peak) if final_peak > 0 else -120.0
    original_peak = np.max(np.abs(data))
    original_peak_db = 20 * np.log10(original_peak) if original_peak > 0 else -120.0
    limiting_applied = peak_db > max_truepeak
    if limiting_applied:
        final_lufs_val = current_lufs + gain_db - (peak_db - max_truepeak)
    else:
        final_lufs_val = target_lufs

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    duration_s = len(data) / rate
    duration_min = int(duration_s // 60)
    duration_sec = duration_s % 60

    meta_path = os.path.join(session_dir, "Loudness Correction Metadata.txt")
    try:
        with open(meta_path, "w", encoding="utf-8") as mf:
            mf.write("=" * 60 + "\n")
            mf.write("  LOUDNESS CORRECTION METADATA\n")
            mf.write("  EBU R128 / ITU-R BS.1770\n")
            mf.write("=" * 60 + "\n\n")
            mf.write(f"  Datum:              {timestamp}\n")
            mf.write(f"  Quelldatei:         {target_name}\n")
            mf.write(f"  Sample-Rate:        {rate} Hz\n")
            mf.write(f"  Kanaele:            {'Stereo' if data.ndim == 2 else 'Mono'}\n")
            mf.write(f"  Dauer:              {duration_min}:{duration_sec:05.2f}\n\n")
            mf.write("-" * 60 + "\n")
            mf.write("  MESSWERTE\n")
            mf.write("-" * 60 + "\n\n")
            mf.write(f"  Original Lautheit:  {current_lufs:.1f} LUFS\n")
            mf.write(f"  Ziel Lautheit:      {target_lufs:.1f} LUFS\n")
            mf.write(f"  Gain-Korrektur:     {gain_db:+.1f} dB\n\n")
            mf.write(f"  Original True Peak: {original_peak_db:.1f} dB\n")
            mf.write(f"  Max True Peak:      {max_truepeak:.1f} dB\n")
            mf.write(f"  True Peak Limiter:  {'Ja (%.1f dB)' % (peak_db - max_truepeak) if limiting_applied else 'Nein'}\n\n")
            mf.write("-" * 60 + "\n")
            mf.write("  ERGEBNIS\n")
            mf.write("-" * 60 + "\n\n")
            mf.write(f"  Endgueltige Lautheit: {final_lufs_val:.1f} LUFS\n")
            mf.write(f"  Endgueltiger Peak:    {final_peak_db:.1f} dB TP\n")
            mf.write(f"  Norm konform:         {'JA' if final_lufs_val >= target_lufs - 0.5 and final_peak_db <= max_truepeak else 'NEIN'}\n\n")
            mf.write("=" * 60 + "\n")
        logging.info(f"  Metadata geschrieben: {meta_path}")
    except Exception as e:
        logging.warning(f"  Metadata schreiben: {e}")

    # Pro Tools aktualisieren und Clip umbenennen
    _prog(0.85, t("prog_track_refresh").format(target_name))
    try:
        # Puffer fuer OS-Datei-Schreibvorgaenge und PT-Hintergrund-Tasks
        time.sleep(1.5)
        try:
            engine.refresh_all_modified_audio_files()
            logging.info("  Pro Tools Audio-Dateien aktualisiert")
        except Exception as re:
            logging.warning(f"  Pro Tools Audio-Dateien Refresh fehlgeschlagen (wird fortgesetzt): {re}")
        time.sleep(3.0)  # PT braucht Zeit um die Datei neu einzulesen

        # Clip-Name = Dateiname ohne Extension (z.B. ST_02.wav -> ST_02)
        clip_name = os.path.splitext(target_name)[0]

        # Bereits umbenannt? (verhindert ST_02-loudness -> ST_02-loudness-loudness)
        if "-loudness" in clip_name:
            logging.info(f"  Clip '{clip_name}' hat bereits '-loudness' Suffix – Umbenennung uebersprungen.")
        else:
            new_name = f"{clip_name}-loudness"
            renamed = False

            # Versuch 1: rename_target_clip mit exaktem Clip-Namen (Dateiname ohne Extension)
            for rf in [True, False]:
                try:
                    engine.rename_target_clip(clip_name, new_name, rename_file=rf)
                    logging.info(f"  Clip umbenannt: {clip_name} -> {new_name} (rename_file={rf})")
                    renamed = True
                    break
                except Exception:
                    continue

            # Versuch 2: Reiner Track-Name (typisch fuer Stereo Interleaved nach Consolidate)
            if not renamed and clip_name != track_name:
                for rf in [True, False]:
                    try:
                        engine.rename_target_clip(track_name, f"{track_name}-loudness", rename_file=rf)
                        logging.info(f"  Clip umbenannt (Track-Name): {track_name} -> {track_name}-loudness (rename_file={rf})")
                        renamed = True
                        new_name = f"{track_name}-loudness"
                        break
                    except Exception:
                        continue

            # Versuch 3: Nummerierte Fallbacks (<track_name>_01, _02, ...)
            if not renamed:
                for i in range(1, 20):
                    try_name = f"{track_name}_{i:02d}"
                    for rf in [True, False]:
                        try:
                            engine.rename_target_clip(try_name, f"{try_name}-loudness", rename_file=rf)
                            logging.info(f"  Clip umbenannt (Fallback): {try_name} -> {try_name}-loudness (rename_file={rf})")
                            renamed = True
                            new_name = f"{try_name}-loudness"
                            break
                        except Exception:
                            continue
                    if renamed:
                        break

            if not renamed:
                logging.warning("  Clip konnte nicht umbenannt werden")
            else:
                # ── Timeline-Clip Rename Absicherung ──────────────────────────
                # Da der Clip auf der Timeline nach dem Trimmen ein Sub-Clip ist,
                # benennt rename_target_clip oft nur das File/Hauptclip um.
                # Wir selektieren den Clip auf der Spur und benennen ihn explizit um.
                try:
                    engine.select_all_clips_on_track(track_name)
                    time.sleep(0.25)
                    engine.rename_selected_clip(new_name, rename_file=False)
                    logging.info(f"  Timeline-Clip auf Spur '{track_name}' umbenannt -> {new_name}")
                except Exception as e:
                    logging.warning(f"  Timeline-Clip Rename fehlgeschlagen auf Spur '{track_name}': {e}")

            # ── Datei-Rename Absicherung ──────────────────────────────────
            # PT aendert manchmal nur den Clip-Namen intern, benennt aber die
            # Datei auf der Festplatte nicht um (besonders bei Stereo Interleaved).
            # Wir pruefen ob die Datei noch den alten Namen hat und benennen sie
            # manuell um, damit PT den korrekten Namen auf der Spur anzeigt.
            if renamed and os.path.exists(target_file):
                new_file = os.path.join(os.path.dirname(target_file),
                                        new_name + os.path.splitext(target_name)[1])
                if not os.path.exists(new_file):
                    try:
                        os.rename(target_file, new_file)
                        logging.info(f"  Datei manuell umbenannt: {target_name} -> {os.path.basename(new_file)}")
                        # Puffer vor dem Refresh
                        time.sleep(0.5)
                        try:
                            engine.refresh_all_modified_audio_files()
                        except Exception as re:
                            logging.warning(f"  Pro Tools Audio-Dateien Refresh nach manuellem Rename fehlgeschlagen: {re}")
                        time.sleep(1.5)
                    except OSError as e:
                        logging.warning(f"  Datei-Rename fehlgeschlagen: {e}")
                else:
                    logging.info(f"  Datei bereits umbenannt: {os.path.basename(new_file)}")
    except Exception as e:
        logging.warning(f"  PT Rename/Refresh Hauptfehler: {e}")

    _prog(0.98, f"Spur '{track_name}': Fertig.")


# ─────────────────────────────────────────────────────────────────────────────
# Lautheits-Fortschrittsfenster
# ─────────────────────────────────────────────────────────────────────────────

_loudness_win_refs = []  # Hält ObjC-Referenzen am Leben (verhindert PyObjC-Dealloc-Crash)


def _dispatch_main(fn):
    """Schedult fn() auf dem Haupt-Run-Loop (fire-and-forget)."""
    try:
        import Foundation as _F
        _F.NSRunLoop.mainRunLoop().performBlock_(fn)
    except Exception:
        pass


def _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp):
    """Ruft normalize_track für jede Spur auf und zeigt dabei ein Fortschrittsfenster."""
    import AppKit as _AK

    win_ref   = [None]
    bar_ref   = [None]
    phase_ref = [None]
    WIN_W, WIN_H = 360, 100

    def _make_window():
        try:
            rect  = _AK.NSMakeRect(0, 0, WIN_W, WIN_H)
            win   = _AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, _AK.NSWindowStyleMaskTitled, _AK.NSBackingStoreBuffered, False)
            win.setTitle_(t("prog_loudness_win_title"))
            win.setLevel_(3)
            win.center()

            cv = win.contentView()

            lbl = _AK.NSTextField.alloc().initWithFrame_(
                _AK.NSMakeRect(20, WIN_H - 38, WIN_W - 40, 18))
            lbl.setStringValue_(t("prog_loudness_init"))
            lbl.setBezeled_(False)
            lbl.setEditable_(False)
            lbl.setDrawsBackground_(False)
            lbl.setFont_(_AK.NSFont.systemFontOfSize_(12))
            cv.addSubview_(lbl)

            bar = _AK.NSProgressIndicator.alloc().initWithFrame_(
                _AK.NSMakeRect(20, WIN_H - 66, WIN_W - 40, 16))
            bar.setStyle_(0)  # 0 = NSProgressIndicatorBarStyle (Balken)
            bar.setIndeterminate_(False)
            bar.setMinValue_(0.0)
            bar.setMaxValue_(1.0)
            bar.setDoubleValue_(0.0)
            cv.addSubview_(bar)

            win_ref[0]   = win
            bar_ref[0]   = bar
            phase_ref[0] = lbl
            _loudness_win_refs.extend([win, bar, lbl])
            win.makeKeyAndOrderFront_(None)
        except Exception as e:
            logging.debug(f"  Loudness-Fortschrittsfenster: {e}")

    def _update(frac, msg):
        def _do():
            try:
                if bar_ref[0]:   bar_ref[0].setDoubleValue_(frac)
                if phase_ref[0]: phase_ref[0].setStringValue_(msg)
            except Exception:
                pass
        _dispatch_main(_do)

    def _close():
        def _do():
            try:
                if win_ref[0]:
                    win_ref[0].orderOut_(None)
                    win_ref[0] = None
            except Exception:
                pass
        _dispatch_main(_do)

    _dispatch_main(_make_window)
    time.sleep(0.15)

    n = max(len(loud_tracks), 1)
    for i, lt in enumerate(loud_tracks):
        base, span = i / n, 1.0 / n
        def _cb(frac, msg, _b=base, _s=span):
            _update(_b + frac * _s, msg)
        normalize_track(engine, session_dir, lt, target_lufs, max_tp, progress_cb=_cb)

    _update(1.0, t("prog_loudness_done"))
    time.sleep(0.8)
    _close()


# ─────────────────────────────────────────────────────────────────────────────
# Fortschrittsfenster für Import / Export
# ─────────────────────────────────────────────────────────────────────────────
_prog_win_refs = []  # Hält ObjC-Referenzen am Leben

def _show_progress_win(title):
    """
    Öffnet ein schwebendes Fortschrittsfenster ohne Fokus-Diebstahl.
    Gibt {"update": fn(frac, msg), "close": fn()} zurück.
    orderFront_ statt makeKeyAndOrderFront_ → Fokus bleibt beim aktiven Fenster.
    """
    import AppKit as _AK
    WIN_W, WIN_H = 320, 76

    win_ref = [None]
    lbl_ref = [None]
    bar_ref = [None]

    def _make():
        try:
            rect = _AK.NSMakeRect(0, 0, WIN_W, WIN_H)
            win = _AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, _AK.NSWindowStyleMaskTitled, _AK.NSBackingStoreBuffered, False
            )
            win.setTitle_(title)
            win.setLevel_(3)  # NSFloatingWindowLevel – immer sichtbar, kein Fokus
            win.setIgnoresMouseEvents_(True)
            screen = _AK.NSScreen.mainScreen()
            if screen:
                sf = screen.frame()
                win.setFrameOrigin_(_AK.NSMakePoint(
                    sf.size.width - WIN_W - 20,
                    sf.size.height - WIN_H - 56
                ))
            cv = win.contentView()
            lbl = _AK.NSTextField.alloc().initWithFrame_(
                _AK.NSMakeRect(12, WIN_H - 36, WIN_W - 24, 16)
            )
            lbl.setStringValue_("Starte…")
            lbl.setBezeled_(False); lbl.setEditable_(False); lbl.setDrawsBackground_(False)
            lbl.setFont_(_AK.NSFont.systemFontOfSize_(11))
            cv.addSubview_(lbl)
            bar = _AK.NSProgressIndicator.alloc().initWithFrame_(
                _AK.NSMakeRect(12, WIN_H - 58, WIN_W - 24, 12)
            )
            bar.setStyle_(0)
            bar.setIndeterminate_(False)
            bar.setMinValue_(0.0); bar.setMaxValue_(1.0); bar.setDoubleValue_(0.0)
            cv.addSubview_(bar)
            win_ref[0] = win; lbl_ref[0] = lbl; bar_ref[0] = bar
            _prog_win_refs.extend([win, lbl, bar])
            win.orderFront_(None)  # kein makeKeyAndOrderFront_ → kein Fokus-Diebstahl
        except Exception as e:
            logging.debug(f"  Fortschrittsfenster: {e}")

    def update(frac, msg):
        def _do():
            try:
                if lbl_ref[0]: lbl_ref[0].setStringValue_(msg)
                if bar_ref[0]: bar_ref[0].setDoubleValue_(frac)
            except Exception: pass
        _dispatch_main(_do)

    def close():
        def _do():
            try:
                if win_ref[0]:
                    win_ref[0].orderOut_(None)
                    win_ref[0] = None
            except Exception: pass
        _dispatch_main(_do)

    _dispatch_main(_make)
    time.sleep(0.1)
    return {"update": update, "close": close}


# ─────────────────────────────────────────────────────────────────────────────
# Globale Referenzliste für ObjC-Objekte (verhindert PyObjC Dealloc-Crash)
# Python-Attribut-Assignments auf ObjC-Proxies lösen Deallokationskaskaden
# aus die in PyObjC/Python 3.14 SIGBUS/SIGSEGV verursachen.
# Deshalb: NIEMALS ObjC-Objekte als Instanz-Attribute speichern/überschreiben.
# ─────────────────────────────────────────────────────────────────────────────
_config_refs = []  # Wird in _open_config_window befüllt

def _close_existing_settings_window():
    """Schließt ein noch offenes Einstellungsfenster und gibt alle Referenzen frei.
    Verhindert den PyObjC-Crash der entsteht wenn _config_refs überschrieben wird
    während ObjC noch eine schwache Referenz auf den alten target hält."""
    global _config_refs
    if not _config_refs:
        return False  # kein Fenster offen
    try:
        win = _config_refs[0]
        if win.isVisible():
            win.makeKeyAndOrderFront_(None)
            return True  # Fenster ist sichtbar → nach vorne bringen, kein neues öffnen
        win.close()
    except Exception:
        pass
    _config_refs = []
    return False  # Fenster war geschlossen → neu öffnen erlaubt

# ── Icon-Pfad ermitteln (global, wird für Dock, Fenster und Menüleiste verwendet) ─
_ICON_PNG_PATH = None

def _resolve_icon_path():
    """Ermittelt den Pfad zum PunchBuddy Icon (PNG)."""
    global _ICON_PNG_PATH
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "PunchBuddy.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Resources", "PunchBuddy.png"),
    ]
    for p in candidates:
        if os.path.exists(p):
            _ICON_PNG_PATH = p
            return p
    return None

def _load_ns_icon():
    """Lädt das PunchBuddy Icon als NSImage (für Dock und Fenster)."""
    if not APPKIT_OK:
        return None
    path = _ICON_PNG_PATH or _resolve_icon_path()
    if path and os.path.exists(path):
        try:
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
            if icon:
                icon.setSize_(AppKit.NSMakeSize(128, 128))
                return icon
        except Exception as e:
            logging.warning(f"Icon laden fehlgeschlagen: {e}")
    return None

def _ensure_dock_icon():
    """Setzt PunchBuddy Icon UND Namen im Dock.
    
    Wenn ein Python-Script läuft, zeigt macOS im Dock standardmäßig
    'Python' als Name und das Python-Icon. Diese Funktion überschreibt
    beides über das NSBundle-InfoDictionary und die NSApplication API.
    """
    if not APPKIT_OK:
        return
    try:
        ns_app = AppKit.NSApplication.sharedApplication()

        # 1. Dock-Name ändern: CFBundleName im InfoDictionary überschreiben
        #    Damit zeigt macOS im Dock "PunchBuddy" statt "Python"
        bundle = AppKit.NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info:
            info['CFBundleName'] = 'PunchBuddy'

        # 2. Activation Policy auf Regular → App erscheint im Dock
        ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

        # 3. Dock-Icon setzen
        icon = _load_ns_icon()
        if icon:
            ns_app.setApplicationIconImage_(icon)

        logging.info("Dock: Name='PunchBuddy', Icon gesetzt.")
    except Exception as e:
        logging.warning(f"Dock-Icon/Name setzen fehlgeschlagen: {e}")

_resolve_icon_path()  # Beim Import einmal auflösen


def _get_network_interfaces():
    """Returns [(display_label, ip), ...] for all active network interfaces."""
    result = [("Localhost (127.0.0.1)", "127.0.0.1")]
    try:
        import subprocess as _sp
        out = _sp.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        current_iface = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                current_iface = line.split(":")[0].strip()
            elif current_iface and "inet " in line and "inet6" not in line:
                parts = line.strip().split()
                try:
                    ip = parts[parts.index("inet") + 1]
                    if ip and ip != "127.0.0.1":
                        result.append((f"{current_iface}  ({ip})", ip))
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Menüleisten-App
# ─────────────────────────────────────────────────────────────────────────────
class PunchBuddyApp(rumps.App):
    def __init__(self):
        super().__init__("PunchBuddy", icon=_ICON_PNG_PATH, template=True,
                         quit_button=None)  # Custom Quit-Handler für Watchdog-Cleanup
        global _app_ref
        _app_ref = self
        self.settings        = load_settings()

        # ── Profil A (Standard) ──────────────────────────────────────────
        self.start_item      = rumps.MenuItem(t("record_a_start"),      callback=self._on_start)

        # ── Profil B (zweiter Trigger) ───────────────────────────────────
        self.start_b_item    = rumps.MenuItem(t("record_b_start"),      callback=self._on_start_b)

        # ── Play (Auto Monitor) ──────────────────────────────────────────
        self.play_item       = rumps.MenuItem(t("play_input"),      callback=self._on_start_play)

        # ── Play Custom ──────────────────────────────────────────────────
        self.play_custom_item = rumps.MenuItem(t("play_custom"), callback=self._on_start_play_custom)

        # ── Export ────────────────────────────────────────────────────────
        self.wav_export_item   = rumps.MenuItem(t("export_wav"),  callback=self._on_start_export_wav)
        self.aaf_export_item   = rumps.MenuItem(t("export_aaf"),  callback=self._on_start_export_aaf)
        self.interplay_export_item = rumps.MenuItem(t("export_interplay"), callback=self._on_start_export_interplay)

        self.import_item       = rumps.MenuItem(t("interplay_import_start"),  callback=self._on_start_import)

        self.settings_item      = rumps.MenuItem(t("settings"),  callback=self._on_unified_settings)
        self.open_log_item      = rumps.MenuItem(t("open_log"), callback=self._on_open_log)
        self.refresh_tracks_item = rumps.MenuItem(t("refresh_tracks"), callback=self._on_refresh_tracks)

        # ── Menuestruktur ────────────────────────────────────────────────
        
        # 1. Haupt-Trigger (Top Level)
        self.menu = [
            self.start_item,
            self.start_b_item,
            self.play_item,
            self.play_custom_item,
            rumps.separator,
        ]

        # Export-Untermenue
        self.export_menu = rumps.MenuItem(t("tab_export"))
        self.export_menu.add(self.wav_export_item)
        self.export_menu.add(self.aaf_export_item)
        self.export_menu.add(self.interplay_export_item)
        self.menu.add(self.export_menu)

        self.menu.add(self.import_item)
        self.menu.add(rumps.separator)

        # 2. Einstellungen (nur noch Fenster – URLs sind im Webtrigger-Tab)
        self.menu.add(rumps.separator)
        self.menu.add(self.settings_item)
        self.menu.add(self.refresh_tracks_item)
        self.menu.add(self.open_log_item)

        
        # 3. Hilfe & Info (Untermenü)
        self.help_menu = rumps.MenuItem(t("help_info"))
        
        self.manual_item = rumps.MenuItem(t("manual"), callback=self._on_help_manual)
        self.docs_item = rumps.MenuItem(t("tech_docs"), callback=self._on_help_docs)
        
        self.credits_title = rumps.MenuItem(t("developed_by"), callback=None)
        credits_names = rumps.MenuItem("Jens Rühl & Christian Becker", callback=None)
        
        self.opensource_item = rumps.MenuItem(t("opensource"), callback=self._on_help_opensource)

        self.help_menu.add(self.manual_item)
        self.help_menu.add(self.docs_item)
        self.help_menu.add(rumps.separator)
        self.help_menu.add(self.opensource_item)
        self.help_menu.add(rumps.separator)
        self.help_menu.add(self.credits_title)
        self.help_menu.add(credits_names)

        self.menu.add(self.help_menu)
        self.menu.add(rumps.separator)
        
        self.quit_item = rumps.MenuItem(t("quit"), callback=self._on_quit)
        self.menu.add(self.quit_item)


        self._start_http()
        self._start_keepalive()

        # ── Startup: Pre-Roll sicherstellen dass AUS ────────────────────────
        def _startup_preroll_check():
            time.sleep(2.0)  # Kurz warten bis PT vollständig bereit
            logging.info("=== Startup: Pre-Roll auf AUS stellen ===")
            restore_preroll()
            # Track-Cache beim Start befüllen – erspart PTSL-Call beim ersten Record
            eng = _get_engine()
            if eng is not None:
                refresh_session_tracks(eng)

        threading.Thread(target=_startup_preroll_check, daemon=True, name="StartupPreRoll").start()

        # ── Tooltip für Menüleisten-Icon setzen (nach App-Start) ───────────
        self._tooltip_timer = rumps.Timer(self._set_tooltip, 1)
        self._tooltip_timer.start()

    # ── Internes ──────────────────────────────────────────────────────────

    def _set_tooltip(self, timer):
        """Setzt Tooltip und Dock-Icon nach App-Start (NSApp existiert jetzt)."""
        timer.stop()
        try:
            # ── Tooltip für Menüleisten-Icon ────────────────────────────────
            if hasattr(self, '_nsapp') and self._nsapp:
                nsstatusitem = getattr(self._nsapp, 'nsstatusitem', None)
                if nsstatusitem:
                    nsstatusitem.button().setToolTip_("PunchBuddy")
                    logging.info("Menüleisten-Tooltip gesetzt: PunchBuddy")
        except Exception as e:
            logging.debug(f"Tooltip setzen fehlgeschlagen: {e}")

        # ── Dock-Icon sofort beim Start anzeigen ─────────────────────────
        _ensure_dock_icon()

    def load_preset_by_index(self, idx):
        presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if not (0 <= idx < len(presets)):
            logging.error(f"Preset-Index {idx} ungültig (0-{len(presets)-1})")
            return False

        p = presets[idx]

        # Spurenzuweisungen anwenden
        self.settings["tracks"] = p.get("rec_a", [])
        self.settings["monitor_tracks"] = p.get("mon_a", [])
        self.settings["tracks_b"] = p.get("rec_b", [])
        self.settings["monitor_tracks_b"] = p.get("mon_b", [])
        self.settings["export_tracks"] = p.get("export_tracks", p.get("export", []))
        self.settings["loudness_tracks"] = p.get("loudness_tracks", [])
        self.settings["play_monitor_tracks"] = p.get("play_monitor_tracks", [])

        # Kontrollen-Keys anwenden
        bool_keys = ["wav_export_enabled", "aaf_export_enabled", "interplay_enabled",
                     "loudness_enabled", "interplay_rename_enabled", "import_close_session"]
        for key in bool_keys:
            if key in p:
                self.settings[key] = p[key]

        str_keys = ["export_start_tc", "video_track", "interplay_workspace",
                    "export_error_keywords", "export_success_keywords",
                    "interplay_rename_prefix", "interplay_rename_suffix"]
        for key in str_keys:
            if key in p:
                self.settings[key] = p[key]

        int_keys = ["extend_count", "interplay_workspace_steps",
                    "interplay_rename_trim_start", "interplay_rename_trim_end", "http_port"]
        for key in int_keys:
            if key in p:
                self.settings[key] = p[key]

        float_keys = ["target_lufs", "max_truepeak"]
        for key in float_keys:
            if key in p:
                self.settings[key] = p[key]

        if "http_bind_host" in p:
            self.settings["http_bind_host"] = p["http_bind_host"]

        save_settings(self.settings)
        logging.info(f"Preset {idx} ('{p.get('name')}') erfolgreich geladen und gespeichert.")
        return True

    def _start_http(self):
        app_ref = self

        class _ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class Handler(http.server.BaseHTTPRequestHandler):
            def _fire(self, fn):
                """Antwortet sofort mit 200 OK und führt fn in eigenem Thread aus."""
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
                threading.Thread(target=fn, daemon=True).start()

            def _authorized(self, query):
                """Prüft das Webtrigger-Token, falls eines konfiguriert ist.
                Token via ?token=… oder X-Auth-Token-Header."""
                expected = app_ref.settings.get("webtrigger_token", "") or ""
                supplied = (query.get("token", [""])[0]
                            or self.headers.get("X-Auth-Token", ""))
                return webtrigger_token_ok(expected, supplied)

            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if not self._authorized(query):
                    self.send_response(401)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Unauthorized")
                    return

                if path == "/trigger":
                    self._fire(app_ref._trigger)
                elif path == "/trigger2":
                    self._fire(app_ref._trigger_b)
                elif path == "/import":
                    self._fire(app_ref._trigger_import)
                elif path == "/export_wav":
                    self._fire(app_ref._trigger_export_wav)
                elif path == "/export_aaf":
                    self._fire(app_ref._trigger_export_aaf)
                elif path == "/export_interplay":
                    self._fire(app_ref._trigger_export_interplay)
                elif path == "/play":
                    self._fire(app_ref._trigger_play)
                elif path == "/play_custom":
                    self._fire(app_ref._trigger_play_custom)
                elif path == "/start":
                    self._fire(app_ref._trigger_start)
                elif path.startswith("/preset/"):
                    try:
                        preset_num = int(path.split("/preset/")[1])
                        idx = preset_num - 1
                        if app_ref.load_preset_by_index(idx):
                            self.send_response(200)
                            self.send_header("Content-type", "text/plain; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(f"Preset {preset_num} geladen".encode("utf-8"))
                        else:
                            self.send_response(400)
                            self.send_header("Content-type", "text/plain; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(b"Ungueltiger Preset Index")
                    except Exception as e:
                        self.send_response(500)
                        self.send_header("Content-type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(str(e).encode("utf-8"))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt, *args):
                pass  # kein HTTP-Logging in Konsole

        try:
            port = self.settings.get("http_port", 8899)
            bind_host = self.settings.get("http_bind_host", "127.0.0.1")
            actual_bind = "127.0.0.1" if bind_host == "127.0.0.1" else "0.0.0.0"

            # Sicherheit: Bei Bind auf alle Interfaces (LAN erreichbar) ist ein
            # Token Pflicht. Fehlt es, wird einmalig eines erzeugt und gespeichert,
            # damit der Server nicht ungeschützt im Netz steht.
            if actual_bind == "0.0.0.0" and not self.settings.get("webtrigger_token"):
                import secrets
                self.settings["webtrigger_token"] = secrets.token_urlsafe(16)
                save_settings(self.settings)
                logging.warning(
                    "Webtrigger ist im LAN erreichbar (0.0.0.0) – automatisch ein "
                    "Token erzeugt. Stream-Deck-URLs müssen ?token=… anhängen.")
            _tok = self.settings.get("webtrigger_token", "")
            if _tok:
                logging.info(f"Webtrigger-Token aktiv (an URLs anhängen: ?token={_tok})")

            self.httpd = None
            for attempt in range(4):
                try:
                    self.httpd = _ThreadingHTTPServer((actual_bind, port), Handler)
                    break
                except OSError as e:
                    if attempt < 3:
                        logging.warning(f"Port {port} belegt (Versuch {attempt+1}/4), warte 0.5s...")
                        time.sleep(0.5)
                    else:
                        logging.warning(f"Port {port} dauerhaft belegt – weiche auf freien Port aus (wird NICHT gespeichert)...")
                        self.httpd = _ThreadingHTTPServer((actual_bind, 0), Handler)
                        port = self.httpd.server_address[1]
            self._http_port = port
            self._http_bind_host_display = bind_host
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            atexit.register(self.httpd.shutdown)
            logging.info(f"Stream Deck HTTP-Server lauscht auf {actual_bind}:{port} (Anzeige: {bind_host})")
            logging.info(f"  /trigger  → Profil A")
            logging.info(f"  /trigger2 → Profil B")
            logging.info(f"  /export   → Export (komplett)")
            logging.info(f"  /import   → Interplay Import")
            logging.info(f"  /export_wav → WAV Export")
            logging.info(f"  /export_interplay → Interplay Export")
            logging.info(f"  /play       → Play/Stop (Toggle)")
            logging.info(f"  /play_custom → Play Custom (Mute KH2/Unmute ST Abh)")
            logging.info(f"  /start       → Cursor auf Start-Timecode")
            logging.info(f"  /preset/{{1-8}} → Preset 1-8 laden")
        except Exception as e:
            self._http_port = 8899  # Fallback für URL-Anzeige
            self._http_bind_host_display = self.settings.get("http_bind_host", "127.0.0.1")
            logging.error(f"HTTP-Server Start fehlgeschlagen: {e}")

    def _restart_http(self):
        if hasattr(self, 'httpd') and self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self._start_http()

    def _start_keepalive(self):
        """Startet einen Hintergrund-Thread der alle 30s transport_state() abfragt.
        Hält die PTSL-Verbindung warm und verhindert den NEXIS-Cold-Start-Delay."""
        _KEEPALIVE_INTERVAL = 30.0

        def _loop():
            while True:
                time.sleep(_KEEPALIVE_INTERVAL)
                # Nicht pingen während Aufnahme oder Import läuft
                if _running or _import_running:
                    continue
                with _engine_lock:
                    eng = _engine_instance
                if eng is None:
                    continue
                # Über _ptsl_call: serialisiert via _ptsl_lock (kein Race mit
                # echten Befehlen) und mit gRPC-Deadline. Ein Fehler verwirft
                # die Engine, der nächste Trigger verbindet frisch neu.
                ok, _ = _ptsl_call(eng.transport_state, label="KeepAlive", timeout=10.0)
                if ok:
                    logging.debug("KeepAlive: transport_state() OK")
                else:
                    logging.debug("KeepAlive: transport_state() fehlgeschlagen")

        threading.Thread(target=_loop, daemon=True, name="PTSLKeepAlive").start()
        logging.info("PTSL Keep-Alive gestartet (Intervall: 30s)")

    def _trigger(self):
        logging.info(">>> TRIGGER A <<<")
        tracks = self.settings.get("tracks", DEFAULT_SETTINGS["tracks"])
        monitor = self.settings.get("monitor_tracks", DEFAULT_SETTINGS["monitor_tracks"])
        threading.Thread(target=run_punch_in, args=(tracks, monitor), daemon=True).start()

    def _trigger_play(self):
        logging.info(">>> TRIGGER PLAY <<<")
        threading.Thread(target=run_play, daemon=True).start()

    def _trigger_play_custom(self):
        logging.info(">>> TRIGGER PLAY CUSTOM <<<")
        threading.Thread(target=run_play_custom, daemon=True).start()

    def _trigger_start(self):
        threading.Thread(target=run_goto_start, daemon=True).start()

    def _trigger_b(self):
        logging.info(">>> TRIGGER B <<<")
        tracks = self.settings.get("tracks_b", DEFAULT_SETTINGS["tracks_b"])
        if not tracks:
            logging.warning("Profil B hat keine Spuren definiert – Trigger ignoriert.")
            return
        monitor = self.settings.get("monitor_tracks_b", DEFAULT_SETTINGS["monitor_tracks_b"])
        threading.Thread(target=run_punch_in, args=(tracks, monitor), daemon=True).start()

    def _trigger_import(self):
        logging.info(">>> IMPORT TRIGGER <<<")
        threading.Thread(target=run_interplay_import, daemon=True).start()

    def _trigger_export_wav(self):
        logging.info(">>> WAV EXPORT TRIGGER <<<")
        _dispatch_main(lambda: _set_busy(True))
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        threading.Thread(target=run_wav_export_standalone, args=(export_tracks, self.settings), daemon=True).start()

    def _trigger_export_aaf(self):
        logging.info(">>> AAF EXPORT TRIGGER <<<")
        _dispatch_main(lambda: _set_busy(True))
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        threading.Thread(target=run_aaf_export_standalone, args=(export_tracks, self.settings), daemon=True).start()

    def _trigger_export_interplay(self):
        logging.info(">>> INTERPLAY EXPORT TRIGGER <<<")
        _dispatch_main(lambda: _set_busy(True))  # sofort sichtbar, noch vor Thread-Start
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        ws_steps = self.settings.get("interplay_workspace_steps", 17)
        threading.Thread(target=run_interplay_export, args=(export_tracks, self.settings, ws_steps), daemon=True).start()

    def update_menu_titles(self):
        """Updates all menu titles dynamically to reflect the current language."""
        self.start_item.title = t("record_a_start")
        self.start_b_item.title = t("record_b_start")
        self.play_item.title = t("play_input")
        self.play_custom_item.title = t("play_custom")
        self.wav_export_item.title = t("export_wav")
        self.aaf_export_item.title = t("export_aaf")
        self.interplay_export_item.title = t("export_interplay")
        self.import_item.title = t("interplay_import_start")
        self.settings_item.title = t("settings")
        self.refresh_tracks_item.title = t("refresh_tracks")
        self.open_log_item.title = t("open_log")
        self.export_menu.title = t("tab_export")
        self.help_menu.title = t("help_info")
        self.manual_item.title = t("manual")
        self.docs_item.title = t("tech_docs")
        self.credits_title.title = t("developed_by")
        self.opensource_item.title = t("opensource")
        self.quit_item.title = t("quit")

    # ── Menü-Callbacks ────────────────────────────────────────────────────

    def _on_start(self, _):
        logging.info(">>> MENU 'Record A starten' <<<")
        self._trigger()

    def _on_start_b(self, _):
        logging.info(">>> MENU 'Record B starten' <<<")
        self._trigger_b()

    def _on_start_play(self, _):
        logging.info(">>> MENU 'Play Input' <<<")
        self._trigger_play()

    def _on_start_play_custom(self, _):
        logging.info(t("log_menu_play_custom"))
        self._trigger_play_custom()

    def _on_start_import(self, _):
        logging.info(">>> MENU 'Interplay Import starten' <<<")
        self._trigger_import()

    def _on_quit(self, _):
        logging.info(">>> MENU 'Beenden' <<<")
        _cleanup_watchdog()
        _close_engine()
        logging.info("PunchBuddy beendet.")
        rumps.quit_application()

    def _copy_url(self, path, label="URL"):
        """Generische Methode: kopiert eine Trigger-URL in die Zwischenablage."""
        port = getattr(self, '_http_port', 8899)
        url = f"http://127.0.0.1:{port}{path}"
        try:
            subprocess.run(["pbcopy"], input=url, universal_newlines=True)
            rumps.notification("PunchBuddy", "Kopiert", f"{label}: {url}")
        except Exception:
            rumps.alert(label, f"URL:\n{url}")


    def _on_unified_settings(self, _):
        """Öffnet das kombinierte Einstellungs- und Spurenauswahl-Fenster."""
        _ensure_dock_icon()
        if not APPKIT_OK:
            rumps.alert(t("alert_error"), "AppKit nicht verfügbar.")
            return

        # Spuren aus Pro Tools lesen
        track_names = []
        try:
            engine = _get_engine()
            if engine is not None:
                ok, all_tracks = _ptsl_call(engine.track_list, label="SettingsTrackList", timeout=5.0)
                if ok and all_tracks:
                    track_names = [t.name for t in all_tracks]
        except Exception as e:
            logging.warning(f"Konnte Spuren nicht aus Pro Tools lesen: {e}")

        try:
            self._open_unified_settings_window(track_names)
        except Exception as e:
            logging.error(f"Fehler beim Oeffnen des Fensters: {e}", exc_info=True)
            rumps.alert(t("alert_error"), f"Fenster konnte nicht geoeffnet werden:\n{e}")

    def _on_refresh_tracks(self, _):
        logging.info(">>> MENU 'Spuren neu einlesen' <<<")
        _invalidate_session_tracks()
        eng = _get_engine()
        if eng is None:
            rumps.alert(t("alert_error"), "PTSL nicht verfügbar.")
            return
        ok = refresh_session_tracks(eng)
        if ok:
            with _track_cache_lock:
                n = len(_cached_track_names) if _cached_track_names else 0
            rumps.notification("PunchBuddy", t("refresh_tracks"), f"{n} Spuren eingelesen.")
        else:
            rumps.alert(t("alert_error"), "Spuren konnten nicht gelesen werden.")

    def _on_open_log(self, _):
        logging.info(">>> MENU 'Log File oeffnen' <<<")
        if os.path.exists(LOG_PATH):
            subprocess.Popen(["open", LOG_PATH])
        else:
            rumps.alert(t("alert_note"), t("msg_log_not_created"))

    def _on_start_export_wav(self, _):
        logging.info(">>> MENU 'WAV Export' <<<")
        self._trigger_export_wav()

    def _on_start_export_aaf(self, _):
        logging.info(">>> MENU 'AAF Export' <<<")
        self._trigger_export_aaf()

    def _on_start_export_interplay(self, _):
        logging.info(">>> MENU 'Interplay Export' <<<")
        self._trigger_export_interplay()

    def _on_open_general_settings(self, _):
        _ensure_dock_icon()
        if not APPKIT_OK:
            rumps.alert("Fehler", "AppKit nicht verfügbar – natives Fenster kann nicht geöffnet werden.")
            return
        
        try:
            self._open_settings_window()
        except Exception as e:
            logging.error(f"Fehler beim Oeffnen des Einstellungsfensters: {e}", exc_info=True)
            rumps.alert("Fehler", f"Fenster konnte nicht geoeffnet werden:\n{e}")

    def _open_settings_window(self):
        if _close_existing_settings_window():
            return
        NSWindow          = AppKit.NSWindow
        NSView            = AppKit.NSView
        NSButton          = AppKit.NSButton
        NSTextField       = AppKit.NSTextField
        NSFont            = AppKit.NSFont
        NSMakeRect        = AppKit.NSMakeRect
        NSOnState         = AppKit.NSOnState
        NSOffState        = AppKit.NSOffState
        NSBezelStyleRounded = AppKit.NSBezelStyleRounded

        WIN_W = 400
        WIN_H = 580
        PAD = 20

        rect = NSMakeRect(0, 0, WIN_W, WIN_H)
        style = (
            AppKit.NSWindowStyleMaskTitled |
            AppKit.NSWindowStyleMaskClosable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            AppKit.NSBackingStoreBuffered,
            False
        )
        window.setTitle_("Einstellungen PunchBuddy")
        window.setLevel_(3)

        # Fenster-Icon setzen
        _win_icon = _load_ns_icon()
        if _win_icon:
            _win_icon.setSize_(AppKit.NSMakeSize(32, 32))
            window.setRepresentedURL_(AppKit.NSURL.URLWithString_("punchbuddy://settings"))
            window.standardWindowButton_(AppKit.NSWindowDocumentIconButton).setImage_(_win_icon)

        content = window.contentView()
        content.setWantsLayer_(True)

        target = _SettingsButtonTarget.alloc().init()
        controls = {}

        # ── Loudness Section ──
        y = WIN_H - 40
        lbl = NSTextField.labelWithString_("Lautheitskorrektur (EBU R128):")
        lbl.setFrame_(NSMakeRect(PAD, y, 200, 20))
        lbl.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl)

        y -= 25
        cb_loudness = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_loudness.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_loudness.setTitle_("")
        cb_loudness.setState_(NSOnState if self.settings.get("loudness_enabled", True) else NSOffState)
        content.addSubview_(cb_loudness)
        controls["loudness_enabled"] = cb_loudness
        lbl_cb1 = NSTextField.labelWithString_("Aktivieren")
        lbl_cb1.setFrame_(NSMakeRect(PAD + 25, y+2, 100, 20))
        content.addSubview_(lbl_cb1)

        y -= 25
        lbl_lufs = NSTextField.labelWithString_("Ziel-Lautheit (LUFS):")
        lbl_lufs.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_lufs)
        tf_lufs = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 80, 22))
        tf_lufs.setStringValue_(str(self.settings.get("target_lufs", -23.0)))
        content.addSubview_(tf_lufs)
        controls["target_lufs"] = tf_lufs

        y -= 25
        lbl_tp = NSTextField.labelWithString_("Max True Peak (dB):")
        lbl_tp.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_tp)
        tf_tp = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 80, 22))
        tf_tp.setStringValue_(str(self.settings.get("max_truepeak", -3.0)))
        content.addSubview_(tf_tp)
        controls["max_truepeak"] = tf_tp

        # ── Export Einstellungen Allgemein ──
        y -= 40
        lbl_tc = NSTextField.labelWithString_("Export Einstellungen Allgemein:")
        lbl_tc.setFrame_(NSMakeRect(PAD, y, 250, 20))
        lbl_tc.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl_tc)

        y -= 25
        lbl_vt = NSTextField.labelWithString_("Video-Spurname:")
        lbl_vt.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_vt)
        tf_vt = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 180, 22))
        tf_vt.setStringValue_(str(self.settings.get("video_track", "Video 1")))
        content.addSubview_(tf_vt)
        controls["video_track"] = tf_vt

        y -= 25
        lbl_tc2 = NSTextField.labelWithString_("Start-Timecode (HH:MM:SS:FF):")
        lbl_tc2.setFrame_(NSMakeRect(PAD, y, 200, 20))
        content.addSubview_(lbl_tc2)
        tf_tc = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 200, y, 120, 22))
        tf_tc.setStringValue_(str(self.settings.get("export_start_tc", "10:00:00:00")))
        content.addSubview_(tf_tc)
        controls["export_start_tc"] = tf_tc

        y -= 25
        lbl_ext = NSTextField.labelWithString_("Shift+Ö Anzahl (Spurausdehnung):")
        lbl_ext.setFrame_(NSMakeRect(PAD, y, 210, 20))
        content.addSubview_(lbl_ext)
        tf_ext = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 210, y, 60, 22))
        tf_ext.setStringValue_(str(self.settings.get("extend_count", 7)))
        content.addSubview_(tf_ext)
        controls["extend_count"] = tf_ext

        y -= 25
        cb_wav = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_wav.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_wav.setTitle_("")
        cb_wav.setState_(NSOnState if self.settings.get("wav_export_enabled", False) else NSOffState)
        content.addSubview_(cb_wav)
        controls["wav_export_enabled"] = cb_wav
        lbl_wav = NSTextField.labelWithString_("WAV Export aktivieren")
        lbl_wav.setFrame_(NSMakeRect(PAD + 25, y+2, 200, 20))
        content.addSubview_(lbl_wav)

        y -= 25
        cb_aaf = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_aaf.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_aaf.setTitle_("")
        cb_aaf.setState_(NSOnState if self.settings.get("aaf_export_enabled", False) else NSOffState)
        content.addSubview_(cb_aaf)
        controls["aaf_export_enabled"] = cb_aaf
        lbl_aaf = NSTextField.labelWithString_("AAF Export aktivieren (Embedded Audio)")
        lbl_aaf.setFrame_(NSMakeRect(PAD + 25, y+2, 250, 20))
        content.addSubview_(lbl_aaf)

        # ── Interplay Section ──
        y -= 40
        lbl_ip = NSTextField.labelWithString_("Avid Interplay NEXIS Export:")
        lbl_ip.setFrame_(NSMakeRect(PAD, y, 200, 20))
        lbl_ip.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl_ip)

        y -= 25
        cb_interplay = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_interplay.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_interplay.setTitle_("")
        cb_interplay.setState_(NSOnState if self.settings.get("interplay_enabled", False) else NSOffState)
        content.addSubview_(cb_interplay)
        controls["interplay_enabled"] = cb_interplay
        lbl_cb2 = NSTextField.labelWithString_("Aktivieren")
        lbl_cb2.setFrame_(NSMakeRect(PAD + 25, y+2, 100, 20))
        content.addSubview_(lbl_cb2)

        y -= 25
        lbl_ws = NSTextField.labelWithString_("Workspace Name:")
        lbl_ws.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_ws)
        tf_ws = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 180, 22))
        tf_ws.setStringValue_(str(self.settings.get("interplay_workspace", "001-aktuelles [fad-nexis]")))
        content.addSubview_(tf_ws)
        controls["interplay_workspace"] = tf_ws

        y -= 25
        lbl_steps = NSTextField.labelWithString_("Workspace Position (Steps):")
        lbl_steps.setFrame_(NSMakeRect(PAD, y, 170, 20))
        content.addSubview_(lbl_steps)
        tf_steps = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 170, y, 60, 22))
        tf_steps.setStringValue_(str(self.settings.get("interplay_workspace_steps", 17)))
        content.addSubview_(tf_steps)
        controls["interplay_workspace_steps"] = tf_steps

        # ── Buttons ──
        target.setup(self, controls, window)

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 20, 100, 30))
        cancel_btn.setTitle_("Abbrechen")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(target)
        cancel_btn.setAction_(objc.selector(target.onCancel_, signature=b'v@:@'))
        content.addSubview_(cancel_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W - 120, 20, 100, 30))
        save_btn.setTitle_("Speichern")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r")
        save_btn.setTarget_(target)
        save_btn.setAction_(objc.selector(target.onSave_, signature=b'v@:@'))
        content.addSubview_(save_btn)

        global _config_refs
        _config_refs = [window, target, controls]
        window.makeKeyAndOrderFront_(None)
        window.center()

    # ── Hilfe & Info Callbacks ───────────────────────────────────────────
    
    @objc.python_method
    def _doc_path(self, filename):
        """Sucht eine Dokumentationsdatei neben der App oder neben dem Skript."""
        import sys as _sys
        candidates = []
        if getattr(_sys, 'frozen', False):
            # Neben PunchBuddy.app (im DMG-Ordner)
            app_dir = os.path.dirname(os.path.dirname(os.path.dirname(_sys.executable)))
            candidates.append(os.path.join(os.path.dirname(app_dir), filename))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), filename))
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    def _on_help_manual(self, _):
        import subprocess as _sp
        _doc = self._doc_path("BEDIENUNGSANLEITUNG.md")
        if _doc:
            try:
                _sp.Popen(["open", _doc]); return
            except Exception:
                pass
        rumps.alert(title=t("msg_manual_not_found_title"),
                    message=t("msg_manual_not_found_body"))

    def _on_help_docs(self, _):
        import subprocess as _sp
        _doc = self._doc_path("TECHNISCHE_DOKUMENTATION.md")
        if _doc:
            try:
                _sp.Popen(["open", _doc]); return
            except Exception:
                pass
        rumps.alert(title=t("msg_docs_not_found_title"),
                    message=t("msg_docs_not_found_body"))

    def _on_help_opensource(self, _):
        rumps.alert(
            title=t("msg_opensource_title"),
            message=t("msg_opensource_body")
        )





    # ── Kombiniertes Einstellungsfenster (Tab-basiert) ─────────────────────

    def _open_unified_settings_window(self, track_names):
        """Erstellt ein Tab-basiertes Fenster mit Spurenauswahl + Erweiterte Einstellungen."""
        if _close_existing_settings_window():
            return
        NSWindow          = AppKit.NSWindow
        NSView            = AppKit.NSView
        NSButton          = AppKit.NSButton
        NSTextField       = AppKit.NSTextField
        NSScrollView      = AppKit.NSScrollView
        NSTabView         = AppKit.NSTabView
        NSTabViewItem     = AppKit.NSTabViewItem
        NSFont            = AppKit.NSFont
        NSColor           = AppKit.NSColor
        NSMakeRect        = AppKit.NSMakeRect
        NSScreen          = AppKit.NSScreen
        NSOnState         = AppKit.NSOnState
        NSOffState        = AppKit.NSOffState
        NSBezelStyleRounded = AppKit.NSBezelStyleRounded

        WIN_W = 680
        WIN_H = 850
        PAD = 20
        BUTTON_H = 50

        screen = NSScreen.mainScreen().frame()
        xx = (screen.size.width - WIN_W) / 2
        yy = (screen.size.height - WIN_H) / 2

        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(xx, yy, WIN_W, WIN_H), style, AppKit.NSBackingStoreBuffered, False)
        window.setTitle_(t("title_settings_window"))
        window.setLevel_(3)

        # Fenster-Icon setzen
        _win_icon = _load_ns_icon()
        if _win_icon:
            _win_icon.setSize_(AppKit.NSMakeSize(32, 32))
            window.setRepresentedURL_(AppKit.NSURL.URLWithString_("punchbuddy://settings"))
            window.standardWindowButton_(AppKit.NSWindowDocumentIconButton).setImage_(_win_icon)

        content = window.contentView()
        content.setWantsLayer_(True)

        tab_view = NSTabView.alloc().initWithFrame_(
            NSMakeRect(10, BUTTON_H + 10, WIN_W - 20, WIN_H - BUTTON_H - 20))

        # ── TAB 1: SPUREN ────────────────────────────────────────────────
        tab1 = NSTabViewItem.alloc().initWithIdentifier_("Spuren")
        tab1.setLabel_(t("tab_tracks"))
        t1_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab1.setView_(t1_view)

        checkboxes = {}

        if track_names:
            rec_a_set  = set(self.settings.get("tracks", []))
            mon_a_set  = set(self.settings.get("monitor_tracks", []))
            rec_b_set  = set(self.settings.get("tracks_b", []))
            mon_b_set  = set(self.settings.get("monitor_tracks_b", []))
            export_set = set(self.settings.get("export_tracks", []))
            loud_set   = set(self.settings.get("loudness_tracks", []))
            play_set   = set(self.settings.get("play_monitor_tracks", []))

            ROW_H = 28; LABEL_W = 180; CB_W = 55; GAP = 10
            col_a_x      = LABEL_W + 5
            col_b_x      = col_a_x + CB_W + GAP
            col_export_x = col_b_x + CB_W + GAP + 10
            col_loud_x   = col_export_x + CB_W + GAP
            col_play_x   = col_loud_x + CB_W + GAP

            HEADER_H = 65
            scroll_h = t1_view.frame().size.height - HEADER_H

            # Column Headers
            for txt, cx, w in [(t("col_record_a"), col_a_x, CB_W), (t("col_record_b"), col_b_x, CB_W),
                                (t("col_export"), col_export_x, CB_W), (t("col_loudness"), col_loud_x, CB_W + 15),
                                (t("col_play_input"), col_play_x, CB_W + 20)]:
                lbl = NSTextField.labelWithString_(txt)
                lbl.setFrame_(NSMakeRect(cx, scroll_h + 15, w, 18))
                lbl.setFont_(NSFont.boldSystemFontOfSize_(11))
                lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
                t1_view.addSubview_(lbl)

            lbl_spur = NSTextField.labelWithString_(t("col_track"))
            lbl_spur.setFrame_(NSMakeRect(PAD, scroll_h + 15, LABEL_W, 18))
            lbl_spur.setFont_(NSFont.boldSystemFontOfSize_(11))
            t1_view.addSubview_(lbl_spur)

            # ScrollView
            scroll_view = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(0, 0, t1_view.frame().size.width, scroll_h))
            scroll_view.setHasVerticalScroller_(True)
            scroll_view.setAutohidesScrollers_(True)
            scroll_view.setBorderType_(AppKit.NSBezelBorder)

            doc_h = len(track_names) * ROW_H + PAD
            doc_view = NSView.alloc().initWithFrame_(
                NSMakeRect(0, 0, t1_view.frame().size.width - 20, doc_h))

            for i, name in enumerate(track_names):
                row_y = doc_h - (i + 1) * ROW_H
                lbl = NSTextField.labelWithString_(name)
                lbl.setFrame_(NSMakeRect(PAD, row_y + 4, LABEL_W - PAD, 20))
                lbl.setFont_(NSFont.systemFontOfSize_(12))
                doc_view.addSubview_(lbl)
                if i % 2 == 0:
                    stripe = AppKit.NSBox.alloc().initWithFrame_(
                        NSMakeRect(0, row_y, t1_view.frame().size.width, ROW_H))
                    stripe.setBoxType_(AppKit.NSBoxCustom)
                    stripe.setBorderType_(AppKit.NSNoBorder)
                    stripe.setFillColor_(NSColor.quaternaryLabelColor())
                    doc_view.addSubview_positioned_relativeTo_(stripe, AppKit.NSWindowBelow, lbl)
                cbs = {}
                for key, cx, active_set in [
                    ("rec_a",    col_a_x,     rec_a_set),
                    ("rec_b",    col_b_x,     rec_b_set),
                    ("export",   col_export_x, export_set),
                    ("loud",     col_loud_x,      loud_set),
                    ("play_mon", col_play_x,      play_set),
                ]:
                    cb = NSButton.alloc().initWithFrame_(NSMakeRect(cx + 15, row_y + 3, 22, 22))
                    cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                    cb.setTitle_("")
                    cb.setState_(NSOnState if name in active_set else NSOffState)
                    doc_view.addSubview_(cb)
                    cbs[key] = cb
                checkboxes[name] = cbs
            scroll_view.setDocumentView_(doc_view)
            t1_view.addSubview_(scroll_view)
        else:
            nl = NSTextField.labelWithString_(t("msg_no_tracks"))
            nl.setFrame_(NSMakeRect(PAD, t1_view.frame().size.height / 2, WIN_W - 40, 30))
            t1_view.addSubview_(nl)

        # ── TAB 2: EXPORT ────────────────────────────────────────────────
        tab2 = NSTabViewItem.alloc().initWithIdentifier_("Export")
        tab2.setLabel_(t("tab_export"))
        t2_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab2.setView_(t2_view)

        controls = {}
        t2_y = t2_view.frame().size.height - 35

        # ── Rubrik 1: Export Einstellungen Allgemein ──
        h2 = NSTextField.labelWithString_(t("lbl_export_settings_general"))
        h2.setFrame_(NSMakeRect(PAD, t2_y, 350, 20))
        h2.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h2)

        t2_y -= 28
        ltc = NSTextField.labelWithString_(t("lbl_start_tc"))
        ltc.setFrame_(NSMakeRect(PAD, t2_y, 220, 20)); t2_view.addSubview_(ltc)
        tf_tc = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 120, 22))
        tf_tc.setStringValue_(str(self.settings.get("export_start_tc", "10:00:00:00")))
        t2_view.addSubview_(tf_tc); controls["export_start_tc"] = tf_tc

        t2_y -= 25
        lvt = NSTextField.labelWithString_(t("lbl_video_track"))
        lvt.setFrame_(NSMakeRect(PAD, t2_y, 220, 20)); t2_view.addSubview_(lvt)
        tf_vt2 = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 120, 22))
        tf_vt2.setStringValue_(str(self.settings.get("video_track", "Video 1")))
        t2_view.addSubview_(tf_vt2); controls["video_track"] = tf_vt2

        t2_y -= 30
        cb_l = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, 20, 20))
        cb_l.setButtonType_(AppKit.NSButtonTypeSwitch); cb_l.setTitle_("")
        cb_l.setState_(NSOnState if self.settings.get("loudness_enabled", True) else NSOffState)
        t2_view.addSubview_(cb_l); controls["loudness_enabled"] = cb_l
        ll = NSTextField.labelWithString_(t("lbl_loudness_enable"))
        ll.setFrame_(NSMakeRect(PAD + 25, t2_y + 2, 300, 20)); t2_view.addSubview_(ll)

        t2_y -= 25
        ll2 = NSTextField.labelWithString_(t("lbl_target_lufs"))
        ll2.setFrame_(NSMakeRect(PAD + 25, t2_y, 170, 20)); t2_view.addSubview_(ll2)
        tf_lufs = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 80, 22))
        tf_lufs.setStringValue_(str(self.settings.get("target_lufs", -23.0)))
        t2_view.addSubview_(tf_lufs); controls["target_lufs"] = tf_lufs

        t2_y -= 25
        ll3 = NSTextField.labelWithString_(t("lbl_max_truepeak"))
        ll3.setFrame_(NSMakeRect(PAD + 25, t2_y, 170, 20)); t2_view.addSubview_(ll3)
        tf_tp = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 80, 22))
        tf_tp.setStringValue_(str(self.settings.get("max_truepeak", -3.0)))
        t2_view.addSubview_(tf_tp); controls["max_truepeak"] = tf_tp

        # ── Trennlinie ──
        t2_y -= 20
        sep2 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2 - 40, 1))
        sep2.setBoxType_(AppKit.NSBoxSeparator)
        t2_view.addSubview_(sep2)

        # ── Rubrik 2: Interplay Export Einstellungen ──
        t2_y -= 25
        h3 = NSTextField.labelWithString_(t("lbl_interplay_export_settings"))
        h3.setFrame_(NSMakeRect(PAD, t2_y, 350, 20))
        h3.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h3)

        t2_y -= 28
        lst = NSTextField.labelWithString_(t("lbl_workspace_steps"))
        lst.setFrame_(NSMakeRect(PAD, t2_y, 200, 20)); t2_view.addSubview_(lst)
        tf_st = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 60, 22))
        tf_st.setStringValue_(str(self.settings.get("interplay_workspace_steps", 17)))
        t2_view.addSubview_(tf_st); controls["interplay_workspace_steps"] = tf_st

        t2_y -= 28
        l_ekw = NSTextField.labelWithString_(t("lbl_error_keywords"))
        l_ekw.setFrame_(NSMakeRect(PAD, t2_y, 145, 20)); t2_view.addSubview_(l_ekw)
        tf_ekw = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, t2_y, 400, 22))
        tf_ekw.setStringValue_(self.settings.get("export_error_keywords",
            "error,fail,fehler,unsuccessful,could not,unable,problem,warning,aborted,abgebrochen"))
        tf_ekw.setToolTip_("Kommagetrennte Schlüsselwörter – bei Treffer wird der Export als fehlgeschlagen gewertet")
        t2_view.addSubview_(tf_ekw); controls["export_error_keywords"] = tf_ekw

        t2_y -= 25
        l_skw = NSTextField.labelWithString_(t("lbl_success_keywords"))
        l_skw.setFrame_(NSMakeRect(PAD, t2_y, 145, 20)); t2_view.addSubview_(l_skw)
        tf_skw = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, t2_y, 400, 22))
        tf_skw.setStringValue_(self.settings.get("export_success_keywords",
            "success,complete,finished,done,exported,erfolgreich,abgeschlossen,fertig"))
        tf_skw.setToolTip_("Kommagetrennte Schlüsselwörter – bei Treffer wird der Export als erfolgreich gewertet")
        t2_view.addSubview_(tf_skw); controls["export_success_keywords"] = tf_skw

        # ── Trennlinie ──
        t2_y -= 20
        sep3 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2 - 40, 1))
        sep3.setBoxType_(AppKit.NSBoxSeparator)
        t2_view.addSubview_(sep3)

        # ── Rubrik 4: Rename Sequence ──
        t2_y -= 25
        h4 = NSTextField.labelWithString_(t("lbl_rename_seq"))
        h4.setFrame_(NSMakeRect(PAD, t2_y, 420, 20))
        h4.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h4)

        t2_y -= 28
        cb_rn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, 20, 20))
        cb_rn.setButtonType_(AppKit.NSButtonTypeSwitch); cb_rn.setTitle_("")
        cb_rn.setState_(NSOnState if self.settings.get("interplay_rename_enabled", False) else NSOffState)
        t2_view.addSubview_(cb_rn); controls["interplay_rename_enabled"] = cb_rn
        l_rn = NSTextField.labelWithString_(t("lbl_rename_enable"))
        l_rn.setFrame_(NSMakeRect(PAD + 25, t2_y + 2, 350, 20)); t2_view.addSubview_(l_rn)

        t2_y -= 25
        l_ts2 = NSTextField.labelWithString_(t("lbl_rename_trim_start"))
        l_ts2.setFrame_(NSMakeRect(PAD + 25, t2_y, 200, 20)); t2_view.addSubview_(l_ts2)
        tf_ts = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 60, 22))
        tf_ts.setStringValue_(str(self.settings.get("interplay_rename_trim_start", 0)))
        t2_view.addSubview_(tf_ts); controls["interplay_rename_trim_start"] = tf_ts

        t2_y -= 25
        l_te2 = NSTextField.labelWithString_(t("lbl_rename_trim_end"))
        l_te2.setFrame_(NSMakeRect(PAD + 25, t2_y, 200, 20)); t2_view.addSubview_(l_te2)
        tf_te = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 60, 22))
        tf_te.setStringValue_(str(self.settings.get("interplay_rename_trim_end", 0)))
        t2_view.addSubview_(tf_te); controls["interplay_rename_trim_end"] = tf_te

        t2_y -= 25
        l_pf2 = NSTextField.labelWithString_(t("lbl_rename_prefix"))
        l_pf2.setFrame_(NSMakeRect(PAD + 25, t2_y, 150, 20)); t2_view.addSubview_(l_pf2)
        tf_pf = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 160, 22))
        tf_pf.setStringValue_(self.settings.get("interplay_rename_prefix", ""))
        t2_view.addSubview_(tf_pf); controls["interplay_rename_prefix"] = tf_pf

        t2_y -= 25
        l_sf2 = NSTextField.labelWithString_(t("lbl_rename_suffix"))
        l_sf2.setFrame_(NSMakeRect(PAD + 25, t2_y, 150, 20)); t2_view.addSubview_(l_sf2)
        tf_sf = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 160, 22))
        tf_sf.setStringValue_(self.settings.get("interplay_rename_suffix", ""))
        t2_view.addSubview_(tf_sf); controls["interplay_rename_suffix"] = tf_sf

        # ── Trennlinie ──
        t2_y -= 25
        sep_lang = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2 - 40, 1))
        sep_lang.setBoxType_(AppKit.NSBoxSeparator)
        t2_view.addSubview_(sep_lang)

        # ── Sprache / Language ──
        t2_y -= 28
        h_lang = NSTextField.labelWithString_(t("language"))
        h_lang.setFrame_(NSMakeRect(PAD, t2_y + 2, 150, 20))
        h_lang.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h_lang)

        t2_y -= 30
        lang_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 25, t2_y, 220, 24))
        lang_popup.removeAllItems()
        langs_ui = [
            ("Deutsch", "de"),
            ("English", "en"),
            ("Français", "fr"),
            ("Español", "es"),
            ("Português", "pt")
        ]
        saved_lang = self.settings.get("language", "de")
        _selected_lang_idx = 0
        for _ii, (_dn, _code) in enumerate(langs_ui):
            lang_popup.addItemWithTitle_(_dn)
            if _code == saved_lang:
                _selected_lang_idx = _ii
        lang_popup.selectItemAtIndex_(_selected_lang_idx)
        t2_view.addSubview_(lang_popup)
        controls["language"] = lang_popup

        # ── TAB 3: IMPORT-EINSTELLUNGEN ──────────────────────────────────
        tab3 = NSTabViewItem.alloc().initWithIdentifier_("Import")
        tab3.setLabel_(t("tab_import"))
        t3_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab3.setView_(t3_view)

        t3_y = t3_view.frame().size.height - 35

        h_imp = NSTextField.labelWithString_(t("lbl_import_settings_header"))
        h_imp.setFrame_(NSMakeRect(PAD, t3_y, 400, 20))
        h_imp.setFont_(NSFont.boldSystemFontOfSize_(13))
        t3_view.addSubview_(h_imp)

        t3_y -= 35
        cb_close = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t3_y, 20, 20))
        cb_close.setButtonType_(AppKit.NSButtonTypeSwitch); cb_close.setTitle_("")
        cb_close.setState_(NSOnState if self.settings.get("import_close_session", True) else NSOffState)
        t3_view.addSubview_(cb_close); controls["import_close_session"] = cb_close
        l_close = NSTextField.labelWithString_(t("lbl_import_close_session"))
        l_close.setFrame_(NSMakeRect(PAD + 25, t3_y + 2, 450, 20)); t3_view.addSubview_(l_close)

        t3_y -= 30
        l_desc = NSTextField.labelWithString_(t("lbl_import_close_desc"))
        l_desc.setFrame_(NSMakeRect(PAD + 25, t3_y - 30, 450, 50))
        l_desc.setFont_(NSFont.systemFontOfSize_(11))
        l_desc.setTextColor_(NSColor.secondaryLabelColor())
        l_desc.setBezeled_(False); l_desc.setDrawsBackground_(False)
        l_desc.setEditable_(False); l_desc.setSelectable_(False)
        t3_view.addSubview_(l_desc)

        # ── TAB 4: WEBTRIGGER ─────────────────────────────────────────────
        tab4 = NSTabViewItem.alloc().initWithIdentifier_("Webtrigger")
        tab4.setLabel_(t("tab_webtrigger"))
        t4_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab4.setView_(t4_view)

        t4_y = t4_view.frame().size.height - 35

        h_wt = NSTextField.labelWithString_(t("lbl_http_server_config"))
        h_wt.setFrame_(NSMakeRect(PAD, t4_y, 400, 20))
        h_wt.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_wt)

        t4_y -= 30
        l_port = NSTextField.labelWithString_(t("lbl_http_port"))
        l_port.setFrame_(NSMakeRect(PAD, t4_y, 40, 22)); t4_view.addSubview_(l_port)
        tf_port = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 45, t4_y, 70, 22))
        tf_port.setStringValue_(str(self.settings.get("http_port", 8899)))
        t4_view.addSubview_(tf_port); controls["http_port"] = tf_port

        l_port_info = NSTextField.labelWithString_(t("lbl_http_restart_info"))
        l_port_info.setFrame_(NSMakeRect(PAD + 125, t4_y + 2, 280, 18))
        l_port_info.setFont_(NSFont.systemFontOfSize_(11))
        l_port_info.setTextColor_(NSColor.secondaryLabelColor())
        t4_view.addSubview_(l_port_info)

        # ── Netzwerk-Interface ──────────────────────────────────────────
        t4_y -= 30
        sep_iface = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t4_y, WIN_W - PAD * 2 - 40, 1))
        sep_iface.setBoxType_(AppKit.NSBoxSeparator)
        t4_view.addSubview_(sep_iface)

        t4_y -= 28
        h_iface = NSTextField.labelWithString_(t("lbl_network_interface"))
        h_iface.setFrame_(NSMakeRect(PAD, t4_y, 300, 20))
        h_iface.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_iface)

        t4_y -= 30
        l_iface = NSTextField.labelWithString_(t("lbl_interface"))
        l_iface.setFrame_(NSMakeRect(PAD, t4_y + 2, 80, 20))
        l_iface.setFont_(NSFont.systemFontOfSize_(12))
        t4_view.addSubview_(l_iface)

        _iface_list = _get_network_interfaces()
        saved_host = self.settings.get("http_bind_host", "127.0.0.1")
        iface_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 85, t4_y, 260, 24))
        iface_popup.removeAllItems()
        _selected_iface_idx = 0
        for _ii, (_dn, _ip) in enumerate(_iface_list):
            iface_popup.addItemWithTitle_(_dn)
            if _ip == saved_host:
                _selected_iface_idx = _ii
        iface_popup.selectItemAtIndex_(_selected_iface_idx)
        t4_view.addSubview_(iface_popup)
        controls["http_bind_host"] = iface_popup

        l_iface_note = NSTextField.labelWithString_(t("lbl_http_restart_info"))
        l_iface_note.setFrame_(NSMakeRect(PAD + 355, t4_y + 2, 240, 18))
        l_iface_note.setFont_(NSFont.systemFontOfSize_(11))
        l_iface_note.setTextColor_(NSColor.secondaryLabelColor())
        t4_view.addSubview_(l_iface_note)

        # ── Token (Webtrigger-Authentifizierung) ────────────────────────
        t4_y -= 30
        l_token = NSTextField.labelWithString_(t("lbl_webtrigger_token"))
        l_token.setFrame_(NSMakeRect(PAD, t4_y + 2, 80, 20))
        l_token.setFont_(NSFont.systemFontOfSize_(12))
        t4_view.addSubview_(l_token)

        tf_token = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 85, t4_y, 260, 22))
        tf_token.setStringValue_(self.settings.get("webtrigger_token", "") or "")
        tf_token.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
        t4_view.addSubview_(tf_token)
        controls["webtrigger_token"] = tf_token

        btn_gen_token = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 355, t4_y, 90, 22))
        btn_gen_token.setTitle_(t("btn_generate_token"))
        btn_gen_token.setBezelStyle_(NSBezelStyleRounded)
        btn_gen_token.setFont_(NSFont.systemFontOfSize_(11))
        t4_view.addSubview_(btn_gen_token)

        btn_clear_token = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 450, t4_y, 90, 22))
        btn_clear_token.setTitle_(t("btn_clear_token"))
        btn_clear_token.setBezelStyle_(NSBezelStyleRounded)
        btn_clear_token.setFont_(NSFont.systemFontOfSize_(11))
        t4_view.addSubview_(btn_clear_token)

        t4_y -= 20
        l_token_hint = NSTextField.labelWithString_(t("lbl_webtrigger_token_hint"))
        l_token_hint.setFrame_(NSMakeRect(PAD + 85, t4_y, 460, 18))
        l_token_hint.setFont_(NSFont.systemFontOfSize_(11))
        l_token_hint.setTextColor_(NSColor.secondaryLabelColor())
        t4_view.addSubview_(l_token_hint)
        self._token_field = tf_token
        self._token_gen_button = btn_gen_token
        self._token_clear_button = btn_clear_token

        t4_y -= 35
        h_urls = NSTextField.labelWithString_(t("lbl_trigger_urls"))
        h_urls.setFrame_(NSMakeRect(PAD, t4_y, 400, 20))
        h_urls.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_urls)

        port = getattr(self, '_http_port', self.settings.get("http_port", 8899))
        display_host = getattr(self, '_http_bind_host_display',
                               self.settings.get("http_bind_host", "127.0.0.1"))
        _tok = self.settings.get("webtrigger_token", "") or ""
        _token_qs = f"?token={_tok}" if _tok else ""

        self._webtrigger_urls = []

        _core_entries = [
            ("/trigger",           "Record A"),
            ("/trigger2",          "Record B"),
            ("/play",              "Play Input/Stop (Toggle)"),
            ("/play_custom",       "Play Custom (KH2/ST Abh)"),
            ("/start",             "Cursor → Start-Timecode"),
            ("/export_wav",        "WAV Export"),
            ("/export_aaf",        "AAF Export"),
            ("/export_interplay",  "Interplay Export"),
            ("/import",            "Interplay Import"),
        ]

        for path, label in _core_entries:
            t4_y -= 28
            url = f"http://{display_host}:{port}{path}{_token_qs}"
            self._webtrigger_urls.append((url, label))
            tag_idx = len(self._webtrigger_urls) - 1

            l_url = NSTextField.labelWithString_(f"{label}:")
            l_url.setFrame_(NSMakeRect(PAD, t4_y + 2, 140, 18))
            l_url.setFont_(NSFont.systemFontOfSize_(12))
            t4_view.addSubview_(l_url)

            tf_url = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 145, t4_y, 405, 22))
            tf_url.setStringValue_(url)
            tf_url.setEditable_(False); tf_url.setSelectable_(True)
            tf_url.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
            t4_view.addSubview_(tf_url)

            copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 560, t4_y, 70, 22))
            copy_btn.setTitle_(t("btn_copy"))
            copy_btn.setBezelStyle_(NSBezelStyleRounded)
            copy_btn.setFont_(NSFont.systemFontOfSize_(11))
            copy_btn.setTag_(tag_idx)
            t4_view.addSubview_(copy_btn)

        # ── Preset Webtrigger URLs ────────────────────────────────────────
        t4_y -= 15
        sep_presets = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t4_y, WIN_W - PAD * 2 - 40, 1))
        sep_presets.setBoxType_(AppKit.NSBoxSeparator)
        t4_view.addSubview_(sep_presets)

        t4_y -= 25
        h_preset_urls = NSTextField.labelWithString_(t("lbl_preset_trigger_urls"))
        h_preset_urls.setFrame_(NSMakeRect(PAD, t4_y, 400, 20))
        h_preset_urls.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_preset_urls)

        presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        for idx in range(8):
            preset_name = f"Preset {idx+1}"
            if idx < len(presets):
                preset_name = presets[idx].get("name", preset_name)

            path = f"/preset/{idx+1}"
            url = f"http://{display_host}:{port}{path}{_token_qs}"
            self._webtrigger_urls.append((url, preset_name))
            tag_idx = len(self._webtrigger_urls) - 1

            t4_y -= 28
            l_url = NSTextField.labelWithString_(f"{preset_name}:")
            l_url.setFrame_(NSMakeRect(PAD, t4_y + 2, 140, 18))
            l_url.setFont_(NSFont.systemFontOfSize_(12))
            t4_view.addSubview_(l_url)

            tf_url = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 145, t4_y, 405, 22))
            tf_url.setStringValue_(url)
            tf_url.setEditable_(False); tf_url.setSelectable_(True)
            tf_url.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
            t4_view.addSubview_(tf_url)

            copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 560, t4_y, 70, 22))
            copy_btn.setTitle_(t("btn_copy"))
            copy_btn.setBezelStyle_(NSBezelStyleRounded)
            copy_btn.setFont_(NSFont.systemFontOfSize_(11))
            copy_btn.setTag_(tag_idx)
            t4_view.addSubview_(copy_btn)

        self._webtrigger_buttons = []

        _wt_target = _WebtriggerCopyTarget.alloc().init()
        _wt_target._app = self
        _wt_target._token_field = getattr(self, '_token_field', None)
        for sv in t4_view.subviews():
            if isinstance(sv, NSButton) and sv.title() == t("btn_copy"):
                sv.setTarget_(_wt_target)
                sv.setAction_(objc.selector(_wt_target.onCopy_, signature=b'v@:@'))
                self._webtrigger_buttons.append(sv)
        # Token-Buttons verdrahten
        if getattr(self, '_token_gen_button', None) is not None:
            self._token_gen_button.setTarget_(_wt_target)
            self._token_gen_button.setAction_(
                objc.selector(_wt_target.onGenerateToken_, signature=b'v@:@'))
        if getattr(self, '_token_clear_button', None) is not None:
            self._token_clear_button.setTarget_(_wt_target)
            self._token_clear_button.setAction_(
                objc.selector(_wt_target.onClearToken_, signature=b'v@:@'))
        self._wt_target = _wt_target

        # ── TAB 5: PRESETS ──────────────────────────────────────────────────
        tab5 = NSTabViewItem.alloc().initWithIdentifier_("Presets")
        tab5.setLabel_(t("tab_presets"))
        t5_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab5.setView_(t5_view)

        t5_y = t5_view.frame().size.height - 35

        h_pr = NSTextField.labelWithString_(t("tab_presets"))
        h_pr.setFrame_(NSMakeRect(PAD, t5_y, 400, 20))
        h_pr.setFont_(NSFont.boldSystemFontOfSize_(13))
        t5_view.addSubview_(h_pr)

        t5_y -= 12
        sep_pr0 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t5_y, WIN_W - PAD * 2 - 40, 1))
        sep_pr0.setBoxType_(AppKit.NSBoxSeparator)
        t5_view.addSubview_(sep_pr0)

        t5_y -= 35
        l_psel = NSTextField.labelWithString_(t("preset_select"))
        l_psel.setFrame_(NSMakeRect(PAD, t5_y + 3, 130, 20))
        l_psel.setFont_(NSFont.systemFontOfSize_(12))
        t5_view.addSubview_(l_psel)

        presets_tab_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 135, t5_y, 220, 24))
        presets_tab_popup.removeAllItems()
        _cur_presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        for _pp in _cur_presets:
            presets_tab_popup.addItemWithTitle_(_pp.get("name", "Preset"))
        t5_view.addSubview_(presets_tab_popup)

        t5_y -= 40
        btn_load_pr  = NSButton.alloc().initWithFrame_(NSMakeRect(PAD,        t5_y, 75, 28))
        btn_load_pr.setTitle_(t("btn_load"));      btn_load_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_load_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_load_pr)

        btn_save_pr  = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 85,   t5_y, 90, 28))
        btn_save_pr.setTitle_(t("save"));  btn_save_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_save_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_save_pr)

        btn_rename_pr = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 185, t5_y, 105, 28))
        btn_rename_pr.setTitle_(t("btn_rename")); btn_rename_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_rename_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_rename_pr)

        btn_new_pr   = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 300,  t5_y, 60, 28))
        btn_new_pr.setTitle_(t("btn_new"));         btn_new_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_new_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_new_pr)

        btn_del_pr   = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 370,  t5_y, 80, 28))
        btn_del_pr.setTitle_(t("btn_delete"));     btn_del_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_del_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_del_pr)

        t5_y -= 35
        sep_pr2 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t5_y, WIN_W - PAD * 2 - 40, 1))
        sep_pr2.setBoxType_(AppKit.NSBoxSeparator)
        t5_view.addSubview_(sep_pr2)

        t5_y -= 28
        h_pr_info = NSTextField.labelWithString_(t("preset_contains"))
        h_pr_info.setFrame_(NSMakeRect(PAD, t5_y, 300, 20))
        h_pr_info.setFont_(NSFont.boldSystemFontOfSize_(12))
        t5_view.addSubview_(h_pr_info)

        _info_text = (
            t("preset_info_tracks") +
            t("preset_info_mon") +
            t("preset_info_export") +
            t("preset_info_keywords") +
            t("preset_info_import") +
            t("preset_info_webtrigger")
        )
        t5_y -= 95
        l_info = NSTextField.labelWithString_(_info_text)
        l_info.setFrame_(NSMakeRect(PAD + 15, t5_y, WIN_W - PAD * 2 - 55, 90))
        l_info.setFont_(NSFont.systemFontOfSize_(11))
        l_info.setTextColor_(NSColor.secondaryLabelColor())
        l_info.setBezeled_(False); l_info.setDrawsBackground_(False)
        l_info.setEditable_(False); l_info.setSelectable_(False)
        t5_view.addSubview_(l_info)

        # Tabs hinzufuegen
        # ── TAB 6: MONITORING ────────────────────────────────────────────
        tab6 = NSTabViewItem.alloc().initWithIdentifier_("Monitoring")
        tab6.setLabel_("Monitoring")
        t6_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab6.setView_(t6_view)

        t6_y = t6_view.frame().size.height - 20

        lbl_mon_hdr = NSTextField.labelWithString_("Play Custom – Mute-Zustände")
        lbl_mon_hdr.setFrame_(NSMakeRect(PAD, t6_y, 400, 20))
        lbl_mon_hdr.setFont_(NSFont.boldSystemFontOfSize_(13))
        t6_view.addSubview_(lbl_mon_hdr)

        t6_y -= 20
        lbl_mon_desc = NSTextField.labelWithString_(
            "Definiert welche Spuren beim Play Custom gemutet oder entmutet werden.")
        lbl_mon_desc.setFrame_(NSMakeRect(PAD, t6_y, WIN_W - PAD * 2 - 40, 18))
        lbl_mon_desc.setFont_(NSFont.systemFontOfSize_(11))
        lbl_mon_desc.setTextColor_(NSColor.secondaryLabelColor())
        t6_view.addSubview_(lbl_mon_desc)

        t6_y -= 30

        # Spalten-Header
        for txt, x, w in [("Spur", PAD, 180), ("Mute bei Play-Start", PAD + 190, 140), ("Mute bei Stop", PAD + 340, 120)]:
            h = NSTextField.labelWithString_(txt)
            h.setFrame_(NSMakeRect(x, t6_y, w, 18))
            h.setFont_(NSFont.boldSystemFontOfSize_(11))
            t6_view.addSubview_(h)

        track_options = [""] + (track_names or [])

        for ch_idx in range(1, 3):
            t6_y -= 35
            lbl_ch = NSTextField.labelWithString_(f"Kanal {ch_idx}:")
            lbl_ch.setFrame_(NSMakeRect(PAD, t6_y + 3, 55, 18))
            t6_view.addSubview_(lbl_ch)

            track_key    = f"play_custom_ch{ch_idx}_track"
            mute_s_key   = f"play_custom_ch{ch_idx}_mute_start"
            mute_end_key = f"play_custom_ch{ch_idx}_mute_stop"

            cur_track     = self.settings.get(track_key, DEFAULT_SETTINGS.get(track_key, ""))
            cur_mute_s    = self.settings.get(mute_s_key, DEFAULT_SETTINGS.get(mute_s_key, True))
            cur_mute_end  = self.settings.get(mute_end_key, DEFAULT_SETTINGS.get(mute_end_key, False))

            popup = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(PAD + 60, t6_y, 120, 24))
            popup.removeAllItems()
            for opt in track_options:
                popup.addItemWithTitle_(opt)
            if cur_track in track_options:
                popup.selectItemWithTitle_(cur_track)
            t6_view.addSubview_(popup)
            controls[track_key] = popup

            cb_start = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 190 + 40, t6_y + 2, 20, 20))
            cb_start.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb_start.setTitle_("")
            cb_start.setState_(AppKit.NSOnState if cur_mute_s else AppKit.NSOffState)
            t6_view.addSubview_(cb_start)
            controls[mute_s_key] = cb_start

            cb_stop = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 340 + 20, t6_y + 2, 20, 20))
            cb_stop.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb_stop.setTitle_("")
            cb_stop.setState_(AppKit.NSOnState if cur_mute_end else AppKit.NSOffState)
            t6_view.addSubview_(cb_stop)
            controls[mute_end_key] = cb_stop

        tab_view.addTabViewItem_(tab1)
        tab_view.addTabViewItem_(tab2)
        tab_view.addTabViewItem_(tab3)
        tab_view.addTabViewItem_(tab4)
        tab_view.addTabViewItem_(tab5)
        tab_view.addTabViewItem_(tab6)
        content.addSubview_(tab_view)

        # Target
        target = _UnifiedSettingsTarget.alloc().init()
        target.setup(self, window, checkboxes, track_names, controls, presets_tab_popup,
                     iface_list=_iface_list)
        btn_load_pr.setTarget_(target)
        btn_load_pr.setAction_(objc.selector(target.onLoadPreset_, signature=b'v@:@'))
        btn_save_pr.setTarget_(target)
        btn_save_pr.setAction_(objc.selector(target.onSavePreset_, signature=b'v@:@'))
        btn_rename_pr.setTarget_(target)
        btn_rename_pr.setAction_(objc.selector(target.onRenamePreset_, signature=b'v@:@'))
        btn_new_pr.setTarget_(target)
        btn_new_pr.setAction_(objc.selector(target.onNewPreset_, signature=b'v@:@'))
        btn_del_pr.setTarget_(target)
        btn_del_pr.setAction_(objc.selector(target.onDeletePreset_, signature=b'v@:@'))

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 10, 100, 30))
        cancel_btn.setTitle_(t("cancel")); cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(target)
        cancel_btn.setAction_(objc.selector(target.onCancel_, signature=b'v@:@'))
        content.addSubview_(cancel_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W - 120, 10, 100, 30))
        save_btn.setTitle_(t("save")); save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r"); save_btn.setTarget_(target)
        save_btn.setAction_(objc.selector(target.onSave_, signature=b'v@:@'))
        content.addSubview_(save_btn)

        global _config_refs
        _config_refs = [window, target, controls, checkboxes, tab_view,
                        presets_tab_popup, btn_load_pr, btn_save_pr, btn_rename_pr,
                        btn_new_pr, btn_del_pr, iface_popup, lang_popup]
        window.makeKeyAndOrderFront_(None)
        window.center()


    # ── Spuren-Konfiguration (natives Fenster) ────────────────────────────

    def _on_configure_tracks(self, _):
        """Liest Spuren aus Pro Tools via PTSL und öffnet Konfigurationsfenster."""
        _ensure_dock_icon()
        if not APPKIT_OK:
            rumps.alert("Fehler", "AppKit nicht verfügbar – natives Fenster kann nicht geöffnet werden.")
            return

        # Spuren aus Pro Tools lesen
        try:
            engine = _get_engine()
            if engine is None:
                rumps.alert("Fehler", "PTSL Engine nicht verfügbar.")
                return
            ok, all_tracks = _ptsl_call(engine.track_list, label="ConfigTrackList", timeout=5.0)
            if not ok or not all_tracks:
                rumps.alert("Fehler", "Konnte Spuren nicht aus Pro Tools lesen:\nTimeout")
                return
            track_names = [t.name for t in all_tracks]
        except Exception as e:
            rumps.alert("Fehler", f"Konnte Spuren nicht aus Pro Tools lesen:\n{e}")
            return

        if not track_names:
            rumps.alert("Keine Spuren", "Keine Spuren in der aktuellen Pro Tools Session gefunden.")
            return

        logging.info(f"Spuren aus Pro Tools gelesen: {track_names}")
        try:
            self._open_config_window(track_names)
        except Exception as e:
            logging.error(f"Fehler beim Oeffnen des Konfigurationsfensters: {e}", exc_info=True)
            rumps.alert("Fehler", f"Fenster konnte nicht geoeffnet werden:\n{e}")

    def _open_config_window(self, track_names):
        """Erstellt natives macOS Fenster mit Checkboxen für jede Spur."""
        if _close_existing_settings_window():
            return
        NSWindow          = AppKit.NSWindow
        NSView            = AppKit.NSView
        NSButton          = AppKit.NSButton
        NSTextField       = AppKit.NSTextField
        NSScrollView      = AppKit.NSScrollView
        NSFont            = AppKit.NSFont
        NSColor           = AppKit.NSColor
        NSMakeRect        = AppKit.NSMakeRect
        NSScreen          = AppKit.NSScreen
        NSOnState         = AppKit.NSOnState
        NSOffState        = AppKit.NSOffState
        NSBezelStyleRounded = AppKit.NSBezelStyleRounded

        # Aktuelle Settings laden
        rec_a_set = set(self.settings.get("tracks", []))
        mon_a_set = set(self.settings.get("monitor_tracks", []))
        rec_b_set = set(self.settings.get("tracks_b", []))
        mon_b_set = set(self.settings.get("monitor_tracks_b", []))
        export_set = set(self.settings.get("export_tracks", []))
        loud_set = set(self.settings.get("loudness_tracks", []))
        play_set = set(self.settings.get("play_monitor_tracks", []))

        # Layout-Konstanten
        ROW_H      = 28
        PAD        = 16
        LABEL_W    = 180
        CB_W       = 55
        GAP        = 20    # Abstand zwischen Trigger-Gruppen
        EXPORT_GAP = 20    # Abstand vor Export-Spalte
        LOUD_GAP   = 5     # Abstand vor Loud-Spalte
        PLAY_GAP   = 5     # Abstand vor Play-Spalte
        HEADER_H   = 60
        BUTTON_H   = 50
        WIN_W      = LABEL_W + CB_W * 4 + GAP + CB_W + EXPORT_GAP + CB_W + LOUD_GAP + CB_W + PLAY_GAP + PAD * 2 + 40

        scroll_h   = min(len(track_names) * ROW_H + PAD, 420)
        WIN_H      = HEADER_H + scroll_h + BUTTON_H + PAD

        # Fenster erstellen
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width - WIN_W) / 2
        y = (screen.size.height - WIN_H) / 2

        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, WIN_W, WIN_H),
            style,
            AppKit.NSBackingStoreBuffered,
            False
        )
        window.setTitle_("Einstellungen PunchBuddy – Spuren")
        window.setLevel_(3)  # Floating über anderen Fenstern

        # Fenster-Icon setzen
        _win_icon = _load_ns_icon()
        if _win_icon:
            _win_icon.setSize_(AppKit.NSMakeSize(32, 32))
            window.setRepresentedURL_(AppKit.NSURL.URLWithString_("punchbuddy://config"))
            window.standardWindowButton_(AppKit.NSWindowDocumentIconButton).setImage_(_win_icon)

        content = window.contentView()
        content.setWantsLayer_(True)

        # ── Header-Labels ─────────────────────────────────────────────────
        col_a_x = LABEL_W + PAD + 10
        col_b_x = col_a_x + CB_W * 2 + GAP
        col_export_x = col_b_x + CB_W * 2 + EXPORT_GAP
        col_loud_x   = col_export_x + CB_W + LOUD_GAP
        col_play_x   = col_loud_x + CB_W + PLAY_GAP

        # "Record A" Header
        lbl_a = NSTextField.labelWithString_("Record A")
        lbl_a.setFrame_(NSMakeRect(col_a_x, WIN_H - 30, CB_W * 2, 18))
        lbl_a.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_a.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_a)

        # "Record B" Header
        lbl_b = NSTextField.labelWithString_("Record B")
        lbl_b.setFrame_(NSMakeRect(col_b_x, WIN_H - 30, CB_W * 2, 18))
        lbl_b.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_b.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_b)

        # "Export" Header
        lbl_export = NSTextField.labelWithString_("Export")
        lbl_export.setFrame_(NSMakeRect(col_export_x, WIN_H - 30, CB_W, 18))
        lbl_export.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_export.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_export)

        # Sub-Header: Rec (rot) / Mon (grün) – farbige Labels statt dynamischer Hintergründe
        for base_x in [col_a_x, col_b_x]:
            sub_rec = NSTextField.labelWithString_("Rec")
            sub_rec.setFrame_(NSMakeRect(base_x, WIN_H - 48, CB_W, 16))
            sub_rec.setFont_(NSFont.boldSystemFontOfSize_(10))
            sub_rec.setTextColor_(NSColor.systemRedColor())
            sub_rec.setAlignment_(AppKit.NSTextAlignmentCenter)
            content.addSubview_(sub_rec)

            sub_mon = NSTextField.labelWithString_("Mon")
            sub_mon.setFrame_(NSMakeRect(base_x + CB_W, WIN_H - 48, CB_W, 16))
            sub_mon.setFont_(NSFont.boldSystemFontOfSize_(10))
            sub_mon.setTextColor_(NSColor.systemGreenColor())
            sub_mon.setAlignment_(AppKit.NSTextAlignmentCenter)
            content.addSubview_(sub_mon)

        # Export Sub-Header (blau)
        sub_export = NSTextField.labelWithString_("✔")
        sub_export.setFrame_(NSMakeRect(col_export_x, WIN_H - 48, CB_W, 16))
        sub_export.setFont_(NSFont.systemFontOfSize_(10))
        sub_export.setTextColor_(NSColor.systemBlueColor())
        sub_export.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(sub_export)

        # "Loudness" Header (orange)
        lbl_loud = NSTextField.labelWithString_("Loudness")
        lbl_loud.setFrame_(NSMakeRect(col_loud_x, WIN_H - 30, CB_W, 18))
        lbl_loud.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_loud.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_loud)

        sub_loud = NSTextField.labelWithString_("✔")
        sub_loud.setFrame_(NSMakeRect(col_loud_x, WIN_H - 48, CB_W, 16))
        sub_loud.setFont_(NSFont.systemFontOfSize_(10))
        sub_loud.setTextColor_(NSColor.systemOrangeColor())
        sub_loud.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(sub_loud)

        # "Play Input" Header
        lbl_play = NSTextField.labelWithString_("Play Input")
        lbl_play.setFrame_(NSMakeRect(col_play_x, WIN_H - 30, CB_W, 18))
        lbl_play.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_play.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_play)

        sub_play = NSTextField.labelWithString_("Mon")
        sub_play.setFrame_(NSMakeRect(col_play_x, WIN_H - 48, CB_W, 16))
        sub_play.setFont_(NSFont.boldSystemFontOfSize_(10))
        sub_play.setTextColor_(NSColor.systemGreenColor())
        sub_play.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(sub_play)

        # "Spur" Header
        lbl_spur = NSTextField.labelWithString_("Spur")
        lbl_spur.setFrame_(NSMakeRect(PAD, WIN_H - 30, LABEL_W, 18))
        lbl_spur.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl_spur)

        # ── ScrollView mit Track-Zeilen ───────────────────────────────────
        PRESET_H = 35  # Platz fuer Preset-Leiste
        scroll_frame = NSMakeRect(0, BUTTON_H, WIN_W, scroll_h)
        scroll_view = NSScrollView.alloc().initWithFrame_(scroll_frame)
        scroll_view.setHasVerticalScroller_(True)
        scroll_view.setAutohidesScrollers_(True)
        scroll_view.setBorderType_(AppKit.NSBezelBorder)

        doc_h = len(track_names) * ROW_H + PAD
        doc_view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIN_W - 20, doc_h)
        )

        # Wir verwenden _ConfigButtonTarget als ObjC Action-Target
        target = _ConfigButtonTarget.alloc().init()
        
        checkboxes = {}  # {track_name: {rec_a, mon_a, rec_b, mon_b, export}}

        for i, name in enumerate(track_names):
            # In AppKit: y=0 ist unten, wir zaehlen von oben
            row_y = doc_h - (i + 1) * ROW_H

            # Track-Name Label
            lbl = NSTextField.labelWithString_(name)
            lbl.setFrame_(NSMakeRect(PAD, row_y + 4, LABEL_W - PAD, 20))
            lbl.setFont_(NSFont.systemFontOfSize_(12))
            doc_view.addSubview_(lbl)

            # Zebra-Streifen (abwechselnd) - NSBox statt layer/CGColor
            if i % 2 == 0:
                stripe = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(0, row_y, WIN_W - 20, ROW_H))
                stripe.setBoxType_(AppKit.NSBoxCustom)
                stripe.setBorderType_(AppKit.NSNoBorder)
                stripe.setFillColor_(NSColor.quaternaryLabelColor())
                doc_view.addSubview_positioned_relativeTo_(stripe, AppKit.NSWindowBelow, lbl)

            cbs = {}
            col_positions = [
                ("rec_a",    col_a_x,          rec_a_set),
                ("mon_a",    col_a_x + CB_W,   mon_a_set),
                ("rec_b",    col_b_x,          rec_b_set),
                ("mon_b",    col_b_x + CB_W,   mon_b_set),
                ("export",   col_export_x,      export_set),
                ("loud",     col_loud_x,        loud_set),
                ("play_mon", col_play_x,        play_set),
            ]

            for key, cx, active_set in col_positions:
                cb = NSButton.alloc().initWithFrame_(NSMakeRect(cx + 15, row_y + 3, 22, 22))
                cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                cb.setTitle_("")
                is_on = name in active_set
                cb.setState_(NSOnState if is_on else NSOffState)
                cb.setIdentifier_(key)
                doc_view.addSubview_(cb)
                cbs[key] = cb

            checkboxes[name] = cbs

        scroll_view.setDocumentView_(doc_view)
        content.addSubview_(scroll_view)

        # ── Preset-Leiste (unterhalb der Buttons, ueber Scroll) ──────────
        preset_y = BUTTON_H + scroll_h + 2
        # Header-Bereich bleibt bestehen, Presets kommen zwischen Header und Scroll

        # Stattdessen: Presets am unteren Rand, zwischen ScrollView und Buttons
        # Verschiebe ScrollView nach oben und fuege Preset-Leiste darunter ein
        # → Einfacher: Preset-Leiste ins Fenster als eigene Zeile einbauen
        # Die Preset-Leiste kommt UNTERHALB der ScrollView, OBERHALB der Buttons.
        
        # Fenster vergroessern fuer Presets
        new_win_h = WIN_H + PRESET_H
        window.setFrame_display_(NSMakeRect(
            window.frame().origin.x, window.frame().origin.y - PRESET_H,
            WIN_W, new_win_h), True)
        
        # ScrollView nach oben verschieben
        sv_frame = scroll_view.frame()
        scroll_view.setFrame_(NSMakeRect(sv_frame.origin.x, sv_frame.origin.y + PRESET_H,
                                          sv_frame.size.width, sv_frame.size.height))
        
        # Header-Elemente nach oben verschieben
        for subview in list(content.subviews()):
            if subview != scroll_view:
                f = subview.frame()
                if f.origin.y > BUTTON_H + scroll_h - 5:  # Header-Bereich
                    subview.setFrame_(NSMakeRect(f.origin.x, f.origin.y + PRESET_H,
                                                  f.size.width, f.size.height))

        # Preset-Leiste zeichnen
        preset_base_y = BUTTON_H + 4
        presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])

        lbl_preset = NSTextField.labelWithString_("Presets:")
        lbl_preset.setFrame_(NSMakeRect(PAD, preset_base_y + 5, 55, 20))
        lbl_preset.setFont_(NSFont.boldSystemFontOfSize_(11))
        content.addSubview_(lbl_preset)

        # Popup-Button fuer Preset-Auswahl
        preset_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(PAD + 55, preset_base_y + 3, 180, 24))
        preset_popup.removeAllItems()
        for p in presets:
            preset_popup.addItemWithTitle_(p.get("name", "Preset"))
        content.addSubview_(preset_popup)

        # Laden-Button
        load_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 240, preset_base_y + 3, 65, 24))
        load_btn.setTitle_("Laden")
        load_btn.setBezelStyle_(NSBezelStyleRounded)
        load_btn.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(load_btn)

        # Speichern-Button
        save_preset_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 310, preset_base_y + 3, 80, 24))
        save_preset_btn.setTitle_("Speichern")
        save_preset_btn.setBezelStyle_(NSBezelStyleRounded)
        save_preset_btn.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(save_preset_btn)

        # Umbenennen-Button
        rename_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 395, preset_base_y + 3, 90, 24))
        rename_btn.setTitle_("Umbenennen")
        rename_btn.setBezelStyle_(NSBezelStyleRounded)
        rename_btn.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(rename_btn)

        # ── Buttons ───────────────────────────────────────────────────────
        target.setup(self, checkboxes, track_names, window, preset_popup)

        # Preset-Button Actions setzen
        load_btn.setTarget_(target)
        load_btn.setAction_(objc.selector(target.onLoadPreset_, signature=b'v@:@'))
        save_preset_btn.setTarget_(target)
        save_preset_btn.setAction_(objc.selector(target.onSavePreset_, signature=b'v@:@'))
        rename_btn.setTarget_(target)
        rename_btn.setAction_(objc.selector(target.onRenamePreset_, signature=b'v@:@'))

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 12, 100, 30))
        cancel_btn.setTitle_("Abbrechen")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(target)
        cancel_btn.setAction_(objc.selector(target.onCancel_, signature=b'v@:@'))
        content.addSubview_(cancel_btn)

        play_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W // 2 - 60, 12, 120, 30))
        play_btn.setTitle_("Play / Stop Testen")
        play_btn.setBezelStyle_(NSBezelStyleRounded)
        play_btn.setTarget_(target)
        play_btn.setAction_(objc.selector(target.onPlay_, signature=b'v@:@'))
        content.addSubview_(play_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W - 120, 12, 100, 30))
        save_btn.setTitle_("Speichern")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r")  # Enter = Speichern
        save_btn.setTarget_(target)
        save_btn.setAction_(objc.selector(target.onSave_, signature=b'v@:@'))
        content.addSubview_(save_btn)

        # Referenzen in globaler Liste halten (NICHT als Instanz-Attribute!)
        # Python-Attribut-Assignments auf ObjC-Proxies loesen Deallokations-
        # kaskaden aus die in PyObjC/Python 3.14 crashen (SIGBUS/SIGSEGV).
        global _config_refs
        _config_refs = [window, target, checkboxes, doc_view, scroll_view, preset_popup,
                        load_btn, save_preset_btn, rename_btn, play_btn]
        window.makeKeyAndOrderFront_(None)
        window.center()


# ─────────────────────────────────────────────────────────────────────────────
# ObjC Helper-Klasse für Button-Actions im Konfigurationsfenster
# ─────────────────────────────────────────────────────────────────────────────
if APPKIT_OK:
    class _ConfigButtonTarget(AppKit.NSObject):
        """ObjC-kompatibles Target fuer Save/Cancel/Preset Buttons."""

        @objc.python_method
        def setup(self, app_ref, checkboxes, track_names, window, preset_popup=None):
            self._app = app_ref
            self._checkboxes = checkboxes
            self._track_names = track_names
            self._window = window
            self._preset_popup = preset_popup

        def onCheckboxToggle_(self, sender):
            pass

        @objc.python_method
        def _read_current_config(self):
            rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list = [], [], [], [], [], [], []
            for name in self._track_names:
                cbs = self._checkboxes.get(name)
                if not cbs:
                    continue
                if cbs["rec_a"].state() == AppKit.NSOnState:
                    rec_a.append(name)
                if cbs["mon_a"].state() == AppKit.NSOnState:
                    mon_a.append(name)
                if cbs["rec_b"].state() == AppKit.NSOnState:
                    rec_b.append(name)
                if cbs["mon_b"].state() == AppKit.NSOnState:
                    mon_b.append(name)
                if cbs["export"].state() == AppKit.NSOnState:
                    export_list.append(name)
                if cbs["loud"].state() == AppKit.NSOnState:
                    loud_list.append(name)
                if cbs.get("play_mon") and cbs["play_mon"].state() == AppKit.NSOnState:
                    play_list.append(name)
            return rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list

        @objc.python_method
        def _apply_config(self, rec_a, mon_a, rec_b, mon_b, export_list, loud_list=None, play_list=None):
            ra, ma, rb, mb, ex = set(rec_a), set(mon_a), set(rec_b), set(mon_b), set(export_list)
            lo = set(loud_list) if loud_list else set()
            pl = set(play_list) if play_list else set()
            for name in self._track_names:
                cbs = self._checkboxes.get(name)
                if not cbs:
                    continue
                cbs["rec_a"].setState_(AppKit.NSOnState if name in ra else AppKit.NSOffState)
                cbs["mon_a"].setState_(AppKit.NSOnState if name in ma else AppKit.NSOffState)
                cbs["rec_b"].setState_(AppKit.NSOnState if name in rb else AppKit.NSOffState)
                cbs["mon_b"].setState_(AppKit.NSOnState if name in mb else AppKit.NSOffState)
                cbs["export"].setState_(AppKit.NSOnState if name in ex else AppKit.NSOffState)
                cbs["loud"].setState_(AppKit.NSOnState if name in lo else AppKit.NSOffState)
                if "play_mon" in cbs:
                    cbs["play_mon"].setState_(AppKit.NSOnState if name in pl else AppKit.NSOffState)

        def onLoadPreset_(self, sender):
            idx = self._preset_popup.indexOfSelectedItem()
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            if 0 <= idx < len(presets):
                p = presets[idx]
                self._apply_config(
                    p.get("rec_a", []), p.get("mon_a", []),
                    p.get("rec_b", []), p.get("mon_b", []),
                    p.get("export", [])
                )
                logging.info(f"Preset {p.get('name')} geladen.")

        def onSavePreset_(self, sender):
            idx = self._preset_popup.indexOfSelectedItem()
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            if 0 <= idx < len(presets):
                rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list = self._read_current_config()
                presets[idx]["rec_a"] = rec_a
                presets[idx]["mon_a"] = mon_a
                presets[idx]["rec_b"] = rec_b
                presets[idx]["mon_b"] = mon_b
                presets[idx]["export"] = export_list
                presets[idx]["loudness_tracks"] = loud_list
                self._app.settings["track_presets"] = presets
                save_settings(self._app.settings)
                name = presets[idx].get("name", f"Preset {idx+1}")
                logging.info(f"Preset {name} gespeichert.")
                rumps.alert("Preset gespeichert", f"{name} wurde gespeichert.")

        def onRenamePreset_(self, sender):
            idx = self._preset_popup.indexOfSelectedItem()
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            if 0 <= idx < len(presets):
                old_name = presets[idx].get("name", f"Preset {idx+1}")
                response = rumps.Window(
                    title="Preset umbenennen",
                    message=f"Neuer Name fuer {old_name}:",
                    default_text=old_name,
                    ok="Umbenennen",
                    cancel="Abbrechen"
                ).run()
                if response.clicked:
                    new_name = response.text.strip()
                    if new_name:
                        presets[idx]["name"] = new_name
                        self._app.settings["track_presets"] = presets
                        save_settings(self._app.settings)
                        self._preset_popup.itemAtIndex_(idx).setTitle_(new_name)
                        logging.info(f"Preset umbenannt: {old_name} -> {new_name}")

        def onSave_(self, sender):
            rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list = self._read_current_config()
            self._app.settings["tracks"] = rec_a
            self._app.settings["monitor_tracks"] = mon_a
            self._app.settings["tracks_b"] = rec_b
            self._app.settings["monitor_tracks_b"] = mon_b
            self._app.settings["export_tracks"] = export_list
            self._app.settings["loudness_tracks"] = loud_list
            self._app.settings["play_monitor_tracks"] = play_list
            save_settings(self._app.settings)
            self._window.close()
            logging.info(f"Spuren-Konfiguration gespeichert:")
            logging.info(f"  Trigger A: Rec={rec_a}, Mon={mon_a}")
            logging.info(f"  Trigger B: Rec={rec_b}, Mon={mon_b}")
            logging.info(f"  Export: {export_list}")
            logging.info(f"  Loudness: {loud_list}")
            logging.info(f"  Play Mon: {play_list}")
            rumps.alert("Gespeichert",
                f"Trigger A: {len(rec_a)} Rec, {len(mon_a)} Mon\n"
                f"Trigger B: {len(rec_b)} Rec, {len(mon_b)} Mon\n"
                f"Export: {len(export_list)} Spuren\n"
                f"Loudness: {len(loud_list)} Spuren\n"
                f"Play Mon: {len(play_list)} Spuren")

        def onCancel_(self, sender):
            self._window.close()



    class _SettingsButtonTarget(AppKit.NSObject):
        """ObjC-kompatibles Target für Settings Save/Cancel Buttons."""

        @objc.python_method
        def setup(self, app_ref, controls, window):
            self._app = app_ref
            self._controls = controls
            self._window = window

        def onSave_(self, sender):
            # Lese Werte aus den Textfeldern und Checkboxen
            try:
                l_enabled = (self._controls["loudness_enabled"].state() == AppKit.NSOnState)
                t_lufs = float(self._controls["target_lufs"].stringValue().replace(",", "."))
                m_tp = float(self._controls["max_truepeak"].stringValue().replace(",", "."))
                i_enabled = (self._controls["interplay_enabled"].state() == AppKit.NSOnState)
                i_ws = self._controls["interplay_workspace"].stringValue()
                i_steps = int(self._controls["interplay_workspace_steps"].stringValue())

                e_tc = self._controls["export_start_tc"].stringValue().strip()
                # TC Format validieren: HH:MM:SS:FF
                tc_parts = e_tc.split(":")
                if len(tc_parts) != 4 or not all(p.isdigit() for p in tc_parts):
                    raise ValueError(f"Ungültiger Timecode: '{e_tc}'\nFormat: HH:MM:SS:FF (z.B. 10:00:00:00)")

                if i_steps < 0:
                    raise ValueError("Steps muss >= 0 sein")

                w_enabled = (self._controls["wav_export_enabled"].state() == AppKit.NSOnState)
                a_enabled = (self._controls["aaf_export_enabled"].state() == AppKit.NSOnState)

                self._app.settings["export_start_tc"] = e_tc
                self._app.settings["wav_export_enabled"] = w_enabled
                self._app.settings["aaf_export_enabled"] = a_enabled
                self._app.settings["loudness_enabled"] = l_enabled
                self._app.settings["target_lufs"] = t_lufs
                self._app.settings["max_truepeak"] = m_tp
                self._app.settings["interplay_enabled"] = i_enabled
                self._app.settings["interplay_workspace"] = i_ws
                self._app.settings["interplay_workspace_steps"] = i_steps
                
                save_settings(self._app.settings)
                logging.info(f"Einstellungen gespeichert: TC={e_tc}, WAV={w_enabled}, AAF={a_enabled}, Loudness={l_enabled}, Interplay={i_enabled}")
                
                self._window.close()
                rumps.alert("Gespeichert", "Erweiterte Einstellungen wurden erfolgreich gespeichert.")
            except ValueError as e:
                AppKit.NSAlert.alertWithMessageText_defaultButton_alternateButton_otherButton_informativeTextWithFormat_(
                    "Eingabefehler", "OK", None, None, f"Bitte ueberpruefe die Eingaben:\n{e}"
                ).runModal()

        def onCancel_(self, sender):
            self._window.close()

class _WebtriggerCopyTarget(AppKit.NSObject):
    """ObjC Target für die Kopieren-Buttons im Webtrigger-Tab."""

    @objc.python_method
    def _do_copy(self, sender):
        tag = sender.tag()
        if hasattr(self, '_app') and hasattr(self._app, '_webtrigger_urls'):
            urls = self._app._webtrigger_urls
            if 0 <= tag < len(urls):
                url, label = urls[tag]
                try:
                    subprocess.run(["pbcopy"], input=url, universal_newlines=True)
                    rumps.notification("PunchBuddy", "Kopiert", f"{label}: {url}")
                except Exception:
                    pass

    def onCopy_(self, sender):
        self._do_copy(sender)

    def onGenerateToken_(self, sender):
        """Erzeugt ein neues Token und schreibt es ins Token-Feld."""
        import secrets
        field = getattr(self, '_token_field', None)
        if field is not None:
            field.setStringValue_(secrets.token_urlsafe(16))

    def onClearToken_(self, sender):
        """Leert das Token-Feld (Webtrigger danach ungeschützt)."""
        field = getattr(self, '_token_field', None)
        if field is not None:
            field.setStringValue_("")

class _UnifiedSettingsTarget(AppKit.NSObject):
    """ObjC-kompatibles Target fuer das kombinierte Einstellungsfenster."""

    @objc.python_method
    def setup(self, app_ref, window, checkboxes, track_names, controls, preset_popup,
              iface_list=None):
        self._app = app_ref
        self._window = window
        self._checkboxes = checkboxes
        self._track_names = track_names
        self._controls = controls
        self._preset_popup = preset_popup
        self._iface_list = iface_list or [("Localhost (127.0.0.1)", "127.0.0.1")]

    @objc.python_method
    def _read_current_config(self):
        rec_a, rec_b, export_list, loud_list, play_list = [], [], [], [], []
        for name in self._track_names:
            cbs = self._checkboxes.get(name)
            if not cbs:
                continue
            if cbs.get("rec_a") and cbs["rec_a"].state() == AppKit.NSOnState: rec_a.append(name)
            if cbs.get("rec_b") and cbs["rec_b"].state() == AppKit.NSOnState: rec_b.append(name)
            if cbs.get("export") and cbs["export"].state() == AppKit.NSOnState: export_list.append(name)
            if cbs.get("loud") and cbs["loud"].state() == AppKit.NSOnState: loud_list.append(name)
            if cbs.get("play_mon") and cbs["play_mon"].state() == AppKit.NSOnState: play_list.append(name)
        return rec_a, rec_b, export_list, loud_list, play_list

    @objc.python_method
    def _apply_config(self, rec_a, rec_b, export_list, loud_list=None, play_list=None):
        ra, rb, ex = set(rec_a), set(rec_b), set(export_list)
        lo = set(loud_list) if loud_list else set()
        pl = set(play_list) if play_list else set()
        for name in self._track_names:
            cbs = self._checkboxes.get(name)
            if not cbs:
                continue
            if cbs.get("rec_a"):
                cbs["rec_a"].setState_(AppKit.NSOnState if name in ra else AppKit.NSOffState)
            if cbs.get("rec_b"):
                cbs["rec_b"].setState_(AppKit.NSOnState if name in rb else AppKit.NSOffState)
            if cbs.get("export"):
                cbs["export"].setState_(AppKit.NSOnState if name in ex else AppKit.NSOffState)
            if cbs.get("loud"):
                cbs["loud"].setState_(AppKit.NSOnState if name in lo else AppKit.NSOffState)
            if cbs.get("play_mon"):
                cbs["play_mon"].setState_(AppKit.NSOnState if name in pl else AppKit.NSOffState)

    @objc.python_method
    def _read_all_controls(self):
        """Returns a dict of all current control values (for preset saving)."""
        d = {}
        bool_keys = ["wav_export_enabled", "aaf_export_enabled", "interplay_enabled",
                     "loudness_enabled", "interplay_rename_enabled", "import_close_session"]
        for key in bool_keys:
            if key in self._controls:
                d[key] = (self._controls[key].state() == AppKit.NSOnState)
        str_keys = ["export_start_tc", "video_track", "interplay_workspace",
                    "export_error_keywords", "export_success_keywords",
                    "interplay_rename_prefix", "interplay_rename_suffix"]
        for key in str_keys:
            if key in self._controls:
                d[key] = self._controls[key].stringValue()
        int_keys = ["extend_count", "interplay_workspace_steps",
                    "interplay_rename_trim_start", "interplay_rename_trim_end", "http_port"]
        for key in int_keys:
            if key in self._controls:
                try:
                    d[key] = int(self._controls[key].stringValue())
                except ValueError:
                    pass
        float_keys = ["target_lufs", "max_truepeak"]
        for key in float_keys:
            if key in self._controls:
                try:
                    d[key] = float(self._controls[key].stringValue().replace(",", "."))
                except ValueError:
                    pass
        if "http_bind_host" in self._controls:
            idx_iface = self._controls["http_bind_host"].indexOfSelectedItem()
            if 0 <= idx_iface < len(self._iface_list):
                d["http_bind_host"] = self._iface_list[idx_iface][1]
        return d

    @objc.python_method
    def _apply_all_controls(self, p):
        """Applies preset dict p to all UI controls."""
        bool_keys = ["wav_export_enabled", "aaf_export_enabled", "interplay_enabled",
                     "loudness_enabled", "interplay_rename_enabled", "import_close_session"]
        for key in bool_keys:
            if key in self._controls and key in p:
                self._controls[key].setState_(
                    AppKit.NSOnState if p[key] else AppKit.NSOffState)
        str_keys = ["export_start_tc", "video_track", "interplay_workspace",
                    "export_error_keywords", "export_success_keywords",
                    "interplay_rename_prefix", "interplay_rename_suffix"]
        for key in str_keys:
            if key in self._controls and key in p:
                self._controls[key].setStringValue_(str(p[key]))
        int_keys = ["extend_count", "interplay_workspace_steps",
                    "interplay_rename_trim_start", "interplay_rename_trim_end", "http_port"]
        for key in int_keys:
            if key in self._controls and key in p:
                self._controls[key].setStringValue_(str(p[key]))
        float_keys = ["target_lufs", "max_truepeak"]
        for key in float_keys:
            if key in self._controls and key in p:
                self._controls[key].setStringValue_(str(p[key]))
        if "http_bind_host" in self._controls and "http_bind_host" in p:
            target_ip = p["http_bind_host"]
            for i, (_, ip) in enumerate(self._iface_list):
                if ip == target_ip:
                    self._controls["http_bind_host"].selectItemAtIndex_(i)
                    break

    def onLoadPreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if 0 <= idx < len(presets):
            p = presets[idx]
            self._apply_config(
                p.get("rec_a", []),
                p.get("rec_b", []),
                p.get("export_tracks", p.get("export", [])),
                p.get("loudness_tracks", []),
                p.get("play_monitor_tracks", []))
            # mon_a/mon_b are no longer in the UI — store directly in settings
            self._app.settings["monitor_tracks"] = p.get("mon_a", [])
            self._app.settings["monitor_tracks_b"] = p.get("mon_b", [])
            self._apply_all_controls(p)
            logging.info(f"Preset {p.get('name')} geladen.")

    def onSavePreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if 0 <= idx < len(presets):
            rec_a, rec_b, export_list, loud_list, play_list = self._read_current_config()
            p = presets[idx]
            p["rec_a"] = rec_a
            p["mon_a"] = self._app.settings.get("monitor_tracks", [])
            p["rec_b"] = rec_b
            p["mon_b"] = self._app.settings.get("monitor_tracks_b", [])
            p["export_tracks"] = export_list
            p["loudness_tracks"] = loud_list
            p["play_monitor_tracks"] = play_list
            p.update(self._read_all_controls())
            self._app.settings["track_presets"] = presets
            save_settings(self._app.settings)
            name = p.get("name", f"Preset {idx+1}")
            logging.info(f"Preset {name} gespeichert.")
            rumps.alert(t("alert_preset_saved"), f"{name} " + t("msg_preset_saved_body"))

    def onRenamePreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if 0 <= idx < len(presets):
            old_name = presets[idx].get("name", f"Preset {idx+1}")
            response = rumps.Window(
                title=t("title_preset_rename"), message=t("msg_preset_rename_body").format(old_name),
                default_text=old_name, ok=t("btn_rename"), cancel=t("cancel")).run()
            if response.clicked:
                new_name = response.text.strip()
                if new_name:
                    presets[idx]["name"] = new_name
                    self._app.settings["track_presets"] = presets
                    save_settings(self._app.settings)
                    self._preset_popup.itemAtIndex_(idx).setTitle_(new_name)
                    logging.info(f"Preset umbenannt: {old_name} -> {new_name}")

    def onNewPreset_(self, sender):
        response = rumps.Window(
            title=t("title_preset_new"), message=t("msg_preset_new_body"),
            default_text=t("default_preset_name"), ok=t("btn_new"), cancel=t("cancel")).run()
        if response.clicked:
            new_name = response.text.strip() or t("default_preset_name")
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            new_preset = {"name": new_name, "rec_a": [], "mon_a": [], "rec_b": [], "mon_b": [],
                          "export_tracks": [], "loudness_tracks": [], "play_monitor_tracks": []}
            presets.append(new_preset)
            self._app.settings["track_presets"] = presets
            save_settings(self._app.settings)
            self._preset_popup.addItemWithTitle_(new_name)
            self._preset_popup.selectItemAtIndex_(len(presets) - 1)
            logging.info(f"Preset {new_name} erstellt.")

    def onDeletePreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if len(presets) <= 1:
            rumps.alert(t("alert_preset_delete_error"), t("msg_preset_delete_error_body"))
            return
        if 0 <= idx < len(presets):
            name = presets[idx].get("name", f"Preset {idx+1}")
            presets.pop(idx)
            self._app.settings["track_presets"] = presets
            save_settings(self._app.settings)
            self._preset_popup.removeItemAtIndex_(idx)
            new_idx = min(idx, len(presets) - 1)
            self._preset_popup.selectItemAtIndex_(new_idx)
            logging.info(f"Preset {name} gelöscht.")

    def onSave_(self, sender):
        try:
            # 1. Spuren speichern (ohne monitor_tracks — nur via Presets steuerbar)
            rec_a, rec_b, export_list, loud_list, play_list = self._read_current_config()
            self._app.settings["tracks"] = rec_a
            self._app.settings["tracks_b"] = rec_b
            self._app.settings["export_tracks"] = export_list
            self._app.settings["loudness_tracks"] = loud_list
            self._app.settings["play_monitor_tracks"] = play_list

            # 2. Erweiterte Einstellungen speichern
            l_enabled = (self._controls["loudness_enabled"].state() == AppKit.NSOnState)
            t_lufs = float(self._controls["target_lufs"].stringValue().replace(",", "."))
            m_tp = float(self._controls["max_truepeak"].stringValue().replace(",", "."))
            
            if "interplay_enabled" in self._controls:
                self._app.settings["interplay_enabled"] = (self._controls["interplay_enabled"].state() == AppKit.NSOnState)
            if "interplay_workspace" in self._controls:
                self._app.settings["interplay_workspace"] = self._controls["interplay_workspace"].stringValue()
                
            i_steps = int(self._controls["interplay_workspace_steps"].stringValue())
            e_tc = self._controls["export_start_tc"].stringValue().strip()

            tc_parts = e_tc.split(":")
            if len(tc_parts) != 4 or not all(p.isdigit() for p in tc_parts):
                raise ValueError(t("msg_invalid_tc").format(e_tc))
            if i_steps < 0:
                raise ValueError(t("msg_steps_negative"))

            if "wav_export_enabled" in self._controls:
                self._app.settings["wav_export_enabled"] = (self._controls["wav_export_enabled"].state() == AppKit.NSOnState)
            if "aaf_export_enabled" in self._controls:
                self._app.settings["aaf_export_enabled"] = (self._controls["aaf_export_enabled"].state() == AppKit.NSOnState)

            v_track = self._controls["video_track"].stringValue().strip()
            if not v_track:
                raise ValueError(t("msg_video_track_empty"))

            self._app.settings["video_track"] = v_track
            self._app.settings["export_start_tc"] = e_tc
            self._app.settings["loudness_enabled"] = l_enabled
            self._app.settings["target_lufs"] = t_lufs
            self._app.settings["max_truepeak"] = m_tp
            self._app.settings["interplay_workspace_steps"] = i_steps

            if "export_error_keywords" in self._controls:
                self._app.settings["export_error_keywords"] = (
                    self._controls["export_error_keywords"].stringValue().strip())
            if "export_success_keywords" in self._controls:
                self._app.settings["export_success_keywords"] = (
                    self._controls["export_success_keywords"].stringValue().strip())

            if "extend_count" in self._controls:
                ext_count = int(self._controls["extend_count"].stringValue())
                if ext_count < 0:
                    raise ValueError(t("msg_extend_count_negative"))
                self._app.settings["extend_count"] = ext_count

            # 3. Monitoring / Play Custom speichern
            for ch_idx in range(1, 3):
                track_key    = f"play_custom_ch{ch_idx}_track"
                mute_s_key   = f"play_custom_ch{ch_idx}_mute_start"
                mute_end_key = f"play_custom_ch{ch_idx}_mute_stop"
                if track_key in self._controls:
                    self._app.settings[track_key] = self._controls[track_key].titleOfSelectedItem() or ""
                if mute_s_key in self._controls:
                    self._app.settings[mute_s_key] = (self._controls[mute_s_key].state() == AppKit.NSOnState)
                if mute_end_key in self._controls:
                    self._app.settings[mute_end_key] = (self._controls[mute_end_key].state() == AppKit.NSOnState)

            # 4. Import-Einstellungen speichern
            if "import_close_session" in self._controls:
                imp_close = (self._controls["import_close_session"].state() == AppKit.NSOnState)
                self._app.settings["import_close_session"] = imp_close

            # 4. Rename Sequence Einstellungen
            if "interplay_rename_enabled" in self._controls:
                self._app.settings["interplay_rename_enabled"] = (
                    self._controls["interplay_rename_enabled"].state() == AppKit.NSOnState)
                try:
                    self._app.settings["interplay_rename_trim_start"] = max(0, int(
                        self._controls["interplay_rename_trim_start"].stringValue()))
                    self._app.settings["interplay_rename_trim_end"] = max(0, int(
                        self._controls["interplay_rename_trim_end"].stringValue()))
                except ValueError:
                    pass
                self._app.settings["interplay_rename_prefix"] = (
                    self._controls["interplay_rename_prefix"].stringValue())
                self._app.settings["interplay_rename_suffix"] = (
                    self._controls["interplay_rename_suffix"].stringValue())

            # 5. Webtrigger-Port und Interface speichern
            need_http_restart = False
            if "http_port" in self._controls:
                try:
                    new_port = int(self._controls["http_port"].stringValue())
                    if not (1024 <= new_port <= 65535):
                        raise ValueError(t("msg_invalid_port"))
                    old_port = self._app.settings.get("http_port", 8899)
                    self._app.settings["http_port"] = new_port
                    if new_port != old_port:
                        need_http_restart = True
                except ValueError as ve:
                    if str(ve) == t("msg_invalid_port"):
                        raise ve
                    raise ValueError(t("msg_invalid_port_value").format(ve))

            if "http_bind_host" in self._controls:
                idx_iface = self._controls["http_bind_host"].indexOfSelectedItem()
                if 0 <= idx_iface < len(self._iface_list):
                    new_host = self._iface_list[idx_iface][1]
                    old_host = self._app.settings.get("http_bind_host", "127.0.0.1")
                    self._app.settings["http_bind_host"] = new_host
                    if new_host != old_host:
                        need_http_restart = True

            if "webtrigger_token" in self._controls:
                new_token = self._controls["webtrigger_token"].stringValue().strip()
                old_token = self._app.settings.get("webtrigger_token", "")
                self._app.settings["webtrigger_token"] = new_token
                if new_token != old_token:
                    need_http_restart = True

            # 6. Sprache speichern und Menü-Titel sofort aktualisieren
            if "language" in self._controls:
                langs_codes = ["de", "en", "fr", "es", "pt"]
                idx_lang = self._controls["language"].indexOfSelectedItem()
                if 0 <= idx_lang < len(langs_codes):
                    new_lang = langs_codes[idx_lang]
                    self._app.settings["language"] = new_lang
                    set_language(new_lang)
                    self._app.update_menu_titles()

            save_settings(self._app.settings)
            self._window.close()
            logging.info("Alle Einstellungen gespeichert.")

            if need_http_restart:
                import threading as _thr
                _thr.Thread(target=self._app._restart_http, daemon=True).start()
                rumps.alert(t("alert_saved"), t("msg_settings_saved_http_restart"))
            else:
                rumps.alert(t("alert_saved"), t("msg_settings_saved_success"))
        except ValueError as e:
            rumps.alert(t("alert_error"), str(e))

    def onCancel_(self, sender):
        self._window.close()

    def onPlay_(self, sender):
        self._app._trigger_play()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
_watchdog_proc = None

def _cleanup_watchdog():
    """Beendet den Watchdog-Prozess beim Schließen von PunchBuddy."""
    global _watchdog_proc
    import sys
    import subprocess as _sp
    
    if getattr(sys, 'frozen', False):
        logging.info("Beende Watchdog App via pkill...")
        try:
            _sp.run(["pkill", "-x", "PunchBuddy_Watchdog"], capture_output=True)
            _sp.run(["pkill", "-f", "MacOS/PunchBuddy_Watchdog$"], capture_output=True)
            logging.info("Watchdog App beendet.")
        except Exception as e:
            logging.debug(f"pkill Watchdog fehlgeschlagen: {e}")
    else:
        if _watchdog_proc is not None:
            logging.info(f"Watchdog Script beenden (PID={_watchdog_proc.pid})...")
            try:
                _watchdog_proc.terminate()
                _watchdog_proc.wait(timeout=3)
                logging.info("Watchdog beendet.")
            except Exception:
                try:
                    _watchdog_proc.kill()
                except Exception:
                    pass
            _watchdog_proc = None

if __name__ == "__main__":

    # Laufzeit-Initialisierung mit Seiteneffekten (Sprache, Settings-Migration).
    # Bewusst hier statt auf Modulebene → Import bleibt nebenwirkungsfrei.
    init_runtime()

    import sys
    import subprocess as _sp
    if getattr(sys, 'frozen', False):
        # Vorher laufende Watchdog-Instanz beenden
        logging.info("Beende ggf. laufende Watchdog-Instanz...")
        try:
            _sp.run(["pkill", "-x", "PunchBuddy_Watchdog"], capture_output=True)
            _sp.run(["pkill", "-f", "MacOS/PunchBuddy_Watchdog$"], capture_output=True)
        except Exception as _ke:
            logging.debug(f"pkill Watchdog (pre-start): {_ke}")
        import time as _t; _t.sleep(0.5)  # kurz warten damit der Prozess weg ist

        # Wir laufen als kompilierte PunchBuddy.app
        my_app_dir = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
        parent_dir = os.path.dirname(my_app_dir)
        target_app = os.path.join(parent_dir, "PunchBuddy_Watchdog.app")

        if os.path.exists(target_app):
            try:
                # -g startet die App im Hintergrund ohne Fokus zu stehlen
                _sp.Popen(["open", "-n", "-g", target_app])
                logging.info(f"Watchdog App gestartet: {target_app}")
            except Exception as e:
                logging.error(f"Konnte Watchdog App nicht starten: {e}")
        else:
            try:
                _sp.Popen(["open", "-n", "-g", "-a", "PunchBuddy_Watchdog"])
                logging.info("Watchdog App via Spotlight gestartet.")
            except Exception as e:
                logging.error(f"Watchdog App Fallback gescheitert: {e}")
    else:
        # Vorher laufende watchdog.py-Instanz beenden (Script-Modus)
        try:
            _sp.run(["pkill", "-f", "watchdog.py"], capture_output=True)
        except Exception:
            pass
        _watchdog_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchdog.py")
        if os.path.exists(_watchdog_path):
            try:
                _watchdog_proc = _sp.Popen(
                    [sys.executable, _watchdog_path],
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
                logging.info(f"Watchdog Script gestartet (💀) PID={_watchdog_proc.pid}")
            except Exception as _we:
                logging.warning(f"Watchdog konnte nicht gestartet werden: {_we}")

    # Signal-Handler: SIGTERM/SIGINT → Watchdog beenden + App stoppen
    import signal

    def _signal_handler(signum, frame):
        logging.info(f"Signal {signum} empfangen – Watchdog beenden...")
        _cleanup_watchdog()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(_cleanup_watchdog)

    PunchBuddyApp().run()
