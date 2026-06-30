# Fehlerbehebungen

> Chronologisches Log aller Fixes. Neueste zuerst.

---

## 2026-06-30 — Robustheit gegen schnelle Befehle + Zombie-PTSL-Verbindungen

Auslöser: Stream-Deck-Befehle (schnell/doppelt gefeuert) überfuhren Pro Tools →
Beachball, „Cannot invoke RPC on closed channel", roter Punkt dauerhaft rot.
Multi-Agent-Code-Audit (verifiziert) fand 5 bestätigte High-Severity-Probleme.

### Fix A — Trigger-Debounce für ALLE triggerbaren Aktionen
- `auto_punch_in.py`: neuer thread-sicherer `_debounce_ok(key, interval)`;
  identische Trigger innerhalb des Intervalls werden verworfen.
- Angewandt auf: record_a/b, play, play_custom, goto_start, move_audio, import,
  alle 4 Exporte (interval 1.0 s), Vocaster-Webtrigger, Preset-Webtrigger.
- Wirkt für **Menü UND Webtrigger**, da beide durch die `_trigger_*`-Methoden
  bzw. die HTTP-Handler laufen.
- Fängt Stream-Deck-Doppel-Requests und Hämmern ab, bevor überlappende Threads
  PT überfahren.

### Fix B — Export-Serialisierung (gemeinsamer Slot über alle Export-Arten)
Problem (Audit #1/#4): Die Standalone-Exporte (WAV/AAF/AAF-Ref/Interplay) hatten
KEINEN Lauf-Guard (nur `_set_busy`). Der 1 s-Debounce verhindert nur
Schnell-Doppelklicks, nicht überlappende Langläufe oder Misch-Sequenzen
(z. B. WAV dann AAF) → parallele Consolidate-Läufe auf dem geteilten gRPC-Kanal.
- `auto_punch_in.py`: `_begin_export_or_skip()` (gemeinsamer `state.export_running`
  + `_export_lock`-Gate) und `_spawn_export(target, *args)` (Worker-Wrapper, der
  den Slot im `finally` IMMER freigibt). Alle 4 Export-Trigger nutzen das jetzt.
- Damit: nie zwei Exporte gleichzeitig; Slot wird auch bei Exception freigegeben.

### Fix C — Zombie-PTSL-Verbindungen verhindern (Audit #2)
Problem: Rohe `engine.*`-Aufrufe laufen NICHT über `_ptsl_call`, daher löst ein
toter Channel KEIN `_reset_engine()` aus → tote Singleton-Engine bleibt, nächster
Befehl trifft denselben toten Channel (CLOSE_WAIT-Zombie).
- `engine.py`: neuer Helper `_reset_if_rpc_error(e)` (verwirft Engine bei
  `grpc.RpcError`).
- `export.py`: `_detect_video_track` läuft jetzt über `_ptsl_call`; in den
  äußeren `except`-Blöcken aller 6 Export-/Import-Funktionen
  (Import, Interplay-Export, run_export, WAV, AAF, AAF-Ref) wird
  `_reset_if_rpc_error(e)` aufgerufen → toter Channel wird verworfen.

### Fix D — App-Quit schließt die Engine immer (Audit #3)
Problem: SIGTERM/SIGINT (z. B. Watchdog-Kill) schloss die Engine nicht →
gRPC-Channel blieb auf PT-Seite in CLOSE_WAIT, tote Verbindungen sammelten sich.
- `auto_punch_in.py`: `_signal_handler` ruft jetzt `_close_engine()` vor dem Exit;
  zusätzlich `atexit.register(_close_engine)`. Idempotent über `_engine_lock`.

### Fix E — Singleton-Engine-Race entschärft (Audit #5, Teil)
Problem: `_get_engine()` gibt die Referenz lock-frei heraus; ein paralleler
Thread kann die Instanz ersetzen/schließen, während ein Worker sie noch benutzt
→ verwaiste, nie geschlossene Engine (CLOSE_WAIT) bzw. „closed channel".
- `engine.py`: `_reset_engine(stale=None)` — schließt nur, wenn `stale` noch die
  aktuelle Instanz ist (Identitäts-Check). `_ptsl_call` übergibt
  `stale=getattr(fn, "__self__", None)` (greift bei gebundenen Methoden).
- `transport.py`: Anti-Stau-Pfad nutzt `_reset_engine(stale=engine)` statt
  `_close_engine()` → kein Wegschließen einer fremden, evtl. frischen Instanz.
- Test `tests/test_core.py` an neue Signatur angepasst (Mock-Lambda `stale=None`).
- **Hinweis:** Die VOLLE Lösung (getter-basiertes `_ptsl_call`, das die Instanz
  selbst holt; alle rohen `engine.*` über `_ptsl_call`) steht in
  [offene_punkte.md](offene_punkte.md). Lambdas umgehen den Identitäts-Check noch.

### Fix F — Diagnose-Script erweitert
`collect_diagnostics.py` Sektionen 10–16: Netzwerk (Interfaces/Service-Order/
Routing/DNS), PT-Satellite/PTSL-Ports (28282/28284/31416/8899, CLOSE_WAIT,
AvidVideoEngine, Satellite-Marker im PT-Log), Microsoft Defender (mdatp health,
system_extensions, Prozesse), System-Extensions (systemextensionsctl), macOS
Firewall (socketfilterfw/alf/pf), Stream Deck (Prozess/Version/Logs/Login-Items),
volle Top-CPU-Prozessliste + Sicherheits-Dritt-Software.

Verifikation: `ast.parse` OK, `import auto_punch_in` OK, 19/19 Tests grün,
Diagnose-Script läuft.

---

## Frühere Fixes (commit-verknüpft, Auszug)
- `cd2eab6` Audio-Move: Dropdown-Auswahl im Import-Tab + Webtrigger.
- `43ae70e` Audio-Move: clip-basiert pro Spur (Subframe-Fix), via
  `_protools_selection_context` (Link Timeline/Track + Selector-Tool).
- `2049435` Pre-Export-Check `_ensure_transport_stopped` (stoppt PT vor Export).
- `0715c2b` Crash-Fix: `_dispatch_main` → `AppHelper.callAfter` (threadsicher),
  faulthandler + excepthooks; Consolidate-Fenster per `startswith`; tote
  SendDialogOnScreen-Marker ersetzt.
- `19b8fe5`/`4b7306b` Doku-Links repariert, Doku gebündelt.
