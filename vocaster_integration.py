#!/usr/bin/env python3
"""
Vocaster-Integration für PunchBuddy.

Stellt zwei Bausteine bereit, die PunchBuddy einbindet:

  • AutogainWindow   – natives AppKit-Fenster mit Fortschrittsbalken für den
                       Autogain-Ablauf (zeigt Ergebnis 1 s, schließt selbst).
  • VocasterController – kapselt die USB-Verbindung (vocaster_control.VocasterUSB),
                       48V-Phantom, den Autogain-Ablauf als Main-Thread-State-
                       Machine (drive()) und die Geräteerkennung.

PunchBuddy ist der einzige Prozess, der das Vocaster-USB steuert – dadurch gibt
es keinen Konflikt mit einer zweiten App. Nur die Vocaster Hub App muss beim
ersten Zugriff weichen (sie hält sonst die USB-Session).

Die gesamte UI + USB-Status-Pollerei läuft im Main-Thread (über drive(), das von
einem rumps.Timer in PunchBuddy aufgerufen wird), weil AppKit-Fenster zwingend
im Main-Thread bedient werden müssen. USB-Transaktionen sind schnell (<50 ms).
"""

import time
import threading
import logging

import AppKit

from vocaster_control import VocasterUSB, VocasterUSBError, detect_vocaster

# ── Konstanten ────────────────────────────────────────────────────────────────
AUTOGAIN_TIMEOUT = 20.0   # Sekunden bis Timeout
AUTOGAIN_POLL    = 0.4    # Poll-/Driver-Intervall (Sekunden)
RESULT_HOLD      = 1.0    # Sekunden Ergebnis anzeigen bevor Fenster schließt

# Ergebnis-Texte (Icon, Klartext)
_RESULT_TEXT = {
    "Success":    ("✅", "Pegeln erfolgreich"),
    "WarnMaxCap": ("✅", "Pegeln OK (max. Verstärkung)"),
    "WarnMinCap": ("✅", "Pegeln OK (min. Verstärkung)"),
    "FailPG":     ("⚠️", "Fehlgeschlagen (Pegelgrenze)"),
    "FailRange":  ("⚠️", "Fehlgeschlagen (Bereich)"),
    "Cancelled":  ("⚠️", "Abgebrochen"),
    "Invalid":    ("⚠️", "Ungültiger Status"),
    "Timeout":    ("⚠️", "Timeout – kein Signal?"),
}

_OK_RESULTS = ("Success", "WarnMaxCap", "WarnMinCap")


# ── Nativer Fortschrittsbalken (AppKit) ───────────────────────────────────────

