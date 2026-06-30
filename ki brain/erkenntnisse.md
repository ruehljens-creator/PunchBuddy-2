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

### UPDATE 2026-06-30 (Vergleich dreier Diagnosen: 17:42 / 20:56 „mit videoengine" / 22:19 „Diagnostic3s")
Belegter Fortschritt + verbleibendes Problem (Marker-Zählung über die drei Files):

| Marker | 17:42 (alt) | 20:56 (VideoEngine) | 22:19 (neueste) |
|---|---|---|---|
| **Firmen-NIC `10.249.243.116`** | **116** | 5 | **0** ✅ |
| SLnk_ / vidsat-Ports | 105 / 124 | 102 / 146 | 82 / 103 |
| IsAcquired-false / waitingtrigger | 19 / 64 | 8 / 23 | 21 / 58 |
| `closed channel` | 13 | 22 | 9 |
| Doppel-Trigger (`Läuft bereits`) | 48 | 22 | 33 |

**1. Firmen-NIC-Routing IST behoben** (Netzwerk-Einstellung am Rechner geändert):
`10.249.243.116` taucht in 22:19 **gar nicht mehr** auf. Der Satellite-Clock-Sync
bindet jetzt `from IP 192.168.1.110 → to IP 127.0.0.1 Port 28284` (Diagnostic3s
Zeile 564/565) – also lokal/privat statt ins geroutete Firmennetz. Die
**gefährliche** Hälfte (Traffic in Firmen-Firewalls/Cisco) ist weg.

