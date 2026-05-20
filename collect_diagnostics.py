#!/usr/bin/env python3
"""
PunchBuddy Diagnose-Script
============================
Führe dieses Script auf dem Studio-Rechner aus:
    python3 collect_diagnostics.py

Das Script sammelt alle relevanten Logs und speichert sie in:
    ~/Desktop/PunchBuddy_Diagnostics.txt

Den Inhalt dieser Datei dann hier einfügen.
"""

import os, sys, subprocess, glob, platform, datetime, shutil

OUT = os.path.expanduser("~/Desktop/PunchBuddy_Diagnostics.txt")
SEP = "\n" + "="*80 + "\n"

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"[Fehler: {e}]"

def read_tail(path, lines=200):
    """Liest die letzten N Zeilen einer Datei."""
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception as e:
        return f"[Lesefehler: {e}]"

def read_grep(path, patterns, lines=300):
    """Liest alle Zeilen einer Datei die einem der Patterns entsprechen."""
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        results = []
        pats = [p.lower() for p in patterns]
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(p in line.lower() for p in pats):
                    results.append(line)
        if not results:
            return "[Keine passenden Einträge gefunden]"
        return "".join(results[-lines:])
    except Exception as e:
        return f"[Lesefehler: {e}]"

def newest_file(pattern):
    """Findet die neueste Datei die dem Glob-Pattern entspricht."""
    files = glob.glob(os.path.expanduser(pattern))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