class AutogainWindow:
    """
    Natives NSWindow mit Fortschrittsbalken.
    Alle Methoden MÜSSEN im Main-Thread aufgerufen werden.
    """

    def __init__(self, label: str):
        self._label_text = label
        rect = AppKit.NSMakeRect(0, 0, 480, 168)
        style = (AppKit.NSWindowStyleMaskTitled |
                 AppKit.NSWindowStyleMaskBorderless)
        self.win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        self.win.setLevel_(AppKit.NSFloatingWindowLevel)
        self.win.setTitle_("Autogain")
        self.win.setReleasedWhenClosed_(False)  # sonst Use-after-free beim close()
        self.win.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.win.center()

        content = self.win.contentView()

        # Überschrift
        self.heading = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 132, 440, 24))
        self._style_label(self.heading, 14, bold=True)
        self.heading.setStringValue_("Das automatische Pegeln vom Mikrofon wurde gestartet.")
        content.addSubview_(self.heading)

        # Erklärungstext (zweizeilig)
        self.explain = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 80, 440, 44))
        self._style_label(self.explain, 12)
        self.explain.setStringValue_(
            "Bitte jetzt für ca. 10 Sekunden mit der Vertonungs"
            "lautstärke in das Mikrofon sprechen.")
        if hasattr(self.explain.cell(), "setWraps_"):
            self.explain.cell().setWraps_(True)
        content.addSubview_(self.explain)

        # Fortschrittsbalken (determinate)
        self.bar = AppKit.NSProgressIndicator.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 48, 440, 20))
        self.bar.setIndeterminate_(False)
        self.bar.setMinValue_(0.0)
        self.bar.setMaxValue_(AUTOGAIN_TIMEOUT)
        self.bar.setDoubleValue_(0.0)
        content.addSubview_(self.bar)

        # Countdown-/Hinweistext (ganze Sekunden)
        self.subtext = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 20, 440, 20))
        self._style_label(self.subtext, 11, color=AppKit.NSColor.secondaryLabelColor())
        self.subtext.setStringValue_("0 s")
        content.addSubview_(self.subtext)

        # Accessory-Apps (Menüleisten-Apps) zeigen normale Fenster nur wenn die
        # Activation-Policy kurzzeitig auf Regular gesetzt wird.
        self._prev_policy = AppKit.NSApp.activationPolicy()
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        self.win.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    @staticmethod
    def _style_label(field, size, bold=False, color=None):
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        font = (AppKit.NSFont.boldSystemFontOfSize_(size) if bold
                else AppKit.NSFont.systemFontOfSize_(size))
        field.setFont_(font)
        if color is not None:
            field.setTextColor_(color)

    def update_progress(self, elapsed: float):
        self.bar.setDoubleValue_(min(elapsed, AUTOGAIN_TIMEOUT))
        self.subtext.setStringValue_(f"{int(elapsed)} s")

    def show_result(self, result: str):
        icon, text = _RESULT_TEXT.get(result, ("ℹ️", result))
        ok = result in _OK_RESULTS
        self.heading.setStringValue_(f"{icon}  {text}")
        self.explain.setStringValue_("")
        self.bar.setDoubleValue_(AUTOGAIN_TIMEOUT if ok else 0.0)
        self.subtext.setStringValue_("")

    def close(self):
        self.win.orderOut_(None)
        self.win.close()
        # Zurück zur vorherigen Policy (i.d.R. Accessory – kein Dock-Icon)
        try:
            AppKit.NSApp.setActivationPolicy_(self._prev_policy)
        except Exception:
            AppKit.NSApp.setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)


# ── Controller ────────────────────────────────────────────────────────────────