**2. Der Video-Engine-Satellite läuft aber WEITER** (Antwort auf „glaubt PT noch,
es gäbe Satellite?" = **JA**):
- Session enthält weiterhin eine **„Video 1"-Spur** (Diagnostic3s Z.80) → Video
  Engine startet automatisch („Waiting for the Video Engine to launch…", Z.554).
- `AvidVideoEngine … servicehint vidsat 28284 127.0.0.1` läuft als Prozess (Z.1362).
- Satellite-Fehler bestehen weiter: `IsAcquired - false` (21×), `waitingtrigger`
  (58×), **DAEError-Dialog** auf `192.168.1.110` (Z.572). Treten über die ganze
  Session verteilt auf (PT-Clock 2212…2901), nicht nur beim Start.
→ **WICHTIGE PROJEKT-CONSTRAINT (2026-06-30):** **Video ist Pflicht** – „die
  machen Fernsehen". Video Engine abschalten ist **KEINE** Option. Die Lösung
  muss video-kompatibel sein: den **Satellite-Clock-Sync auf sauberes Loopback
  `127.0.0.1`** (beide Seiten) bringen, statt auf eine geroutete/Switch-NIC.
  Konkret am Studiorechner:
  - Setup → Peripherals → **Satellites/Video** → Interface explizit auf die
    lokale, **nicht-geroutete** Adresse (idealerweise Loopback) setzen, nicht auf
    `10.249.x` und möglichst nicht auf `192.168.1.110`, falls dort ein echter
    Switch/Router hängt.
  - macOS Systemeinst. → Netzwerk → **Set Service Order**: Firmen-/NEXIS-NIC und
    WLAN nach unten; ungenutzte NICs während Sessions deaktivieren.
  - Firewall/Defender: Ports **28282/28284 lokal** erlauben.
  - Verifikation nach Installation des neuen Builds: erweiterte Diagnose
    (Sektionen 10–16: `lsof -iTCP:28282 -iTCP:28284`) zeigt exakt, worauf der
    Satellite bindet → Ziel ist `127.0.0.1` ↔ `127.0.0.1`, dann lockt der Clock
    sofort und `IsAcquired-false`/`waitingtrigger` verschwinden.

**3. Die Trägheit/`closed channel` ist davon UNABHÄNGIG und im installierten
(alten) Build weiterhin live reproduziert** – Diagnostic3s zeigt die komplette
Fehlerkette:
- `20:54:50` **5× „TRIGGER PLAY CUSTOM" in derselben Sekunde** (Stream-Deck-Salve).
- `20:55:58` **3× „TRIGGER A"** + „Läuft bereits – ignoriert".
- direkt danach `20:56:14 [WaitStop] gRPC-Deadline (8.0s) überschritten` →
  **9× `Cannot invoke RPC on closed channel`** (20:56:15–19) →
  `PTSL Verbindung fehlgeschlagen: _InactiveRpcError`.
- Ab `21:01` nur noch **Menü**-Befehle (`>>> MENU 'Play Input'`), Einzeltakt,
  **keine** Salven, **keine** Fehler → bestätigt erneut Menü = sauber,
  Stream-Deck-Burst = Bruch.
→ Genau das adressieren die noch **nicht ausgerollten** Fixes (Commit `294f531`:
  Debounce + Export-Serialisierung + Zombie-Reset) **und** der neue netzwerkfreie
  Unix-Socket (kein HTTP-Doppel-Request-Verstärker). **DMG ausrollen behebt diese
  Hälfte.**

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

## 1b. Systemweite Trägheit (Stream-Deck-Seitenwechsel, Interplay-Fenster) — 2026-07-01
Symptom: nicht nur Transportbefehle, sondern **systemweit** langsam — SD-Seiten-
wechsel + Interplay-Access-Fenster „braucht ewig". Belegt aus 00:39-Diagnose
(Sektion 16 Top-CPU + Sektion 10 Interfaces):
- **Microsoft Defender (Hauptgrund für App/Fenster-Latenz):** `wdavdaemon_enterprise`
  12.8 %, `epsext` (Endpoint-Security-Ext) 4.1 %, `wdavdaemon privileged` 3.1 %.
  epsext hakt sich in **jeden Prozess-/Datei-/Fensteraufruf** ein → Interplay
  (Java) + App-Starts lahm.
- **Satellite-NIC-Dauerlast:** `en7 = 192.168.1.110` = PCI-Ethernet Slot 7 →
  Treiber **`AppleEthernetE1000` 12.9 % CPU**, getrieben vom stockenden
  Clock-Sync (28282) + Defender-netext. Zusätzlich en3–en6 im **PROMISC**-Modus.
- **WindowServer 17.7 % CPU** → ruckelnde UI (SD-Seitenwechsel). Folge von oben.
- Service-Order: Firmen-NIC `en0` (10.249.x) noch primär.
**Hebel:** (1) Defender Passive-Mode/Exclusions (Pro Tools, AvidVideoEngine,
MC_Client, EuControl, Stream Deck, PunchBuddy, Medien-/Session-Volumes);
(2) Satellite auf 127.0.0.1 (nimmt E1000-Last raus); (3) Service-Order/PROMISC
prüfen. NICHT PunchBuddy — dessen Log ist sauber.

**WICHTIG (2026-07-01): „Echtzeitscan aus" hilft NICHT.** Belegt: trotz
abgeschaltetem Real-Time-Scan laufen `wdavdaemon_enterprise edr` (12.8 %),
`epsext` (EndpointSecurity-Ext, 8.1 %), `wdavdaemon privileged` (7.7 %), `netext`
weiter (~30 % CPU zusammen). RTP = nur der **Datei-Virenscan**; die App-/Fenster-
Latenz kommt von der **EndpointSecurity-Extension (epsext)**, die synchron jeden
exec/Datei-Open autorisiert — die läuft unabhängig vom Scan. Auf verwalteten
Firmenrechnern sind EDR/ES **per MDM/Tenant-Policy erzwungen + Tamper-protected** →
nur IT/Security kann Passive-Mode/EDR-Exclusions setzen. Prüfen: `mdatp health
--field real_time_protection_enabled|passive_mode_enabled|tamper_protection|managed_by`.
Satellite-NIC-Isolierung nimmt nur die netext/E1000-Hälfte raus; die epsext-Latenz
bleibt bis zur Policy-Änderung.

`en7`/192.168.1.110 ist **ebenfalls geroutet** (2. Default-Route via 192.168.1.254)
→ keine wirklich isolierte NIC vorhanden; Satellite läuft trotzdem über Router.
Loopback (127.0.0.1) ist im PT-Satellite-Interface nicht wählbar (nur phys. NICs).

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

## 5. Steuer-Architektur: ein Dispatcher, mehrere Transporte (2026-06-30)
PunchBuddy hatte schon immer eine **API-Schicht** – die HTTP-Webtrigger-
Endpunkte. „API" = *was* aufrufbar ist (Befehle) + *wie* der Aufruf reinkommt
(Transport). Die Netzwerk-Frage betrifft nur den **Transport**, nicht die API.

- **Eine zentrale Befehlstabelle** (`command_dispatch`/`_command_table` in
  `auto_punch_in.py`) ist jetzt der einzige Eintrittspunkt; **alle** Transporte
  rufen sie auf. EIN Eintrag pro Funktion.
- **Transporte:** HTTP-Webtrigger (Loopback) **und** neu der **Unix-Domain-
  Socket** (`/tmp/punchbuddy.sock`, 0600) – **ohne IP-Stack**, daher von
  Netzwerk-Filtern (Defender) prinzipiell nicht erfassbar. Beide teilen sich
  Debounce und Export-Serialisierung, weil sie durch denselben Dispatcher/
  dieselben `_trigger_*` laufen.
- **Wichtig (Einordnung):** Der netzwerkfreie Transport ist die saubere Antwort
  auf „Defender nicht entfernbar / direkterer Weg vom Stream Deck", **nicht** auf
  die Trägheit. Die Trägheit sitzt PT-seitig (Video-Engine-Satellite über die
  Firmen-NIC, §1). HTTP-Loopback war nie der Flaschenhals.
- Stream-Deck-Anbindung netzwerkfrei: `.app`-Launcher (`nc -U`) ODER natives
  Node-Plugin (`net` → Socket). Siehe `streamdeck/`.

## 6. Stream-Deck-Node-Plugin läuft am Studiorechner NICHT (2026-07-01) — Ursache + Lösung
**Symptom:** Plugin installiert (Einstellungen-Button → Stream Deck meldet
`Installed plugin 'com.punchbuddy.control'`), Tasten belegbar, aber beim Drücken
**gelbes Dreieck**.

**Ursache (aus Studio-Diagnose 2026-07-01 00:39, belegt):**
- Stream-Deck-Log wiederholt: `NodeManager — Failed to fetch Node.js manifest:
  canceled` (00:21/00:22/00:23/00:24/00:25 …).
- **Kein** `[com.punchbuddy.control] Plugin connected` (auf einem Rechner MIT
  Internet/Node schon) und **kein** Node-Plugin-Prozess in der Prozessliste; auch
  **keine** SD-NodeJS-Runtime vorhanden.
→ Elgato-**Node-Plugins** laden ihre **Node.js-Runtime einmalig per Download**.
  Das **abgeschottete Studionetz** (Defender/Firewall, kein Elgato-CDN) blockiert
  das → Plugin startet nie → Taste ohne Handler → gelbes Dreieck.
- **Wichtig:** Der Socket selbst lief (`Unix-Socket-Steuerung aktiv`), PunchBuddy
  empfing Befehle. Es scheitert NUR an der nicht ladbaren Node-Runtime.

### Bestätigung der Rest-Trägheit (Studio-Diagnose 2026-07-01 00:39, neuer Build)
- **PunchBuddy-Log jetzt sauber:** nur noch „entprellt"-Einträge, **keine**
  `gRPC-Deadline` / `WaitStop` / `closed channel` mehr → die Robustheits-Fixes
  greifen, PunchBuddy ist NICHT mehr die Quelle der Verzögerung.
- **Rest-Ursache = PT-Satellite auf echter NIC:** lsof zeigt
  `Pro Tools … TCP 192.168.1.110:28282 (LISTEN)` (Video-Engine selbst auf
  `127.0.0.1:28284`). Clock-Sync stockt weiter: `IsAcquired-false` 12×,
  `waitingtrigger` 40×. PT wartet bei Transportbefehlen auf den Clock-Lock über
  diese NIC → „manchmal sehr spät".
- **Defender verstärkt:** `com.microsoft.wdav.netext` läuft und sieht
  192.168.1.110 (Loopback würde es nicht sehen) → Extra-Latenz auf dem Sync.
- **Fix:** Satellite Link auf **127.0.0.1** zwingen (beide Ports), dann lockt der
  Clock sofort und Defender kann ihn nicht mehr anfassen. Video bleibt an.

**Lösung (ohne Internet/Node):** die **`.app`-Launcher** verwenden
(Einstellungen → „Vocaster / Stream Deck" → „Tasten-Launcher erzeugen"), im
Stream Deck mit der eingebauten Aktion **„System → Öffnen"**. Brauchen kein Node,
keinen Download, nur das systemeigene `nc` → Socket. **Das ist der empfohlene Weg
für gesperrte Studiorechner.** (Alternative mit Dropdown-UX, falls Netz-Loopback
ok ist: ein klassisches HTML/JS-SD-Plugin gegen `http://127.0.0.1:8899` — braucht
ebenfalls kein Node; Defender filtert Loopback nicht.)
