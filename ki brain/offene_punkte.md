# Offene Punkte / To-Dos

> Bekannte, noch nicht (vollständig) behobene Schwachstellen mit konkretem Plan.

---

## 1. Singleton-Engine-Race — VOLLE Lösung ausstehend
**Stand:** Teil-Fix umgesetzt (Identitäts-Check in `_reset_engine(stale=...)`,
`_ptsl_call` übergibt `fn.__self__`). **Lücke:** Viele Aufrufer übergeben
**Lambdas** (`lambda: engine.xxx(...)`) → `__self__` fehlt → Identitäts-Check
greift dort nicht; eine zwischenzeitlich ersetzte Instanz kann weiterbenutzt
werden.

**Plan (invasiver, gründlich testen):**
1. `_ptsl_call` holt die Engine SELBST via `_get_engine()` (oder bekommt einen
   `getter`) und übergibt genau diese Instanz an `_reset_engine(stale=...)`.
2. Aufrufer auf `_ptsl_call(lambda e: e.transport_state(), ...)` umstellen
   (Instanz wird injiziert), statt eine gebundene Methode einer alten lokalen
   `engine`-Variable zu cachen.
3. Damit arbeitet jeder Call immer auf der aktuell gültigen Instanz; verwaiste
   Instanzen entstehen nicht mehr.

## 2. Alle rohen `engine.*`-Aufrufe über `_ptsl_call` leiten
**Stand:** Anti-Zombie-Reset in den äußeren `except`-Blöcken ergänzt (Fix C) +
`_detect_video_track` umgestellt. **Lücke:** In `run_export` /
`run_interplay_export` / `run_*_standalone` / `_protools_selection_context` /
`_trim_overhangs` gibt es noch ~40 rohe `engine.*`-Aufrufe (z. B.
`session_path`, `select_tracks_by_name`, `consolidate_clip`, `set_edit_tool` …).
Sie brechen bei totem Channel zwar ab und lösen jetzt im äußeren `except` einen
Reset aus, laufen aber NICHT serialisiert über `_ptsl_lock` und ohne saubere
`(ok, res)`-Auswertung pro Call.

**Plan:** Schrittweise jeden rohen `engine.*`-Aufruf in `export.py` durch
`ok, res = _ptsl_call(engine.xxx, args…, label=…, timeout=…)` ersetzen, mit
Abbruch bei `not ok`. Liste der Stellen siehe Audit-Befund #2.

## 3. `_set_busy` auf Refcount umstellen (optional)
Aktuell Bool-Setter. Falls künftig Nicht-Export-Worker mit Export überlappen,
kann der rote Punkt falsch gelöscht werden. Refcount in `transport.py`
(`_set_busy(True/False)` zählt hoch/runter) macht es robust. Für die aktuelle
Export-Serialisierung NICHT zwingend (nie zwei Export-Worker gleichzeitig).

## 4. Pro-Tools-/Studio-seitig (kein PunchBuddy-Code)
- Avid Video Engine / Satellite-Link-Bindung an die Firmen-/NEXIS-NIC abstellen
  (siehe [erkenntnisse.md](erkenntnisse.md) §1). **Eigentliche Wurzel der
  Trägheit/Beachballs.**
- Defender-Netzwerk-Extension am Studiorechner prüfen/ausnehmen (mdatp-Befehle
  in der erweiterten Diagnose / erkenntnisse.md).

## 5. Verifikation am Studiorechner ausstehend
Die neuen Fixes (Debounce, Export-Slot, Zombie-Resets) müssen unter realer
Stream-Deck-Last am Studiorechner getestet werden. Erwartung: keine
überlappenden Trigger mehr im Log, kein dauerhaft roter Punkt, keine
CLOSE_WAIT-Anhäufung. Mit der erweiterten Diagnose (Sektionen 10–16)
gegenprüfen.

## 6. Stream-Deck-Plugin im echten Stream Deck testen
Das Node-Plugin (`streamdeck/plugin/`) wurde nur gegen den echten Socket
end-to-end verifiziert, **nicht** im echten Stream Deck geladen. Am Zielrechner
prüfen: Plugin installiert sich, Aktion erscheint, Manifest/Icons/Node-Runtime
(Stream Deck ≥ 6.5) ok, Tastendruck zeigt ✓. Fallback ohne jegliche Installation:
`.app`-Launcher via `make_launchers.command` + SD-Aktion „System → Öffnen".

## 7. Unix-Socket-Pfad-Länge (macOS-Limit)
`AF_UNIX` erlaubt unter macOS max. 104 Zeichen. Default `/tmp/punchbuddy.sock`
ist unkritisch; ein vom Nutzer gesetzter sehr langer `unix_socket_path` scheitert
(wird sauber geloggt, kein Crash). Bei Bedarf in der Einstellungs-Validierung
abfangen.