class VocasterController:
    """
    Kapselt die Vocaster-USB-Steuerung für PunchBuddy.

    Threading-Modell:
      • Verbindungsaufbau (langsam, ~4 s) läuft in Hintergrund-Threads.
      • request_autogain()/phantom() dürfen aus beliebigen Threads (Menü, HTTP)
        aufgerufen werden – sie setzen nur Flags bzw. starten kurze Worker.
      • drive() MUSS im Main-Thread laufen (von rumps.Timer in PunchBuddy),
        weil es das AppKit-Fenster bedient.
    """

    def __init__(self, notify=None):
        self._usb = VocasterUSB()
        self._notify = notify or (lambda *a, **k: None)
        self._conn_lock = threading.Lock()

        # Autogain-State (nur von drive() im Main-Thread verändert)
        self._ag_pending   = None   # (channel, label)
        self._ag_active    = False
        self._ag_window    = None
        self._ag_channel   = 0
        self._ag_label     = ""
        self._ag_start_ts  = 0.0
        self._ag_result    = None
        self._ag_result_ts = 0.0

    # ── Erkennung ──────────────────────────────────────────────────────────────

    @staticmethod
    def detect():
        """read-only Geräteerkennung; None oder dict {model,name,channels,...}."""
        try:
            return detect_vocaster()
        except Exception as e:
            logging.warning(f"Vocaster-Erkennung fehlgeschlagen: {e}")
            return None

    @property
    def channels(self) -> int:
        return self._usb.channels

    # ── Verbindung ─────────────────────────────────────────────────────────────

    def ensure_connected(self) -> bool:
        with self._conn_lock:
            if self._usb.connected:
                return True
            try:
                self._usb.connect(stop_hub=True)
                logging.info(f"Vocaster verbunden: {self._usb.model} "
                             f"({self._usb.channels} Kanal/Kanäle)")
                return True
            except Exception as e:
                logging.error(f"Vocaster-Verbindung fehlgeschlagen: {e}")
                self._notify("PunchBuddy – Vocaster", "USB-Fehler", str(e))
                return False

    def shutdown(self):
        try:
            self._usb.disconnect()
        except Exception:
            pass

    # ── 48V Phantom ────────────────────────────────────────────────────────────

    def set_phantom(self, on: bool, announce: bool = True):
        """Schaltet 48V für alle vorhandenen Kanäle. Läuft im Hintergrund-Thread."""
        def work():
            if not self.ensure_connected():
                return
            try:
                for ch in range(self._usb.channels):
                    self._usb.set_phantom(on, ch)
                if announce:
                    state = "ein" if on else "aus"
                    self._notify("PunchBuddy – Vocaster", f"48V {state}geschaltet",
                                 self._usb.model or "")
            except Exception as e:
                self._notify("PunchBuddy – Vocaster", "48V Fehler", str(e))
        threading.Thread(target=work, daemon=True).start()

    # ── Autogain ───────────────────────────────────────────────────────────────

    def request_autogain(self, channel: int, label: str):
        """
        Aus Menü oder HTTP (evtl. fremder Thread). Setzt nur ein Pending-Flag –
        der eigentliche Ablauf passiert im Main-Thread über drive().
        """
        if self._ag_active or self._ag_pending is not None:
            self._notify("PunchBuddy – Vocaster", "Autogain läuft bereits", "Bitte warten.")
            return
        self._ag_pending = (channel, label)

    def drive(self):
        """
        Main-Thread-Treiber (von rumps.Timer aufgerufen). Behandelt:
          1. Pending-Anfrage → verbinden, Autogain starten, Fenster öffnen
          2. laufenden Autogain → Status pollen + Balken aktualisieren
          3. Ergebnis-Haltephase → nach RESULT_HOLD Fenster schließen
        """
        # ── 1. Neue Anfrage ────────────────────────────────────────────────────
        if not self._ag_active and self._ag_pending is not None:
            channel, label = self._ag_pending
            self._ag_pending = None

            if not self.ensure_connected():
                return
            if channel >= self._usb.channels:
                self._notify("PunchBuddy – Vocaster",
                             f"Autogain {label} nicht verfügbar",
                             f"{self._usb.model} hat keinen {label}-Eingang.")
                return
            try:
                self._usb.start_autogain(channel)
            except Exception as e:
                self._notify("PunchBuddy – Vocaster", "Autogain-Fehler", str(e))
                return

            self._ag_active   = True
            self._ag_channel  = channel
            self._ag_label    = label
            self._ag_start_ts = time.time()
            self._ag_result   = None
            self._ag_window   = AutogainWindow(label)
            return

        if not self._ag_active:
            return

        # ── 3. Ergebnis-Haltephase ─────────────────────────────────────────────
        if self._ag_result is not None:
            if time.time() - self._ag_result_ts >= RESULT_HOLD:
                if self._ag_window:
                    self._ag_window.close()
                    self._ag_window = None
                self._ag_active = False
            return

        # ── 2. Laufender Autogain: pollen ──────────────────────────────────────
        elapsed = time.time() - self._ag_start_ts
        try:
            status = self._usb.get_autogain_status(self._ag_channel)
        except Exception as e:
            status = f"Error({e})"

        if self._ag_window:
            self._ag_window.update_progress(elapsed)

        done = status != "Running"
        if done or elapsed >= AUTOGAIN_TIMEOUT:
            result = status if done else "Timeout"
            self._ag_result = result
            self._ag_result_ts = time.time()
            if self._ag_window:
                self._ag_window.show_result(result)
            icon, text = _RESULT_TEXT.get(result, ("ℹ️", result))
            self._notify("PunchBuddy – Vocaster",
                         f"Autogain {self._ag_label}", f"{icon} {text}")
