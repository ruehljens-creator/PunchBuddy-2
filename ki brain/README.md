# 🧠 KI Brain — PunchBuddy Wissensbasis

Dieser Ordner ist die **lebende Wissensbasis** für PunchBuddy. Er enthält **alle
Erkenntnisse, Ursachenanalysen und Fehlerbehebungen** — damit Wissen nicht
verloren geht und nicht „aus dem Gedächtnis" rekonstruiert werden muss.

## Regeln für die Pflege
- **Jede** belastbare Erkenntnis und **jede** Fehlerbehebung kommt hier rein.
- Aussagen mit **Quelle/Beleg** (URL, Datei:Zeile, Log-Zeitstempel) versehen.
- Vermutungen klar von Bewiesenem trennen.
- Datumsangaben absolut (z. B. „2026-06-30"), keine relativen.

## Inhalt
| Datei | Inhalt |
|---|---|
| [erkenntnisse.md](erkenntnisse.md) | Bestätigte Ursachen, Architektur-Fakten, recherchierte Belege (mit Quellen) |
| [fehlerbehebungen.md](fehlerbehebungen.md) | Chronologisches Log aller Fixes (commit-verknüpft) |
| [offene_punkte.md](offene_punkte.md) | Bekannte, noch nicht behobene Schwachstellen / To-Dos |

## Schnellzugriff: wichtigste Erkenntnis (Stand 2026-06-30)
Die „Trägheit / 15 s Verzögerung / Beachball" bei Stream-Deck-Befehlen ist **nicht
PunchBuddy-intern**. Pro Tools steuert seine **interne Video-Engine als Video-
Satellite über TCP/IP** (Satellite-Link-Clock-Sync bei jedem Transport-Befehl) und
bindet diesen Sync an die **geroutete Firmen-/NEXIS-NIC** statt an Loopback →
Firmen-Firewall/Cisco verzögert/dropt den Clock-Lock → Transport-Stall. Details in
[erkenntnisse.md](erkenntnisse.md).
