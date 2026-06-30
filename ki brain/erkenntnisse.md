# Erkenntnisse

> Bestätigte Ursachen, Architektur-Fakten und recherchierte Belege. Mit Quellen.
> Vermutungen sind als solche gekennzeichnet.

---

## 1. Trägheit / 15 s Verzögerung / Beachball bei Stream-Deck-Befehlen (2026-06-30)

### Kernursache (bewiesen)
Pro Tools steuert seine **interne Avid Video Engine als „Video-Satellite" über
TCP/IP** (Satellite-Link-Clock-Sync) — bei **jedem** Transport-Befehl
(Play/Stop/Record), **auch lokal auf demselben Rechner**.

- Beleg (Avid/Fachquelle, verifiziert): „A wired ethernet connection is required
  for Satellite Link, **even when both applications are on the same computer**",
  Default-Port **28282**.
  → https://non-lethal-applications.com/knowledge-base/VideoSync6/11_DAW%20Sync%20Option%201
- Prozess auf dem Studiorechner: `AvidVideoEngine … servicehint **vidsat** 28284 127.0.0.1 …`
- PT-Log bei jedem Transport: `SLnk_` (Satellite Link), `CSync` (Clock Sync),
  `UME_LockToNetworkClock`, `SyncRemoteClocks`, `eSynchronizerState_waitingtrigger`,
  19× `SLnk_ControlLock::IsAcquired - false`, 8× `CProToolsMachine::Stop - display error`.

### Der eigentliche Fehler: Bindung an die FIRMEN-/NEXIS-NIC
Der Clock-Sync bindet an die **geroutete Firmen-IP `10.249.243.116`** statt an
Loopback:
`UME_LockToNetworkClock … to IP 127.0.0.1 Port 28284, **from IP 10.249.243.116** Port 28282`.
Über diese IP laufen **NEXIS und Interplay**, mit **großen Firewalls + Cisco-
Routern**. Der sample-genaue Clock-Lock-Handshake geht damit ins geroutete
Firmennetz → wird verzögert/gedroppt → Transport-Stall, „network clock not
locked", Beachball.

- Avid: Satellite Link braucht ein **isoliertes Gigabit-Netz**, **niemals**
  geroutet. → https://kb.avid.com/pkb/articles/en_US/troubleshooting/Satellite-Guide
- NEXIS-Clients sind typischerweise **Multi-NIC** → genau die Konstellation, in
  der PT die falsche NIC wählt. → Avid NEXIS Client Guide / EUCON Networking Guidelines.

### Differenzierung Menü vs. Stream Deck (entscheidend)
- **Menü-Befehle laufen sauber, kein Beachball erzeugbar.** **Stream-Deck-Befehle**
  erzeugen 15 s + Hänger.
- Beide Pfade führen **identischen Code** aus (`_trigger_* → run_*` im Daemon-
  Thread). Unterschied = **Befehls-RATE/Pattern**: Menü ist menschlich getaktet,
  Stream Deck feuert schnell/doppelt.
- Beleg im Log (PunchBuddy_Diagnostics1, 2026-06-30): zwei identische Trigger in
  derselben Sekunde, `17:41:46 >>> TRIGGER A <<<` / `17:41:46 >>> TRIGGER A <<<`
  „Läuft bereits – ignoriert"; Play-Custom-Salven 17:40:18/19/21/22.
- Folge: überlappende, gegenläufige Transport-Befehle → PT-Degradation →
  `Cannot invoke RPC on closed channel` → Beachball, roter Punkt bleibt rot.

### Was AUSGESCHLOSSEN wurde (mit Beleg)
- **PunchBuddy/HTTP nicht die Ursache:** curl auf `127.0.0.1:8899` und
  `localhost:8899` < 2 ms; Befehle werden bei Ankunft sofort verarbeitet.
