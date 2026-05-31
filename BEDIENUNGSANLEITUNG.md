# PunchBuddy – Bedienungsanleitung

**Version:** aktuell (Build 2026-05-23)  
**Programmiert von:** Jens Rühl & Christian Becker

---

## Inhaltsverzeichnis

1. [Was ist PunchBuddy?](#1-was-ist-punchbuddy)
2. [Installation](#2-installation)
3. [Erste Einrichtung](#3-erste-einrichtung)
4. [Menü-Übersicht](#4-menü-übersicht)
5. [Punch-In](#5-punch-in)
6. [Play / Stop](#6-play--stop)
7. [Export-Funktionen](#7-export-funktionen)
8. [Interplay Import](#8-interplay-import)
9. [Sequence umbenennen](#9-sequence-umbenennen)
10. [Stream Deck & Webtrigger](#10-stream-deck--webtrigger)
11. [Presets](#11-presets)
12. [Einstellungen](#12-einstellungen)
13. [Vocaster (48 V & Autogain)](#13-vocaster-48-v--autogain)
14. [Fortschrittsanzeige](#14-fortschrittsanzeige)
15. [Watchdog](#15-watchdog)
16. [Häufige Fragen (FAQ)](#16-häufige-fragen-faq)

---

## 1. Was ist PunchBuddy?

PunchBuddy ist eine macOS-Menüleisten-App, die wiederkehrende Abläufe in **Avid Pro Tools** automatisiert. Sie läuft unsichtbar im Hintergrund als kleines Icon in der macOS-Menüleiste und kann über das Menü oder extern über einen **Stream Deck** (oder jedes andere Gerät, das HTTP-Requests senden kann) gesteuert werden.

**Kernfunktionen im Überblick:**

| Funktion | Beschreibung |
|---|---|
| Punch-In A / B | Startet eine Aufnahme mit definierten Spuren in Pro Tools |
| Play / Stop | Steuert die Wiedergabe in Pro Tools |
| Export (komplett) | Führt WAV-, AAF- und Interplay-Export in einem Durchgang aus |
| WAV Export | Exportiert konsolidierte WAV-Dateien |
| AAF Export | Erstellt einen AAF mit eingebettetem Audio |
| Interplay Export | Exportiert direkt in Avid Interplay / NEXIS |
| Interplay Import | Importiert eine Sequence aus Interplay Access in Pro Tools |
| Sequence umbenennen | Benennt die aktuelle Sequence in Interplay Access um |

---

## 2. Installation

### Aus dem DMG

1. Das DMG öffnen (Doppelklick) – z.B. `PunchBuddy_Intel_Release.dmg`
2. Die Datei **`PunchBuddy_Setup.command`** ausführen (Doppelklick)  
   → Kopiert die Apps nach **/Programme**, entfernt die Quarantäne, aktiviert den
   Developer-Mode und führt dich durch das Erteilen der Berechtigungen
3. **`PunchBuddy.app`** starten

### Berechtigungen (wichtig)

PunchBuddy braucht vier macOS-Berechtigungen für **alle drei Apps** (PunchBuddy, Watchdog, Diagnose):

| Berechtigung | Wofür |
|---|---|
| **Bedienungshilfen** (Accessibility) | Tastendrücke an Pro Tools senden (Record, Pre-Roll) |
| **Eingabeüberwachung** (Input Monitoring) | Hotkeys erkennen |
| **Festplattenvollzugriff** (Full Disk Access) | Diagnose, Zugriff auf Session-/Exportpfade |
| **Developer Tools** | Hilfsprozesse (Watchdog) starten dürfen |

> **Ehrlicher Hinweis:** macOS erlaubt es aus Sicherheitsgründen **nicht**, diese vier Schalter per Skript automatisch zu setzen (die System-Datenbank ist durch SIP geschützt). Der Setup-Command nimmt dir alles andere ab und **öffnet die vier Bereiche nacheinander** – du musst nur noch die Schalter für die drei Apps auf EIN stellen. Weil der Setup die Apps vorher einmal startet, erscheinen sie meist schon von selbst in den Listen (sonst mit dem **+**-Knopf aus `/Programme` hinzufügen).

### Enthaltene Apps

| App | Zweck |
|---|---|
| `PunchBuddy.app` | Hauptanwendung (Menüleiste) |
| `PunchBuddy_Watchdog.app` | Überwacht PunchBuddy und startet es bei Absturz neu |
| `PunchBuddy_Diagnose.app` | Diagnose-Tool zur Fehlersuche |
| `PunchBuddy_Setup.command` | Geführte Berechtigungs-Einrichtung (einmalig) |

---

## 3. Erste Einrichtung

Nach dem ersten Start:

1. **Einstellungen öffnen** → Menüleisten-Icon klicken → „Einstellungen…"
2. **Tab „Spurenauswahl"** → Pro Tools muss geöffnet sein, damit die Spuren automatisch geladen werden
3. Spuren für die gewünschten Funktionen (Rec A, Rec B, Export, Lautheit, Play) ankreuzen
4. **Tab „Export-Einstellungen"** → Gewünschte Export-Arten aktivieren und Einstellungen prüfen
5. **Tab „Webtrigger"** → Port und Netzwerk-Interface für den HTTP-Server festlegen
6. Mit **„Speichern"** bestätigen

> **Tipp:** Legen Sie sofort ein Preset an (Tab „Presets" → Konfiguration wählen → „Speichern"), damit Sie schnell zwischen verschiedenen Projekten wechseln können.

---

## 4. Menü-Übersicht

Das Menüleisten-Icon zeigt durch verschiedene Zustände an, was PunchBuddy gerade macht:

| Icon-Zustand | Bedeutung |
|---|---|
| Normal (hell/dunkel) | Bereit |
| Animiert / busy | Aktion läuft (Export, Import etc.) |

**Menüstruktur:**

```
● PunchBuddy
├── Punch-In A starten
├── Punch-In B starten
├── Play
├── ─────────────────
├── Export ▶
│   ├── Export starten (komplett)
│   ├── ─────────────────
│   ├── WAV Export
│   ├── AAF Export
│   ├── Interplay Export
│   ├── ─────────────────
│   └── Sequence umbenennen
├── Interplay Import starten
├── ─────────────────
├── Vocaster ▶            (nur bei angeschlossenem Vocaster)
│   ├── Autogain Host
│   ├── Autogain Guest     (nur Vocaster Two)
│   ├── ─────────────────
│   ├── 48V einschalten
│   └── 48V ausschalten
├── ─────────────────
├── Einstellungen…
├── Log File öffnen
├── Hilfe & Info ▶
│   ├── Bedienungsanleitung
│   ├── Technische Dokumentation
│   ├── ─────────────────
│   ├── Open Source Software & Lizenzen
│   ├── ─────────────────
│   └── Programmiert von: Jens Rühl & Christian Becker
├── ─────────────────
└── Beenden
```

---

## 5. Punch-In

Der Punch-In schaltet in Pro Tools definierte Spuren auf **Record-Ready** und startet die Aufnahme. PunchBuddy verwaltet dabei Pre-Roll und stellt sicher, dass Pro Tools nicht in einen undefinierten Zustand gerät.

### Ablauf

1. Pre-Roll wird auf den eingestellten Zustand gesetzt
2. Die konfigurierten **Rec-Spuren** werden arm-geschaltet
3. Die konfigurierten **Monitor-Spuren** werden auf Input-Monitor gesetzt
4. Pro Tools startet die Aufnahme

### Profil A vs. Profil B

- **Profil A** (`/trigger`): Standardkonfiguration — eigene Spurliste (Rec A) und Monitor-Liste
- **Profil B** (`/trigger2`): Alternative Konfiguration — eigene Spurliste (Rec B) und Monitor-Liste  
  → Profil B ist nützlich, wenn Sie z.B. für verschiedene Sprecher unterschiedliche Spuren arm-schalten

> **Wichtig:** Wenn für Profil B keine Spuren definiert sind, wird der Trigger ignoriert und eine Meldung im Log ausgegeben.

### Monitor-Spuren (mon_a / mon_b)

Monitor-Spuren werden **nur über Presets** konfiguriert. Sie sind nicht direkt in der Spurenauswahl sichtbar, werden aber beim Laden eines Presets aktiv.

---

## 6. Play / Stop

| Trigger | Funktion |
|---|---|
| `/play` | Play/Stop Toggle — startet Wiedergabe, oder stoppt wenn schon läuft |
| `/stop` | Stoppt Pro Tools dediziert (auch wenn nicht im Play-Modus) |

Diese Trigger sind nützlich, wenn Sie die Wiedergabe ohne Punch-In starten möchten, z.B. um Material im Schnitt zu kontrollieren.

---

## 7. Export-Funktionen

### 7.1 Export (komplett)

Führt alle aktivierten Export-Arten **in einem Durchgang** aus:

1. Lautheitskorrektur (wenn aktiviert)
2. WAV Export (wenn aktiviert)
3. AAF Export (wenn aktiviert)
4. Interplay Export (wenn aktiviert)

→ Alle Sub-Exporte können in den Einstellungen einzeln ein- oder ausgeschaltet werden.

### 7.2 WAV Export

**Ablauf:**
1. Lautheitskorrektur der definierten Lautheitsspuren (EBU R128)
2. Consolidate der Export-Spuren ab dem definierten Start-Timecode
3. Trim der Überhänge (Anfang/Ende)
4. WAV-Dateien werden in den Session-Ordner kopiert

**Fortschrittsanzeige:** Schwebend oben rechts auf dem Bildschirm, ohne Fokus zu stehlen.

### 7.3 AAF Export

**Ablauf:**
1. Lautheitskorrektur (EBU R128)
2. Consolidate der Export-Spuren
3. Trim der Überhänge
4. AAF mit eingebettetem Audio wird im Session-Ordner erstellt

### 7.4 Interplay Export

Der komplexeste Export-Workflow. PunchBuddy steuert **Avid Interplay Access** vollautomatisch:

**Ablauf:**
1. Lautheitskorrektur (EBU R128)
2. Consolidate der Export-Spuren
3. Trim der Überhänge
4. DMGs auswerfen (falls vorhanden)
5. Export Comment Dialog in Pro Tools bedienen
6. Export Options konfigurieren (Workspace-Auswahl per Pfeiltasten)
7. Export starten und Ergebnis via PT-Log überwachen
8. Bestätigungsdialog bestätigen (kqueue-basiert — kein blindes Warten)
9. Optional: Sequence in Interplay Access umbenennen

**Fortschrittsanzeige:** ~14 Zwischenstände mit Fortschrittsbalken.

### 7.5 Erkennung von Erfolg und Fehler

PunchBuddy liest das Pro Tools Log (`DigiTrace.log`) aus und erkennt anhand konfigurierbarer Schlüsselwörter, ob der Export erfolgreich war oder fehlgeschlagen ist. Beide Listen sind in den Einstellungen anpassbar.

---

## 8. Interplay Import

Importiert eine Sequence aus **Avid Interplay Access** in eine geöffnete Pro Tools Session.

**Ablauf:**
1. Optional: Aktuelle PT-Session speichern und schließen
2. Sequenzname aus Interplay Access kopieren (F2 → Cmd+A → Cmd+C)
3. „Send to Pro Tools" in Interplay Access ausführen (Cmd+Shift+P)
4. Offline-Dialog in PT: Session-Pfad einfügen
5. Import Session Data → Match All → Preset 1

**Voraussetzungen:**
- Interplay Access muss geöffnet sein
- Die gewünschte Sequence muss in Interplay Access ausgewählt sein

---

## 9. Sequence umbenennen

Benennt die aktuell in **Interplay Access** ausgewählte Sequence gemäß den Einstellungen unter „Rename Sequence" um.

**Transformation des Namens:**
1. Zeichen am **Anfang** löschen (konfigurierbar, Standard: 0)
2. Zeichen am **Ende** löschen (konfigurierbar, Standard: 0)
3. **Präfix** voranstellen (konfigurierbar, Standard: leer)
4. **Suffix** anhängen (konfigurierbar, Standard: leer)

> **Beispiel:** Name = `PR_Beitrag_2024`, Trim Start = 3, Suffix = `_FERTIG`  
> Ergebnis: `Beitrag_2024_FERTIG`

---

## 10. Stream Deck & Webtrigger

PunchBuddy startet einen lokalen HTTP-Server und kann über einfache GET-Requests ferngesteuert werden — z.B. vom **Elgato Stream Deck** über das „Website"-Plugin oder von einem Skript.

### Verfügbare Endpunkte

| URL | Funktion |
|---|---|
| `http://[IP]:[Port]/trigger` | Punch-In A |
| `http://[IP]:[Port]/trigger2` | Punch-In B |
| `http://[IP]:[Port]/play` | Play/Stop (Toggle) |
| `http://[IP]:[Port]/stop` | Stop (dediziert) |
| `http://[IP]:[Port]/export` | Export (komplett) |
| `http://[IP]:[Port]/export_wav` | WAV Export |
| `http://[IP]:[Port]/export_aaf` | AAF Export |
| `http://[IP]:[Port]/export_interplay` | Interplay Export |
| `http://[IP]:[Port]/import` | Interplay Import |
| `http://[IP]:[Port]/vocaster/autogain/host` | Vocaster: Autogain Host starten |
| `http://[IP]:[Port]/vocaster/autogain/guest` | Vocaster: Autogain Guest starten (nur Vocaster Two) |
| `http://[IP]:[Port]/vocaster/phantom/on` | Vocaster: 48 V einschalten |
| `http://[IP]:[Port]/vocaster/phantom/off` | Vocaster: 48 V ausschalten |

**Standard:** `http://127.0.0.1:8899/trigger`  
→ Alle URLs werden im Webtrigger-Tab der Einstellungen angezeigt und können per Klick kopiert werden. Die Vocaster-URLs erscheinen zusätzlich im **Vocaster-Tab** (nur wenn ein Gerät angeschlossen ist).

### Netzwerk-Interface

Standardmäßig lauscht der Server nur auf `127.0.0.1` (Localhost — nur der eigene Rechner kann zugreifen). Wenn ein **Stream Deck oder ein anderes Gerät im Netzwerk** PunchBuddy steuern soll:

1. Einstellungen öffnen → Tab „Webtrigger"
2. Unter „Netzwerk-Interface" das gewünschte Interface wählen (z.B. `en0 (192.168.1.42)`)
3. „Speichern" → HTTP-Server startet neu und ist im Netzwerk erreichbar
4. Die angezeigten URLs nutzen (z.B. `http://192.168.1.42:8899/trigger`)

> **Sicherheitshinweis:** Wenn ein Netzwerk-Interface gewählt ist, können alle Geräte im selben Netzwerk PunchBuddy steuern. Nutzen Sie dies nur in vertrauenswürdigen Netzwerken.

---

## 11. Presets

Presets speichern **alle PunchBuddy-Einstellungen** in einem benannten Slot und ermöglichen schnelles Wechseln zwischen verschiedenen Projekten oder Arbeitsmodi.

### Was wird gespeichert?

- Spurzuweisungen (Rec A, Rec B, Export, Lautheit, Play)
- Monitor-Spuren (Trigger A Mon, Trigger B Mon)
- Alle Export-Einstellungen (WAV, AAF, Interplay, Lautheitskorrektur)
- Interplay Workspace, Fehler-/Erfolgs-Keywords, Umbenennung
- Import-Einstellungen
- Webtrigger-Port und Netzwerk-Interface

### Bedienung (Tab „Presets")

| Aktion | Funktion |
|---|---|
| **Laden** | Übernimmt alle Einstellungen des gewählten Presets in die UI |
| **Speichern** | Speichert den aktuellen Stand aller Einstellungen im gewählten Preset |
| **Umbenennen** | Ändert den Namen des gewählten Presets |
| **Neu** | Erstellt ein neues leeres Preset |
| **Löschen** | Löscht das gewählte Preset (mindestens 1 Preset bleibt erhalten) |

> **Hinweis:** „Laden" übernimmt die Einstellungen nur in die UI. Erst wenn Sie anschließend „Speichern" (den Hauptspeichern-Button unten rechts) drücken, werden die Einstellungen aktiv gespeichert und angewendet.  
> **Ausnahme:** Monitor-Spuren werden direkt beim Laden in die internen Einstellungen übernommen.

---

## 12. Einstellungen

Die Einstellungen werden über **Menüleiste → Einstellungen…** geöffnet. Das Fenster hat fünf Tabs (plus einen sechsten **Vocaster**-Tab, sobald ein Vocaster angeschlossen ist):

### Tab 1: Spurenauswahl

Zeigt alle Spuren der aktuell geöffneten Pro Tools Session. Für jede Spur können folgende Rollen aktiviert werden:

| Spalte | Funktion |
|---|---|
| **Trigger A** | Spur wird bei Punch-In A arm-geschaltet (Record) |
| **Trigger B** | Spur wird bei Punch-In B arm-geschaltet (Record) |
| **Export** | Spur wird in alle Export-Vorgänge einbezogen |
| **Loud** | Spur wird für Lautheitskorrektur (EBU R128) berücksichtigt |
| **Play** | Spur wird beim Play-Trigger auf Input-Monitor gesetzt |

> **Monitor-Spuren (Mon A/Mon B):** Diese Einstellungen sind nicht mehr direkt in der Spurenauswahl sichtbar. Sie werden über **Presets** gespeichert und geladen.

### Tab 2: Export-Einstellungen

Alle Parameter für den Export-Workflow. Details → [Technische Dokumentation](TECHNISCHE_DOKUMENTATION.md).

### Tab 3: Import

Einstellungen für den Interplay Import.

### Tab 4: Webtrigger

HTTP-Server Port und Netzwerk-Interface, plus Übersicht aller Trigger-URLs zum Kopieren.

### Tab 5: Presets

Preset-Verwaltung (Laden, Speichern, Umbenennen, Neu, Löschen).

### Tab 6: Vocaster

> Dieser Tab erscheint **nur**, wenn ein Focusrite **Vocaster One** oder **Vocaster Two** angeschlossen ist.

| Element | Funktion |
|---|---|
| **Erkanntes Gerät** | Zeigt das gefundene Modell (z.B. „Vocaster Two") |
| **48 V beim Start einschalten** | Schaltet die Phantomspeisung automatisch ein, sobald PunchBuddy startet |
| **Autogain-Webtrigger** | Fertige URLs für Autogain Host/Guest und 48 V ein/aus, je mit Kopieren-Knopf |

---

## 13. Vocaster (48 V & Autogain)

PunchBuddy kann einen angeschlossenen **Focusrite Vocaster One/Two** direkt steuern – die separate **Vocaster Hub** App wird dafür nicht mehr benötigt. PunchBuddy übernimmt die USB-Steuerung selbst.

### 48 V Phantomspeisung

- **Automatisch beim Start:** Einstellungen → Tab „Vocaster" → „48 V beim Start einschalten" aktivieren.
- **Manuell:** Menü → „Vocaster" → „48 V einschalten / ausschalten", oder per Webtrigger `/vocaster/phantom/on` bzw. `/off`.

### Autogain (Mikrofon automatisch pegeln)

1. Autogain auslösen – per **Stream Deck** (`/vocaster/autogain/host`), per **Menü** („Vocaster → Autogain Host") oder für den Gast-Eingang `…/guest` (nur Vocaster Two).
2. Es erscheint ein Fenster **„Das automatische Pegeln vom Mikrofon wurde gestartet."** mit Fortschrittsbalken.
3. Jetzt **ca. 10 Sekunden** mit der Vertonungslautstärke ins Mikrofon sprechen.
4. Das Fenster zeigt kurz das Ergebnis (z.B. „Pegeln erfolgreich") und **schließt sich von selbst**.

> **Hinweis:** Beim ersten Vocaster-Zugriff beendet PunchBuddy automatisch die „Vocaster Hub"-App, falls sie läuft (sie würde sonst die USB-Verbindung blockieren).

---

## 14. Fortschrittsanzeige

Während Export- und Import-Vorgängen zeigt PunchBuddy ein **schwebendes Fenster** oben rechts auf dem Bildschirm:

- **Stiehlt keinen Fokus** — Pro Tools und andere Apps bleiben bedienbar
- **Click-through** — Das Fenster blockiert keine Mausklicks
- Zeigt den aktuellen Schritt als Text und Fortschrittsbalken
- Schließt sich automatisch nach Abschluss oder Fehler

---

## 15. Watchdog

`PunchBuddy_Watchdog.app` läuft im Hintergrund und überwacht die Hauptanwendung. Falls PunchBuddy abstürzt oder nicht mehr reagiert, startet der Watchdog es automatisch neu.

**Verhalten beim App-Start:**
- PunchBuddy beendet beim Start automatisch eine ggf. noch laufende Watchdog-Instanz
- Startet danach einen neuen Watchdog

**Beim Beenden:**
- PunchBuddy beendet den Watchdog sauber, bevor es sich selbst schließt

---

## 16. Häufige Fragen (FAQ)

**PunchBuddy reagiert nicht auf Stream Deck / externe HTTP-Requests.**  
→ Prüfen Sie im Webtrigger-Tab ob das richtige Netzwerk-Interface gewählt ist. Bei Zugriff von einem anderen Gerät muss ein LAN-Interface (nicht Localhost) gewählt sein.

**Punch-In startet nicht.**  
→ Stellen Sie sicher dass Pro Tools geöffnet ist und eine Session geladen ist. Prüfen Sie ob für Trigger A Spuren in der Spurenauswahl definiert sind.

**Interplay Export schlägt fehl.**  
→ Interplay Access muss geöffnet und mit dem NEXIS verbunden sein. Prüfen Sie den Workspace-Namen und die Workspace-Steps in den Export-Einstellungen.

**Das Fortschrittsfenster erscheint nicht.**  
→ Stellen Sie sicher, dass Accessibility-Berechtigungen für PunchBuddy in den macOS Systemeinstellungen aktiviert sind.

**Monitor-Spuren werden nach einem Update nicht mehr angezeigt.**  
→ Monitor-Spuren sind jetzt nur noch in Presets gespeichert. Laden Sie ein Preset, das die korrekten Monitor-Spuren enthält.

**Wie sehe ich was PunchBuddy gemacht hat?**  
→ Menü → „Log File öffnen" zeigt das detaillierte Log mit Zeitstempeln aller Aktionen.
