#!/usr/bin/env python3
"""
Focusrite Vocaster Two USB – direktes Protokoll (Scarlett2).

Protokoll-Details (aus Linux Kernel sound/usb/mixer_scarlett2.c):
  TX:   Control OUT, Endpoint 0, bmRequestType=0x21, bRequest=2 (CMD_REQ)
  RX:   Interrupt IN, Endpoint 0x83, maxPacket=64

  Initialisierung (3 Schritte):
    1. Control IN bRequest=0 (CMD_INIT) → 24 Byte Device-Info
    2. Interrupt-Polling starten
    3. Control OUT cmd=INIT_1 → Interrupt-ACK abwarten
    4. Control OUT cmd=INIT_2 → Firmware-Info (84 Byte) per Interrupt

  Vocaster config_set (param_buf_addr = 0x1bc):
    Phantom  : offset=0x9c,  size=1 bit, activate=20, pbuf=1
    Autogain : offset=0x1c0, size=8,     activate=19, pbuf=1
    AG-Status: offset=0x1c2, size=8 (kein activate, nur lesen)
"""

import os
import sys
import struct
import time
import threading
import ctypes
import ctypes.util
import subprocess
import logging

VOCASTER_VID  = 0x1235
VOCASTER_ONE  = 0x8216
VOCASTER_TWO  = 0x8217
VENDOR_IFACE  = 3
INTR_EP       = 0x83   # Interrupt IN endpoint auf Interface 3

# Bekannte Vocaster-Modelle: PID → (Name, Eingangskanäle)
# Vocaster One hat nur den Host-Eingang, Vocaster Two zusätzlich Guest.
# Beide nutzen laut Linux-Treiber dasselbe Config-Set (scarlett2_config_set_vocaster),
# d.h. identische Register-Offsets – nur die Kanalzahl unterscheidet sich.
VOCASTER_MODELS = {
    VOCASTER_ONE: ("Vocaster One", 1),
    VOCASTER_TWO: ("Vocaster Two", 2),
}

# USB bmRequestType
_CTRL_OUT = 0x21   # OUT | Class | Interface
_CTRL_IN  = 0xA1   # IN  | Class | Interface

# bRequest für Control Transfers
_CMD_INIT = 0  # nur für Step 0 (IN-Only, kein Paket-Wrapper)
_CMD_REQ  = 2  # TX: Command senden
_CMD_RESP = 3  # (nur für Step 0 RX)

# cmd-Feld innerhalb des Scarlett2-Pakets
_INIT_1   = 0x00000000
_INIT_2   = 0x00000002
_GET_DATA = 0x00800000
_SET_DATA = 0x00800001
_DATA_CMD = 0x00800002

# Vocaster param-buffer
_PBUF_ADDR = 0x1bc

# Autogain Status-Strings.
# Dekodierung (siehe Linux-Treiber scarlett2_update_autogain):
#   - Wenn AUTOGAIN_SWITCH (0x1c0) gesetzt → "Running" (Index 0)
#   - Sonst → _AG_STATUS[raw_status + 1]
_AG_STATUS = ["Running", "Success", "FailPG", "FailRange",
              "WarnMaxCap", "WarnMinCap", "Cancelled", "Invalid"]

_TIMEOUT_MS = 5000


def _candidate_libusb_paths():
    """
    Suchpfade für libusb-1.0 in Reihenfolge:
      1. Im PyInstaller-Bundle (sys._MEIPASS) bzw. neben der App – für das DMG.
      2. Homebrew-Standardpfade (Intel /usr/local, Apple Silicon /opt/homebrew).
      3. Schlichter Name (vom Loader über DYLD-Pfade aufgelöst).
    """
    paths = []
    # 1. Gebündelte dylib (eingefroren via PyInstaller)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        paths.append(os.path.join(base, "libusb-1.0.0.dylib"))
    # Neben dem Executable / im Frameworks-Ordner einer .app
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    paths.append(os.path.join(exe_dir, "libusb-1.0.0.dylib"))
    paths.append(os.path.join(exe_dir, "..", "Frameworks", "libusb-1.0.0.dylib"))
    # Neben diesem Modul (Entwicklungs-/Skriptbetrieb)
    paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "libusb-1.0.0.dylib"))
    # 2. Homebrew
    paths.append("/usr/local/lib/libusb-1.0.0.dylib")
    paths.append("/opt/homebrew/lib/libusb-1.0.0.dylib")
    # 3. Loader-aufgelöst
    paths.append("libusb-1.0.0.dylib")
    return paths