lines = []
lines.append("=" * 80)
lines.append(f"  PunchBuddy Diagnose-Report")
lines.append(f"  Erstellt: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append("=" * 80)

# ── 1. System-Info ─────────────────────────────────────────────────────────
lines.append(SEP + "1. SYSTEM-INFO")
lines.append(f"macOS:   {platform.mac_ver()[0]}")
lines.append(f"Python:  {sys.version}")
lines.append(f"Rechner: {platform.node()}")
lines.append(f"\nInstallierte Python-Pakete (relevant):")
pkgs = run("pip3 list 2>/dev/null | grep -i 'ptsl\\|rumps\\|pynput\\|pyobjc\\|grpc\\|protobuf'")
lines.append(pkgs or "[keine gefunden]")

# ── 2. PunchBuddy Log (komplett, neueste 300 Zeilen) ──────────────────────
lines.append(SEP + "2. PUNCHBUDDY LOG  (~/.punchbuddy/PunchBuddy.log) – letzte 300 Zeilen")
log_path = os.path.expanduser("~/.punchbuddy/PunchBuddy.log")
lines.append(read_tail(log_path, 300))

# ── 3. Pro Tools Log (neueste Datei, gefiltert) ────────────────────────────
lines.append(SEP + "3. PRO TOOLS LOG (~/Library/Logs/Avid/Pro_Tools_*.txt) – gefiltert")
pt_log = newest_file("~/Library/Logs/Avid/Pro_Tools_*.txt")
if pt_log:
    lines.append(f"Datei: {pt_log}")
    lines.append(f"Größe: {os.path.getsize(pt_log):,} Bytes")
    lines.append("\n--- Relevante Einträge (PTSL, Record, Input, Error, Lock, Timeout) ---")
    patterns = ["ptsl", "record", "inputmon", "input_mon", "input mon",
                "setrecord", "set_record", "transport", "error", "lock",
                "timeout", "busy", "waiting", "grpc", "0e28"]
    lines.append(read_grep(pt_log, patterns, lines=400))
    lines.append("\n--- Letzte 100 Zeilen des PT Logs (Kontext) ---")
    lines.append(read_tail(pt_log, 100))
else:
    lines.append("[Keine Pro Tools Log-Datei gefunden – Pro Tools muss nach Session beendet werden]")

# ── 3b. Interplay / MediaCentral Export-Analyse ───────────────────────────
lines.append(SEP + "3b. INTERPLAY / MEDIACENTRAL EXPORT – PT Log-Analyse")
if pt_log:
    lines.append("--- Interplay/Export-Einträge im PT Log ---")
    export_patterns = [
        "interplay", "mediacentral", "media central", "nexis", "mpm",
        "media production", "export", "progress:", "dfw:", "send",
        "transfer", "avid link", "avidlink", "publish", "complete",
        "success", "fail", "error", "dialog", "alert", "notification"
    ]
    lines.append(read_grep(pt_log, export_patterns, lines=500))
else:
    lines.append("[Kein PT Log gefunden]")

lines.append("\n--- Avid Link / AvidControlDesktop Logs ---")
avid_link_log = newest_file("~/Library/Logs/Avid/AvidControlDesktop/*.txt")
if avid_link_log:
    lines.append(f"Datei: {avid_link_log}")
    lines.append(read_tail(avid_link_log, 100))
else:
    lines.append("[Kein AvidControlDesktop Log gefunden]")

lines.append("\n--- macOS Unified Log: Avid/Interplay Prozesse (letzte 30 Min) ---")
unified_avid = run(
    "log show --predicate '"
    "process CONTAINS \"Avid\" OR process CONTAINS \"Interplay\" "
    "OR process CONTAINS \"MediaCentral\" OR process CONTAINS \"AvidLink\" "
    "OR eventMessage CONTAINS \"interplay\" OR eventMessage CONTAINS \"mediacentral\"' "
    "--last 30m --style compact 2>/dev/null | tail -150",
    timeout=30
)
lines.append(unified_avid or "[Keine Einträge oder Berechtigung fehlt]")

# ── 4. Alle Pro Tools Logs auflisten ──────────────────────────────────────
lines.append(SEP + "4. VERFÜGBARE PRO TOOLS LOG-DATEIEN")
lines.append(run("ls -lth ~/Library/Logs/Avid/Pro_Tools_*.txt 2>/dev/null | head -10"))

# ── 5. macOS Console Logs (Pro Tools / PTSL) ──────────────────────────────
lines.append(SEP + "5. macOS CONSOLE LOGS – Pro Tools (letzte 30 Min)")
# log show braucht evtl. sudo, probieren wir es trotzdem
console_out = run(
    "log show --predicate 'process == \"Pro Tools\" OR subsystem CONTAINS \"ptsl\" "
    "OR eventMessage CONTAINS \"PTSL\" OR eventMessage CONTAINS \"InputMonitor\"' "
    "--last 30m --style syslog 2>/dev/null | tail -100",
    timeout=20
)
lines.append(console_out or "[Keine Console-Logs verfügbar oder Berechtigung fehlt]")

# ── 6. PTSL Server Prozess ────────────────────────────────────────────────
lines.append(SEP + "6. PTSL / PRO TOOLS PROZESS-INFO")
lines.append("Laufende Prozesse:")
lines.append(run("ps aux | grep -i 'pro tools\\|ptsl\\|ProTools' | grep -v grep"))
lines.append("\nOffene Netzwerk-Ports (PTSL = 31416):")
lines.append(run("lsof -i :31416 2>/dev/null || netstat -an 2>/dev/null | grep 31416"))

# ── 7. Crash Reports ──────────────────────────────────────────────────────
lines.append(SEP + "7. PRO TOOLS CRASH REPORTS (letzte 3)")
crash_dirs = [
    "~/Library/Logs/DiagnosticReports/",
    "~/Library/Application Support/Avid/Pro Tools/Crashpad/",
]
found_crashes = []
for d in crash_dirs:
    pattern = os.path.expanduser(d) + "/**/*Pro*Tools*"
    found = glob.glob(pattern, recursive=True)
    found_crashes.extend(found)
found_crashes = sorted(found_crashes, key=os.path.getmtime, reverse=True)[:3]
if found_crashes:
    for cf in found_crashes:
        lines.append(f"\n--- {cf} ---")
        lines.append(read_tail(cf, 50))
else:
    lines.append("[Keine Pro Tools Crash-Reports gefunden]")

# ── 8. Settings ───────────────────────────────────────────────────────────
lines.append(SEP + "8. PUNCHBUDDY EINSTELLUNGEN")
settings_path = os.path.expanduser("~/.punchbuddy/settings.json")
lines.append(read_tail(settings_path, 50))

# ── 9. Disk-Info (Schreibgeschwindigkeit relevant für PT-Cleanup-Zeit) ─────
lines.append(SEP + "9. DISK INFO")
lines.append(run("df -h ~ 2>/dev/null"))
lines.append(run("diskutil info / 2>/dev/null | grep -i 'solid state\\|medium type\\|volume name\\|file system'"))

# ── Speichern ─────────────────────────────────────────────────────────────
output = "\n".join(lines)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(output)

print(f"\n✅ Diagnose gespeichert: {OUT}")
print(f"   Größe: {os.path.getsize(OUT):,} Bytes")
print(f"\nBitte den Inhalt von '{OUT}' hier einfügen.\n")

# Optional: direkt im Finder öffnen
try:
    subprocess.run(["open", "-R", OUT])
except:
    pass
