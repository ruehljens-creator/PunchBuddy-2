# PunchBuddy – Technische Dokumentation

**Version:** aktuell (Build 2026-06-10)  
**Programmiert von:** Jens Rühl & Christian Becker

---

## Inhaltsverzeichnis

1. [Architektur](#1-architektur)
2. [Datei-Pfade](#2-datei-pfade)
3. [Alle Einstellungen (Settings-Referenz)](#3-alle-einstellungen-settings-referenz)
4. [HTTP-API Referenz](#4-http-api-referenz)
5. [Preset-Format](#5-preset-format)
6. [Export-Pipeline im Detail](#6-export-pipeline-im-detail)
7. [Import-Pipeline im Detail](#7-import-pipeline-im-detail)
8. [Lautheitskorrektur (EBU R128)](#8-lautheitskorrektur-ebu-r128)
9. [Watchdog](#9-watchdog)
10. [Focusrite Vocaster-Integration](#10-focusrite-vocaster-integration)
11. [Abhängigkeiten & Lizenzen](#11-abhängigkeiten--lizenzen)

---

## 1. Architektur

### Laufzeitumgebung

| Komponente | Version / Details |
|---|---|
| Python | 3.14 (PyInstaller-Bundle) |
| PyObjC / AppKit | System-Framework (macOS) |
| Quartz / CoreGraphics | System-Framework (macOS) |
| PTSL | Pro Tools Scripting Library via gRPC |
| rumps | macOS Menüleisten-Framework (BSD 3-Clause) |
| pyloudnorm | EBU R128 Lautheitsmessung (MIT) |
| soundfile | WAV-Lesen/Schreiben (BSD 3-Clause) |
| numpy / scipy | Numerische Verarbeitung (BSD 3-Clause) |

### Prozessmodell

```
PunchBuddy.app (Hauptprozess)
├── Main Thread         rumps-Runloop + AppKit NSRunLoop
├── HTTP-Thread         TCPServer.serve_forever (daemon)
├── PTSL-Keepalive      Hält gRPC-Verbindung zu Pro Tools offen
├── Background-Thread   Pro-Tools-Aktionen (Export, Import, Punch-In)
└── PunchBuddy_Watchdog.app (separater Prozess, überwacht Hauptprozess)
```

### AppKit-Threading-Regel

Alle AppKit-Aufrufe (Fenster, Labels, Fortschrittsbalken) müssen auf dem Main Thread laufen. PunchBuddy verwendet `_dispatch_main(fn)` für alle solchen Aufrufe aus Background-Threads:

```python
def _dispatch_main(fn):
    NSRunLoop.mainRunLoop().performBlock_(fn)
```

### Keyboard-Injektion

PunchBuddy nutzt drei Methoden zum Senden von Tastendrücken:

| Methode | Verwendung |
|---|---|
| `Quartz.CGEventPost` | Systemweite Tastendrücke (Shift+Ö für Spurausdehnung) |
| `Quartz.CGEventPostToPid` | App-spezifische Tastendrücke (z.B. F2, Cmd+A, Cmd+C, Texteingabe Loop bei Sequenz-Umbenennung in Interplay Access) zur Umgehung von macOS Accessibility-Problemen |
| `AppleScript System Events keystroke` | Java-Menüaktionen und Importschritte in Interplay Access zur Clipboard-Synchronisierung |

---

## 2. Datei-Pfade

| Pfad | Inhalt |
|---|---|
| `~/.punchbuddy/settings.json` | Alle Einstellungen (JSON) |
| `~/.punchbuddy/logs/auto_punch_in_YYYY-MM-DD.log` | Tageslog (Rotation nach 24h) |
| `/tmp/build_ap/` | PyInstaller Build-Cache |
| `/tmp/dist_ap/` | PyInstaller Build-Output |

### Einstellungen laden / speichern

```python
SETTINGS_PATH = os.path.expanduser("~/.punchbuddy/settings.json")
```

Beim Laden werden die gespeicherten Werte mit `DEFAULT_SETTINGS` zusammengeführt — fehlende Schlüssel (z.B. nach einem Update) werden automatisch mit Standardwerten ergänzt.

---

## 3. Alle Einstellungen (Settings-Referenz)

Die Einstellungen werden als JSON in `~/.punchbuddy/settings.json` gespeichert.  
Alle Werte können über **Menü → Einstellungen…** in der GUI verändert werden oder direkt in der JSON-Datei editiert werden (App muss danach neu gestartet werden).

---

### Spurzuweisungen

#### `tracks`
**Typ:** Liste von Strings  
**Standard:** `["ST", "A  IT", "OGA", "Mus", "Spr", "Spr 2"]`  
**Tab:** Spurenauswahl → Spalte „Trigger A"  
Spurnamen, die bei **Punch-In A** auf Record-Ready geschaltet werden. Die Namen müssen exakt den Spurnamen in der Pro Tools Session entsprechen (Groß-/Kleinschreibung beachten).

#### `monitor_tracks`
**Typ:** Liste von Strings  
**Standard:** `["ST", "A  IT", "OGA", "Mus", "Spr"]`  
**Tab:** Nur über Presets  
Spurnamen, die bei **Punch-In A** zusätzlich auf Input-Monitor gesetzt werden. Nur über Presets konfigurierbar.

#### `tracks_b`
**Typ:** Liste von Strings  
**Standard:** `[]`  
**Tab:** Spurenauswahl → Spalte „Trigger B"  
Spurnamen für **Punch-In B**. Wenn leer, wird Trigger B ignoriert.

#### `monitor_tracks_b`
**Typ:** Liste von Strings  
**Standard:** `[]`  
**Tab:** Nur über Presets  
Spurnamen für Input-Monitor bei **Punch-In B**. Nur über Presets konfigurierbar.

#### `export_tracks`
**Typ:** Liste von Strings  
**Standard:** `["ST", "A  IT", "OGA", "Mus", "Spr"]`  
**Tab:** Spurenauswahl → Spalte „Export"  
Spurnamen, die in WAV-, AAF- und Interplay-Export einbezogen werden (Consolidate).

#### `loudness_tracks`
**Typ:** Liste von Strings  
**Standard:** `["ST"]`  
**Tab:** Spurenauswahl → Spalte „Loud"  
Spurnamen, auf die die Lautheitskorrektur (EBU R128) angewendet wird. Meist nur die Stereo-Mix-Spur.

#### `play_monitor_tracks`
**Typ:** Liste von Strings  
**Standard:** `[]`  
**Tab:** Spurenauswahl → Spalte „Play"  
Spurnamen, die beim `/play`-Trigger auf Input-Monitor gesetzt werden.

---

### Export-Einstellungen Allgemein

#### `export_start_tc`
**Typ:** String (Format `HH:MM:SS:FF`)  
**Standard:** `"10:00:00:00"`  
**Tab:** Export-Einstellungen → „Start-Timecode"  
Timecode-Position, ab der der Consolidate-Vorgang beginnt. Alle Export-Spuren werden ab diesem Punkt konsolidiert.

#### `video_track`
**Typ:** String  
**Standard:** `"Video 1"`  
**Tab:** Export-Einstellungen → „Video-Spurname"  
Name der Videospur in Pro Tools. Wird verwendet, um das Ende des Videos zu erkennen und die Export-Länge zu bestimmen.

#### `extend_count`
**Typ:** Integer (≥ 0)  
**Standard:** `7`  
**Tab:** Export-Einstellungen → „Shift+Ö Anzahl"  
Anzahl der Tastendrücke `Shift+Ö` für die Spurausdehnung (Extend Selection to End of Next Clip). Bestimmt wie weit die Selektion über das Videoende hinaus ausgedehnt wird, um Überhänge zu erfassen.

---

### Lautheitskorrektur

#### `loudness_enabled`
**Typ:** Boolean  
**Standard:** `true`  
**Tab:** Export-Einstellungen → „Lautheitskorrektur aktivieren"  
Aktiviert/deaktiviert die EBU R128 Lautheitskorrektur vor dem Export.

#### `target_lufs`
**Typ:** Float  
**Standard:** `-23.0`  
**Tab:** Export-Einstellungen → „Ziel-Lautheit (LUFS)"  
Ziel-Integrated-Loudness in LUFS nach EBU R128. Typische Werte: `-23.0` (EBU R128), `-16.0` (Streaming).

#### `max_truepeak`
**Typ:** Float  
**Standard:** `-3.0`  
**Tab:** Export-Einstellungen → „Max True Peak (dB)"  
Maximaler True Peak Pegel in dBTP. Der Gain der Lautheitskorrektur wird so begrenzt, dass dieser Pegel nicht überschritten wird.

---

### Export-Arten aktivieren/deaktivieren

#### `wav_export_enabled`
**Typ:** Boolean  
**Standard:** `false`  
**Tab:** Export-Einstellungen → „WAV Export aktivieren"  
Wenn `true`, wird bei „Export (komplett)" ein WAV-Export durchgeführt.

#### `aaf_export_enabled`
**Typ:** Boolean  
**Standard:** `false`  
**Tab:** Export-Einstellungen → „AAF Export aktivieren"  
Wenn `true`, wird bei „Export (komplett)" ein AAF-Export durchgeführt.

#### `interplay_enabled`
**Typ:** Boolean  
**Standard:** `false`  
**Tab:** Export-Einstellungen → „Avid Interplay NEXIS Export aktivieren"  
Wenn `true`, wird bei „Export (komplett)" ein Interplay-Export durchgeführt.

---

### Interplay Export Einstellungen

#### `interplay_workspace`
**Typ:** String  
**Standard:** `"001-aktuelles [fad-nexis]"`  
**Tab:** Export-Einstellungen → „Workspace Name"  
Name des Interplay NEXIS Workspace, in den exportiert werden soll. Muss exakt dem Workspace-Namen in Interplay Access entsprechen.

#### `interplay_workspace_steps`
**Typ:** Integer (≥ 0)  
**Standard:** `17`  
**Tab:** Export-Einstellungen → „Workspace Position (Steps)"  
Anzahl der Pfeiltastendrücke (nach oben), um den gewünschten Workspace in der Workspace-Liste in den Export-Optionen anzusteuern. Entspricht der Position des Workspaces in der Liste von unten gezählt.

#### `export_error_keywords`
**Typ:** String (kommagetrennte Schlüsselwörter)  
**Standard:** `"error,fail,fehler,unsuccessful,could not,unable,problem,warning,aborted,abgebrochen"`  
**Tab:** Export-Einstellungen → „Fehler-Keywords"  
Wenn eines dieser Wörter im Export-Ergebnis-Dialog erscheint, wertet PunchBuddy den Export als fehlgeschlagen.

#### `export_success_keywords`
**Typ:** String (kommagetrennte Schlüsselwörter)  
**Standard:** `"success,complete,finished,done,exported,erfolgreich,abgeschlossen,fertig"`  
**Tab:** Export-Einstellungen → „Erfolgs-Keywords"  
Wenn eines dieser Wörter im Export-Ergebnis-Dialog erscheint, wertet PunchBuddy den Export als erfolgreich.

---

### Rename Sequence (nach Interplay Export)

#### `interplay_rename_enabled`
**Typ:** Boolean  
**Standard:** `false`  
**Tab:** Export-Einstellungen → „Umbenennung nach Export aktivieren"  
Wenn `true`, wird nach einem erfolgreichen Interplay Export die Sequence in Interplay Access automatisch umbenannt.

#### `interplay_rename_trim_start`
**Typ:** Integer (≥ 0)  
**Standard:** `0`  
**Tab:** Export-Einstellungen → „Zeichen am Anfang löschen"  
Anzahl der Zeichen, die vom Beginn des aktuellen Sequence-Namens entfernt werden.

#### `interplay_rename_trim_end`
**Typ:** Integer (≥ 0)  
**Standard:** `0`  
**Tab:** Export-Einstellungen → „Zeichen am Ende löschen"  
Anzahl der Zeichen, die vom Ende des aktuellen Sequence-Namens entfernt werden.

#### `interplay_rename_prefix`
**Typ:** String  
**Standard:** `""`  
**Tab:** Export-Einstellungen → „Präfix hinzufügen"  
String, der dem (ggf. gekürzten) Namen vorangestellt wird.

#### `interplay_rename_suffix`
**Typ:** String  
**Standard:** `""`  
**Tab:** Export-Einstellungen → „Suffix hinzufügen"  
String, der dem (ggf. gekürzten) Namen angehängt wird.

**Rename-Formel:**
```
Neuer Name = prefix + name[trim_start : len(name) - trim_end] + suffix
```

---

### Import-Einstellungen

#### `import_close_session`
**Typ:** Boolean  
**Standard:** `true`  
**Tab:** Import → „Offene Session vor Import schließen"  
Wenn `true`, prüft PunchBuddy vor dem Interplay Import ob eine Pro Tools Session geöffnet ist. Falls ja, wird sie gespeichert und geschlossen, bevor der Import startet.

---

### Webtrigger / HTTP-Server

#### `http_port`
**Typ:** Integer (1024–65535)  
**Standard:** `8899`  
**Tab:** Webtrigger → „Port"  
TCP-Port, auf dem der interne HTTP-Server lauscht. Nach Änderung ist ein Neustart des HTTP-Servers erforderlich (erfolgt automatisch beim Speichern, wenn sich der Wert geändert hat).

#### `http_bind_host`
**Typ:** String (IP-Adresse)  
**Standard:** `"127.0.0.1"`  
**Tab:** Webtrigger → „Netzwerk-Interface"  
IP-Adresse, auf der der HTTP-Server lauscht:
- `"127.0.0.1"` → Nur lokaler Zugriff (Localhost)
- Jede andere IP → Server bindet auf `0.0.0.0` (alle Interfaces) und zeigt die gewählte IP in den URLs an

Nach Änderung wird der HTTP-Server automatisch neu gestartet.

---

### Presets

#### `track_presets`
**Typ:** Liste von Preset-Objekten  
**Standard:** 8 leere Presets (`Preset 1` bis `Preset 8`)  
**Tab:** Presets  

Jedes Preset-Objekt kann folgende Felder enthalten (alle optional mit sinnvollen Defaults):

```json
{
  "name": "Preset 1",
  "rec_a": [],
  "mon_a": [],
  "rec_b": [],
  "mon_b": [],
  "export_tracks": [],
  "loudness_tracks": [],
  "play_monitor_tracks": [],
  "wav_export_enabled": false,
  "aaf_export_enabled": false,
  "interplay_enabled": false,
  "loudness_enabled": true,
  "interplay_rename_enabled": false,
  "import_close_session": true,
  "export_start_tc": "10:00:00:00",
  "video_track": "Video 1",
  "extend_count": 7,
  "target_lufs": -23.0,
  "max_truepeak": -3.0,
  "interplay_workspace": "001-aktuelles [fad-nexis]",
  "interplay_workspace_steps": 17,
  "export_error_keywords": "...",
  "export_success_keywords": "...",
  "interplay_rename_trim_start": 0,
  "interplay_rename_trim_end": 0,
  "interplay_rename_prefix": "",
  "interplay_rename_suffix": "",
  "http_port": 8899,
  "http_bind_host": "127.0.0.1"
}
```

---

## 4. HTTP-API Referenz

### Server-Konfiguration

- **Protokoll:** HTTP/1.0 (TCP)
- **Methode:** GET
- **Response:** `200 OK` + Body `"OK"` bei Erfolg, `404` bei unbekanntem Pfad
- **Logging:** Kein HTTP-Access-Log (bewusst deaktiviert)

### Endpunkte

| Pfad | Aktion | Hinweis |
|---|---|---|
| `/trigger` | Punch-In A | Startet Aufnahme mit `tracks` + `monitor_tracks` |
| `/trigger2` | Punch-In B | Startet Aufnahme mit `tracks_b` + `monitor_tracks_b`; ignoriert wenn `tracks_b` leer |
| `/play` | Play/Stop Toggle | Startet oder stoppt Wiedergabe |
| `/stop` | Stop | Stoppt Wiedergabe dediziert |
| `/export` | Export (komplett) | Führt alle aktivierten Export-Arten aus |
| `/export_wav` | WAV Export | Nur WAV (inkl. Lautheitskorrektur) |
| `/export_aaf` | AAF Export | Nur AAF (inkl. Lautheitskorrektur) |
| `/export_interplay` | Interplay Export | Nur Interplay (inkl. Lautheitskorrektur) |
| `/import` | Interplay Import | Importiert Sequence aus Interplay Access |

### Beispiel (curl)

```bash
# Punch-In A auslösen
curl http://127.0.0.1:8899/trigger

# Interplay Export starten
curl http://127.0.0.1:8899/export_interplay
```

### Bindeadresse

```
http_bind_host == "127.0.0.1"  →  TCPServer("127.0.0.1", port)   # nur lokal
http_bind_host != "127.0.0.1"  →  TCPServer("0.0.0.0", port)     # alle Interfaces
```

---

## 5. Preset-Format

Presets werden als Teil von `settings.json` unter dem Schlüssel `track_presets` als JSON-Array gespeichert.

### Backward-Kompatibilität

Ältere Preset-Objekte mit nur `rec_a`, `mon_a`, `rec_b`, `mon_b`, `export` (altes Format) werden weiterhin korrekt geladen — fehlende Felder werden durch `p.get("key", default)` mit sinnvollen Werten ergänzt.

Das neue Feld `export_tracks` ersetzt das alte Feld `export`. Beim Laden wird `export_tracks` bevorzugt, `export` als Fallback verwendet:

```python
p.get("export_tracks", p.get("export", []))
```

---

## 6. Export-Pipeline im Detail

### Interplay Export — Ablauf

```
1.  Lautheitskorrektur (EBU R128) auf loudness_tracks
2.  DMGs auswerfen (falls vorhanden)
3.  Consolidate der export_tracks ab export_start_tc bis Video-Ende
4.  Überhänge trimmen (extend_count × Shift+Ö → Selection End)
5.  Menü File → Export → Clips as Files... öffnen
6.  Export Comment eingeben
7.  Export Options: Format, Sample Rate, Bit Depth wählen
8.  Workspace auswählen (interplay_workspace_steps × Pfeil oben)
9.  Export starten
10. PT-Log via kqueue überwachen → _poll_pt_log_for_export()
11. Bestätigungsdialog lesen → Erfolg/Fehler via Keywords erkennen
12. Return-Taste (Dialog bestätigen)
13. Modal-Dismiss via kqueue → _wait_for_pt_modal_dismissed()
    Signal: "SLnk_MachineMgr::SendDialogOnScreen: 108846044" im DigiTrace.log
14. Optional: Sequence umbenennen in Interplay Access
```

### kqueue-basierte Log-Überwachung

PunchBuddy verwendet BSD kqueue VNODE-Events für effiziente Log-Überwachung ohne Busy-Wait:

```python
ke = select.kevent(fd,
    filter=select.KQ_FILTER_VNODE,
    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
```

Sobald Pro Tools in `DigiTrace.log` schreibt, wird der Event ausgelöst und der neue Inhalt gelesen — Latenz < 10 ms statt 20–40 Sekunden sleep.

### Interplay Access — Fenstersteuerung

Alle Aktionen in Interplay Access werden via **AppleScript System Events** ausgeführt:

```python
_run_applescript('tell application "System Events" to tell process "interplayAccess" '
                 'to keystroke "c" using command down')
```

Vor Keystrokes wird Interplay Access immer explizit aktiviert:

```python
_run_applescript('tell application "interplayAccess" to activate')
time.sleep(0.3)
```

---

## 7. Import-Pipeline im Detail

```
1.  Optional: PT-Session speichern (Cmd+S) und schließen (Cmd+Shift+W)
2.  Interplay Access aktivieren
3.  F2 → Sequenznamen-Edit-Feld öffnen
4.  Cmd+A + Cmd+C → Sequenznamen in Clipboard kopieren
5.  ESC → Edit-Feld schließen
6.  Sequenzname via pbpaste auslesen
7.  Cmd+Shift+P → "Send to Pro Tools" in Interplay Access
8.  Warten bis PT-Dialog erscheint (kqueue auf PT-Log)
9.  Session-Pfad in Offline-Dialog einfügen
10. Import Session Data Dialog navigieren:
    Match All → Preset 1 → Import
11. Warten bis PT Session geladen ist
```

---

## 8. Lautheitskorrektur (EBU R128)

### Messung

PunchBuddy nutzt **pyloudnorm** für die Integrated Loudness Messung nach ITU-R BS.1770-4 / EBU R128:

```python
meter = pyloudnorm.Meter(rate, filter_class="K-weighting")
loudness = meter.integrated_loudness(data)
```

### Gain-Berechnung

```python
gain_db = target_lufs - loudness
# Begrenzen durch max_truepeak:
peak = np.max(np.abs(data))
max_gain_from_peak = 20 * np.log10(10 ** (max_truepeak / 20) / (peak + 1e-9))
gain_db = min(gain_db, max_gain_from_peak)
```

### Fortschrittsanzeige (Lautheit)

Ein separates, zentriertes Fenster zeigt den Fortschritt pro Spur:

```
Track 1/3: ST  ████████████░░░░  65%
```

Fenster-Refs werden in `_loudness_win_refs` gehalten um PyObjC-Dealloc-Crashes zu verhindern.

---

## 9. Watchdog

`PunchBuddy_Watchdog.app` ist eine separate PyInstaller-App, die in einer Schleife prüft ob PunchBuddy läuft, und es bei Bedarf neu startet.

### Start-Sequenz beim PunchBuddy-Start

```
1. pkill -x PunchBuddy_Watchdog          # bestehende Instanz beenden
2. pkill -f MacOS/PunchBuddy_Watchdog$   # Fallback
3. time.sleep(0.5)                        # warten bis Prozess weg
4. open -n -g PunchBuddy_Watchdog.app    # neue Instanz starten (Hintergrund)
```

### Beenden

```python
atexit.register(_cleanup_watchdog)
signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
```

---

## 10. Focusrite Vocaster-Integration

PunchBuddy integriert eine direkte USB-Steuerung für Focusrite Vocaster One & Two Audiointerfaces. Diese Integration läuft direkt über USB (kapselt `pyusb` und die USB-Kommunikation mit dem Gerät), ohne dass die offizielle Focusrite Vocaster Hub-App benötigt wird.

### Features
*   **Auto Gain Steuerung:** Auslösen des automatischen Einpegelns über HTTP-Webtrigger oder das Menü. Ein natives AppKit-Fortschrittsfenster wird auf dem Main Thread gerendert, um den Benutzer durch den 10-sekündigen Pegelvorgang zu leiten.
*   **48V-Phantomspannung:** Schalten der Phantomspeisung beim Starten der Anwendung oder manuell über das Menü.
*   **MUX-Routing:** Auslesen und Wiederherstellen von gespeicherten Routing-Tabellen des Focusrite-Mixers, um das korrekte Kanal-Routing beim App-Start automatisch zu gewährleisten.

### Implementierungsdetails
*   `vocaster_control.py` implementiert das Low-Level USB-Protokoll und die Erkennung.
*   `vocaster_integration.py` kapselt die State-Machine (`drive()`), die über einen `rumps.Timer` alle 400 ms auf dem Main-Thread getaktet wird, sowie die Fenster-Verwaltung.
*   Zur USB-Kommunikation wird die `libusb-1.0` benötigt, die im Intel-Build direkt in das App-Bundle eingebettet ist (`--add-binary`), um eine Homebrew-Installation auf dem Zielsystem überflüssig zu machen.

---

## 11. Abhängigkeiten & Lizenzen

| Bibliothek | Lizenz | Verwendung |
|---|---|---|
| Python 3.14 | PSF License | Laufzeitumgebung |
| rumps | BSD 3-Clause | macOS Menüleisten-Framework |
| PyObjC (AppKit, Quartz) | MIT | Native macOS UI, Keyboard-Events |
| ptsl | MIT | Pro Tools Scripting Library (gRPC) |
| pyloudnorm | MIT | EBU R128 Lautheitsmessung |
| soundfile | BSD 3-Clause | WAV-Lesen/Schreiben |
| numpy | BSD 3-Clause | Numerische Verarbeitung |
| scipy | BSD 3-Clause | Signal-Verarbeitung |
| pyusb | stroke/MIT | USB-Kommando-Übertragung an Vocaster |
| bottle | MIT | HTTP Webtrigger Web-Server |
| pywebview | BSD 3-Clause | Web-UI Visualisierung |
| PyInstaller | GPL + Bootloader-Exception | App-Bundle-Erstellung |

Alle Lizenztexte sind im Menü unter **Hilfe & Info → Open Source Software & Lizenzen** abrufbar.