class VocasterUSBError(Exception):
    pass


def _load_libusb():
    for path in _candidate_libusb_paths():
        try:
            lib = ctypes.cdll.LoadLibrary(path)
            lib.libusb_init.restype = ctypes.c_int
            lib.libusb_exit.restype = None
            lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
            lib.libusb_close.restype = None
            lib.libusb_control_transfer.restype = ctypes.c_int
            lib.libusb_interrupt_transfer.restype = ctypes.c_int
            lib.libusb_claim_interface.restype = ctypes.c_int
            lib.libusb_release_interface.restype = ctypes.c_int
            lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int
            return lib
        except OSError:
            continue
    raise VocasterUSBError("libusb-1.0 nicht gefunden. Bitte: brew install libusb")


def detect_vocaster():
    """
    Prüft (read-only, ohne die USB-Session zu beanspruchen), ob ein Vocaster
    angeschlossen ist. Stört eine laufende Vocaster Hub App NICHT.

    Rückgabe: None oder dict {"pid", "model", "name", "channels"}.
    """
    try:
        lib = _load_libusb()
    except VocasterUSBError:
        return None
    ctx = ctypes.c_void_p()
    lib.libusb_init(ctypes.byref(ctx))
    try:
        for pid, (name, channels) in VOCASTER_MODELS.items():
            h = lib.libusb_open_device_with_vid_pid(ctx, VOCASTER_VID, pid)
            if h:
                lib.libusb_close(ctypes.c_void_p(h))
                return {"pid": pid, "model": "two" if pid == VOCASTER_TWO else "one",
                        "name": name, "channels": channels}
        # Fallback: manche Systeme erlauben open nicht, aber Enumeration schon
        return None
    finally:
        lib.libusb_exit(ctx)