- **Defender filtert den Loopback-PTSL NICHT:** macOS NE-Content-Filter sehen
  Loopback (127.0.0.1/::1) seit macOS 11.3 standardmäßig nicht (`appliesToLoopback = NO`).
  → https://developer.apple.com/forums/thread/680190
  ABER: „Deaktiviert" im UI ≠ Netzwerk-Extension entladen
  (→ https://learn.microsoft.com/en-us/defender-endpoint/mac-support-sys-ext);
  Defender kann unter Burst-Last CPU/Latenz erzeugen und laut MS „network layer
  crashes in unrelated applications"
  (→ https://learn.microsoft.com/en-us/defender-endpoint/network-protection-macos).
  Den LAN-Sync-Traffic (28282) könnte Defender sehr wohl sehen. Avid nennt AV
  ausdrücklich als Video-Engine-Störquelle.
- **macOS 14.7.4:** reines Sicherheits-Update (10.02.2025), kein dokumentierter
  Netzwerk-Bug. → https://support.apple.com/en-us/122901
- **Video-Engine OFF half (zunächst) nicht** — wahrscheinlich, weil eine Videospur
  („Video 1") in der Session die Engine beim Öffnen automatisch reaktiviert
  (belegt). Für dauerhaftes Aus muss die Videospur raus.

### Empfohlene Lösung (Studiorechner, kein Code)
1. **Kein Video nötig →** Avid Video Engine aus (Setup → Playback Engine →
   „Video Engine" abwählen) UND „Video 1"-Spur aus der Session entfernen.
2. **Video nötig →** Setup → Peripherals → Satellites → Interface auf eine
   lokale/nicht-geroutete IP (nicht 10.249.x, nicht WLAN); macOS Systemeinst. →
   Netzwerk → „Set Service Order": NEXIS-/Firmen-NIC nach unten; WLAN aus;
   Firewall/Defender für Ports 28282/28284 lokal freigeben.
3. Verifikation: `sudo lsof -nP -iTCP:28282 -iTCP:28284` → 127.0.0.1 = gut,
   10.249.x = falsch.

---

## 2. PTSL-/gRPC-Architektur-Fakten

- **Genau EINE Engine-Instanz** (Singleton `_engine_instance`, `engine.py`),
  serialisiert über **einen globalen `_ptsl_lock`** — alle PTSL-Calls laufen
  seriell.
- **Default-gRPC-Deadline = 15 s** (`_GRPC_CALL_DEADLINE = 15.0`, `engine.py:154`);
  `_ptsl_call`-Default-Timeout 15 s; Lock-Acquire-Timeout 6 s.
- `_reset_engine()` wird **nur** in `_ptsl_call` bei `grpc.RpcError` ausgelöst.
  → **Rohe `engine.*`-Aufrufe, die NICHT über `_ptsl_call` laufen, verwerfen
  einen toten Channel NICHT** → Zombie-/CLOSE_WAIT-Verbindung (siehe Fehlerbehebungen #2).
- `_set_busy()` (transport.py) ist ein **Bool-Setter**, kein Refcount → bei
  überlappenden Workern kann der rote Punkt falsch gelöscht werden oder hängen
  bleiben.

## 3. Off-Main-Thread (AppKit) — Stand: sauber
Der Audit (2026-06-30) fand **keine** verbleibenden Off-Main-Thread-AppKit-
Aufrufe: `_set_busy` dispatcht korrekt über `_dispatch_main`/`AppHelper.callAfter`;
`AutogainWindow` wird garantiert im Main-Thread erzeugt/geschlossen. Der frühere
SIGSEGV (Import-Ende ohne Internet) wurde mit Commit `0715c2b` behoben
(`_dispatch_main` → `AppHelper.callAfter`).

## 4. Diagnose-Script — was es NICHT erfasst (Stand vor 2026-06-30-Erweiterung)
Die `ps`-Sektion war auf Pro-Tools-Prozesse gefiltert → Defender/Stream Deck/
Netzwerk tauchten nicht auf. Mit der Erweiterung (Sektionen 10–16) werden jetzt
Netzwerk/Service-Order, Satellite-Ports, Defender-Status, System-Extensions,
Firewall, Stream-Deck-Logs und die volle Top-CPU-Prozessliste erfasst.
