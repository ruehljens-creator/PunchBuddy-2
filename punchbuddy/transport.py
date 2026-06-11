"""Transport-Orchestrierung: Punch-In/Out, Play, Play-Custom, Stop, GoTo.

Importiert nach „unten" (state, config, keys, engine, uikit). Wird von der UI
und dem Webtrigger aufgerufen.
"""
import gc
import time
import logging
import threading

from punchbuddy import state
from punchbuddy.config import load_settings, DEFAULT_SETTINGS
from punchbuddy.uikit import _dispatch_main
from punchbuddy.keys import (
    _send_key, _send_key_to_app, _pt_pid, _VK_OE, _VK_K,
)
from punchbuddy.engine import (
    _get_engine, _ptsl_call, ensure_preroll_on, restore_preroll,
    refresh_session_tracks, _get_cached_track_names, _show_error,
    _close_engine, _invalidate_session_tracks,
)

_stop_lock    = threading.Lock()  # verhindert gleichzeitige Stop-Aufrufe

def run_punch_in(target_tracks: list, monitor_tracks: list = None):
    # Recording-Skript: Input-Monitoring wird NICHT umgeschaltet – Pro Tools
    # übernimmt das selbst. (Die Play-Input-Logik in run_play bleibt davon
    # unberührt.) monitor_tracks bleibt nur für Aufrufer-Kompatibilität.
    with state.running_lock:
        if state.running:
            logging.warning("Läuft bereits – Trigger ignoriert.")
            return
        state.running = True
    _set_busy(True)

    try:
        logging.info("=== PunchBuddy START ===")
        logging.info(f"Record-Spuren: {target_tracks}")

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

        logging.info(f"Session-Spuren (gecacht): {pt_names}")
        logging.info(f"Record SOLL:    {sorted(rec_want)}")

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

        time.sleep(0.3)  # Settling – PT muss Rec-Enable verarbeiten

        # Recording: kein Input-Monitor-Umschalten (Pro Tools macht das selbst).

        time.sleep(0.5)  # Settling bevor Aufnahme startet

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
            for _ in range(2000):  # max 10 Minuten
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
                    time.sleep(0.3)
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
            time.sleep(0.2)  # Settling nach Pre-Roll-Aus
            # Recording: kein Input-Monitor-Umschalten (Pro Tools macht das selbst).
        else:
            logging.warning("Transport nicht bestätigt gestoppt – State-Wiederherstellung übersprungen.")
            # Pre-Roll trotzdem zurücksetzen (gefahrlos)
            restore_preroll(engine)

        logging.info("Punch-In Durchlauf abgeschlossen.")

    except Exception as e:
        logging.error(f"Fehler: {e}", exc_info=True)
    finally:
        gc.collect()
        with state.running_lock:
            state.running = False
        _set_busy(False)
        logging.info("=== PunchBuddy ENDE ===")

# ─────────────────────────────────────────────────────────────────────────────
# Play (Monitor Auto) Workflow
# ─────────────────────────────────────────────────────────────────────────────

def run_play_custom():
    """Play/Stop-Toggle mit speziellen Mute-States für KH2 und ST Abh.
    Stop-Pfad: delegiert an run_stop() – funktioniert auch während einer Aufnahme.
    Start-Pfad: mutet KH2, entmutet ST Abh und startet Playback."""
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
    with state.running_lock:
        if state.running:
            logging.warning("Play Custom: Script running – start ignored.")
            return
        state.running = True
    _set_busy(True)
    try:
        cfg = state.app_ref.settings if state.app_ref is not None else load_settings()
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

        state.play_custom_active = True

        time.sleep(0.3)
        logging.info("Play Custom Start: starting playback...")
        _ptsl_call(engine.toggle_play_state, label="PlayCustomToggle", timeout=6.0)

    except Exception as e:
        logging.error(f"Error in run_play_custom: {e}", exc_info=True)
        state.play_custom_active = False
    finally:
        with state.running_lock:
            state.running = False
        _set_busy(False)


def run_play():
    """Play/Stop-Toggle.
    Stop-Pfad: delegiert an run_stop() – funktioniert auch während einer Aufnahme.
    Start-Pfad: setzt Input Monitor EIN und startet Playback."""
    import ptsl.PTSL_pb2 as pt

    # Transport-State zuerst lesen – ohne state.running_lock, damit Stop auch während
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
    with state.running_lock:
        if state.running:
            logging.warning("Play: Script läuft bereits – Play-Start ignoriert.")
            return
        state.running = True
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
            state.play_monitor_tracks = list(tracks_to_mon) if mon_ok else []
            if not mon_ok:
                logging.warning("Play: Monitor-EIN fehlgeschlagen – keine Spuren gemerkt.")
        else:
            state.play_monitor_tracks = []
            logging.info("Play: Keine Spur selektiert – starte ohne Monitor-Änderung.")

        time.sleep(0.3)
        logging.info("Play: Starte Playback...")
        _ptsl_call(engine.toggle_play_state, label="PlayToggle", timeout=6.0)

    except Exception as e:
        logging.error(f"Fehler in run_play: {e}", exc_info=True)
    finally:
        with state.running_lock:
            state.running = False
        _set_busy(False)


def run_stop():
    """Dedizierter Stop: entspricht der Leertaste in Pro Tools.
    Stoppt sowohl Play als auch Recording — UNABHÄNGIG von state.running,
    damit ein laufender Punch-In jederzeit unterbrochen werden kann."""

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
        if state.play_monitor_tracks:
            time.sleep(0.3)
            logging.info(f"Stop: Input Monitor AUS für {sorted(state.play_monitor_tracks)}...")
            for attempt in range(3):
                ok, _ = _ptsl_call(
                    engine.set_track_input_monitor_state,
                    sorted(state.play_monitor_tracks), False,
                    label=f"StopMonOff#{attempt+1}", timeout=5.0
                )
                if ok:
                    logging.info("Stop: Input Monitor AUS OK")
                    break
                logging.warning(f"Stop: Input Monitor AUS FEHLER (Versuch {attempt+1}/3)")
                time.sleep(0.5 * (attempt + 1))
            state.play_monitor_tracks = []

        # Play-Custom-Mutes zurücksetzen falls ein /play_custom aktiv war
        if state.play_custom_active:
            time.sleep(0.3)
            cfg = state.app_ref.settings if state.app_ref is not None else load_settings()
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
            state.play_custom_active = False

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

def _set_busy(busy: bool):
    """Setzt den Menüleisten-Indikator auf rot (busy) oder normal (idle).

    Der rumps-Title-Setter ruft NSStatusItem.setTitle_ (AppKit) auf – das
    darf nur auf dem Main-Thread passieren. Aufrufer laufen aber meist in
    Worker-Threads (Punch-In/Export/Import), daher wird hier immer auf den
    Main-Thread dispatcht."""
    if state.app_ref is None:
        return

    def _do():
        try:
            state.app_ref.title = state.ICON_BUSY if busy else state.ICON_IDLE
        except Exception:
            pass
    _dispatch_main(_do)

# ─────────────────────────────────────────────────────────────────────────────
# Export-Workflow
# ─────────────────────────────────────────────────────────────────────────────
_export_lock = threading.Lock()
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