class VocasterUSB:
    """
    Direkte USB-Kommunikation mit dem Focusrite Vocaster Two.
    TX via Control OUT (Endpoint 0), RX via Interrupt IN (Endpoint 0x83).
    """

    def __init__(self):
        self._lib    = _load_libusb()
        self._ctx    = ctypes.c_void_p()
        self._handle = None
        self._seq    = 1
        self._lock   = threading.Lock()
        self._pid    = None      # erkannte Product-ID
        self._model  = None      # "one" | "two"
        self._channels = 0       # 1 (One) | 2 (Two)
        self._lib.libusb_init(ctypes.byref(self._ctx))

    @property
    def model(self):
        return self._model

    @property
    def channels(self):
        return self._channels

    # ── Verbindung ────────────────────────────────────────────────────────────

    @staticmethod
    def stop_vocaster_hub(wait: float = 2.5):
        """Beendet Vocaster Hub damit unsere USB-Session sauber öffnet."""
        result = subprocess.run(["pgrep", "-x", "Vocaster Hub"], capture_output=True)
        if result.returncode != 0:
            return
        print("Beende Vocaster Hub App...")
        subprocess.run(["pkill", "-x", "Vocaster Hub"])
        time.sleep(wait)

    def _find_device(self):
        """Öffnet das erste angeschlossene Vocaster-Modell. Setzt self._pid/model/channels.
        Gibt das libusb-Handle (int) zurück oder None."""
        for pid, (name, channels) in VOCASTER_MODELS.items():
            h = self._lib.libusb_open_device_with_vid_pid(self._ctx, VOCASTER_VID, pid)
            if h:
                self._pid = pid
                self._model = "two" if pid == VOCASTER_TWO else "one"
                self._channels = channels
                return h
        return None

    def connect(self, stop_hub: bool = True) -> bool:
        """
        Verbindet und initialisiert die USB-Session – modell-bewusst (One/Two).

        PunchBuddy ist der einzige Controller, daher gibt es keinen Konflikt mit
        einer zweiten App – nur die Vocaster Hub App muss weichen (hält sonst die
        USB-Session). Ablauf: öffnen → Hub beenden → USB-Reset → neu öffnen → init.
        """
        h = self._find_device()
        if not h:
            raise VocasterUSBError("Kein Vocaster (One/Two) gefunden.")
        self._handle = ctypes.c_void_p(h)

        if stop_hub:
            self.stop_vocaster_hub()

        # USB-Reset: sauberer Ausgangszustand (nötig nachdem Vocaster Hub die Session hielt)
        self._lib.libusb_reset_device.restype = ctypes.c_int
        ret_reset = self._lib.libusb_reset_device(self._handle)
        logging.info(f"Vocaster USB Reset: {ret_reset}")
        time.sleep(1.5)  # Device braucht Zeit zum Neustart nach Reset

        # Neu öffnen (Handle nach Reset ungültig)
        self._lib.libusb_close(self._handle)
        h2 = self._find_device()
        if not h2:
            raise VocasterUSBError("Vocaster nach Reset nicht gefunden.")
        self._handle = ctypes.c_void_p(h2)

        self._lib.libusb_set_auto_detach_kernel_driver(self._handle, 1)
        ret = self._lib.libusb_claim_interface(self._handle, VENDOR_IFACE)
        if ret != 0:
            raise VocasterUSBError(f"Interface {VENDOR_IFACE} claim fehlgeschlagen: {ret}")

        self._init_session()
        return True

    def disconnect(self):
        if self._handle:
            self._lib.libusb_release_interface(self._handle, VENDOR_IFACE)
            self._lib.libusb_close(self._handle)
            self._handle = None

    @property
    def connected(self) -> bool:
        return self._handle is not None

    # ── Initialisierung ───────────────────────────────────────────────────────

    def _start_intr_poller(self):
        """
        Startet einen Background-Thread der den Interrupt-IN Endpoint kontinuierlich
        liest. Das Device NAKt Control-OUT Transfers solange kein Interrupt-Polling läuft
        (identisches Verhalten wie scarlett2_init_notify im Linux-Treiber).
        Gelesene Pakete werden in self._intr_queue abgelegt.
        """
        import queue
        self._intr_queue = queue.Queue()
        self._intr_stop  = threading.Event()

        def _poll():
            while not self._intr_stop.is_set():
                buf = ctypes.create_string_buffer(64)
                transferred = ctypes.c_int(0)
                ret = self._lib.libusb_interrupt_transfer(
                    self._handle,
                    ctypes.c_uint8(INTR_EP),
                    buf, ctypes.c_int(64),
                    ctypes.byref(transferred),
                    ctypes.c_uint32(500),  # kurzes Timeout damit Stop-Event reagiert
                )
                if ret == 0 and transferred.value > 0:
                    self._intr_queue.put(bytes(buf.raw[:transferred.value]))

        self._intr_thread = threading.Thread(target=_poll, daemon=True)
        self._intr_thread.start()

    def _recv_intr_queued(self, timeout: float = 5.0) -> bytes:
        """Liest nächstes Paket aus der Interrupt-Queue (gepollt vom Background-Thread)."""
        import queue
        try:
            return self._intr_queue.get(timeout=timeout)
        except queue.Empty:
            raise VocasterUSBError("Interrupt RX Timeout (keine Antwort vom Gerät)")

    def _init_session(self):
        """3-Schritt Init wie im Linux-Treiber (scarlett2_usb_init)."""
        # Schritt 0: CMD_INIT IN – Device-Info lesen
        buf = ctypes.create_string_buffer(24)
        ret = self._lib.libusb_control_transfer(
            self._handle,
            ctypes.c_uint8(_CTRL_IN), ctypes.c_uint8(_CMD_INIT),
            ctypes.c_uint16(0), ctypes.c_uint16(VENDOR_IFACE),
            buf, ctypes.c_uint16(24), ctypes.c_uint32(_TIMEOUT_MS),
        )
        if ret < 0:
            raise VocasterUSBError(f"Init Schritt 0 fehlgeschlagen: {ret}")
        print(f"Vocaster Init: Device-Info = {bytes(buf.raw[:ret]).hex()}")

        # Interrupt-Polling starten BEVOR erste Control-OUT gesendet wird
        # (Das Device NAKt Control-OUT solange kein Interrupt-Polling läuft)
        self._start_intr_poller()
        time.sleep(0.05)

        # Schritt 1: INIT_1 (vollständige Transaktion)
        self._seq = 1
        self._transact(_INIT_1, b"", resp_data_len=0)

        # Schritt 2: INIT_2 – Firmware-Info (84 Byte Response)
        self._seq = 1
        try:
            resp2 = self._transact(_INIT_2, b"", resp_data_len=84)
            if len(resp2) >= 12:
                fw = struct.unpack_from("<I", resp2, 8)[0]
                print(f"Vocaster Firmware: {fw}")
        except VocasterUSBError as e:
            print(f"Init2 übersprungen: {e}")

    # ── Low-level ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_header(raw: bytes) -> dict:
        if len(raw) < 16:
            return {}
        cmd, size, seq, error, pad = struct.unpack_from("<IHHIi", raw)
        return {"cmd": cmd, "size": size, "seq": seq, "error": error}

    def _make_pkt(self, cmd: int, data: bytes) -> bytes:
        seq = self._seq & 0xFFFF
        self._seq += 1
        return struct.pack("<IHHIi", cmd, len(data), seq, 0, 0) + data

    def _send_ctrl_locked(self, cmd: int, data: bytes):
        """TX via Control OUT (bRequest=2 CMD_REQ). Caller muss self._lock halten."""
        if not self._handle:
            raise VocasterUSBError("Nicht verbunden")
        pkt = self._make_pkt(cmd, data)
        buf = ctypes.create_string_buffer(bytes(pkt), len(pkt))
        ret = self._lib.libusb_control_transfer(
            self._handle,
            ctypes.c_uint8(_CTRL_OUT), ctypes.c_uint8(_CMD_REQ),
            ctypes.c_uint16(0), ctypes.c_uint16(VENDOR_IFACE),
            buf, ctypes.c_uint16(len(pkt)),
            ctypes.c_uint32(_TIMEOUT_MS),
        )
        if ret < 0:
            raise VocasterUSBError(f"Control TX Fehler: {ret}")

    def _recv_resp(self, resp_data_len: int) -> bytes:
        """RX via Control IN, bRequest=3 (CMD_RESP). Liest 16-Byte-Header + Daten."""
        total = 16 + resp_data_len
        buf = ctypes.create_string_buffer(total)
        ret = self._lib.libusb_control_transfer(
            self._handle,
            ctypes.c_uint8(_CTRL_IN), ctypes.c_uint8(_CMD_RESP),
            ctypes.c_uint16(0), ctypes.c_uint16(VENDOR_IFACE),
            buf, ctypes.c_uint16(total), ctypes.c_uint32(_TIMEOUT_MS),
        )
        if ret < 0:
            raise VocasterUSBError(f"Control RX Fehler: {ret}")
        return bytes(buf.raw[:ret])

    def _transact(self, cmd: int, data: bytes = b"", resp_data_len: int = 0) -> bytes:
        """
        Vollständige Scarlett2-Transaktion:
          1. TX via Control OUT (bRequest=2 CMD_REQ)
          2. ACK-Interrupt abwarten (0x00000001)
          3. RX via Control IN (bRequest=3 CMD_RESP)
        Serialisiert über self._lock damit Sequenz-Nummern konsistent bleiben.
        """
        with self._lock:
            self._send_ctrl_locked(cmd, data)
            # ACK-Interrupt abwarten (vom Background-Poller in die Queue gelegt)
            try:
                self._recv_intr_queued(timeout=3.0)
            except VocasterUSBError:
                pass  # manche Kommandos liefern keinen ACK – RX trotzdem versuchen
            return self._recv_resp(resp_data_len)

    # ── Gerätezugriff ────────────────────────────────────────────────────────

    def _set_data(self, offset: int, size: int, value: int):
        req = struct.pack("<II", offset, size)
        if size == 1:
            req += struct.pack("B", value & 0xFF)
        elif size == 2:
            req += struct.pack("<H", value & 0xFFFF)
        else:
            req += struct.pack("<I", value & 0xFFFFFFFF)
        self._transact(_SET_DATA, req, resp_data_len=0)

    def _get_data(self, offset: int, size: int) -> int:
        req = struct.pack("<II", offset, size)
        raw = self._transact(_GET_DATA, req, resp_data_len=size)
        if len(raw) < 16 + size:
            raise VocasterUSBError(f"Kurze Antwort: {len(raw)} Bytes")
        if size == 1:
            return struct.unpack_from("B", raw, 16)[0]
        elif size == 2:
            return struct.unpack_from("<H", raw, 16)[0]
        return struct.unpack_from("<I", raw, 16)[0]

    def _activate(self, activate_num: int):
        self._transact(_DATA_CMD, struct.pack("<I", activate_num), resp_data_len=0)

    def _set_pbuf(self, activate: int, channel: int, value: int):
        """Schreibt via Parameter-Buffer (pbuf=1 Items)."""
        self._set_data(_PBUF_ADDR + 1, 1, channel)
        self._set_data(_PBUF_ADDR,     1, value)
        self._activate(activate)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_phantom(self, enabled: bool, channel: int = 0):
        """48V Phantomspeisung ein-/ausschalten. channel: 0=Host, 1=Guest."""
        self._set_pbuf(activate=20, channel=channel, value=1 if enabled else 0)

    def start_autogain(self, channel: int = 0):
        """Autogain starten. channel: 0=Host, 1=Guest."""
        self._set_pbuf(activate=19, channel=channel, value=1)

    def get_autogain_status(self, channel: int = 0) -> str:
        """
        Gibt Autogain-Status zurück: 'Running', 'Success', 'FailPG', 'FailRange',
        'WarnMaxCap', 'WarnMinCap', 'Cancelled', 'Invalid'.

        Dekodierung wie im Linux-Treiber:
          - AUTOGAIN_SWITCH (0x1c0) gesetzt → läuft noch → "Running"
          - sonst → _AG_STATUS[raw_status + 1]
        """
        try:
            switch = self._get_data(0x1c0 + channel, 1)
            if switch:
                return "Running"
            raw = self._get_data(0x1c2 + channel, 1)
            idx = raw + 1
            return _AG_STATUS[idx] if idx < len(_AG_STATUS) else "Invalid"
        except Exception as e:
            return f"Error({e})"

    def wait_autogain(self, channel: int = 0, timeout: float = 30.0,
                      poll: float = 0.5, callback=None) -> str:
        """
        Wartet bis Autogain fertig ist.
        callback(elapsed, status) wird nach jedem Poll aufgerufen.
        """
        start = time.time()
        while True:
            elapsed = time.time() - start
            status = self.get_autogain_status(channel)
            if callback:
                callback(elapsed, status)
            if status != "Running":
                return status
            if elapsed >= timeout:
                return "Timeout"
            time.sleep(poll)
