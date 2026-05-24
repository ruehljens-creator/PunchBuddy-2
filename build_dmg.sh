#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Alte Build-Artefakte entfernen ==="
mv build /tmp/old_build_$$ 2>/dev/null || true
mv dist /tmp/old_dist_$$ 2>/dev/null || true
mv PunchBuddy.dmg /tmp/old_dmg_$$ 2>/dev/null || true
mv PunchBuddy.spec /tmp/old_spec_$$ 2>/dev/null || true
mv Diagnose.spec /tmp/old_diag_$$ 2>/dev/null || true
export PYINSTALLER_CONFIG_DIR="/tmp/pyi_config_$$"

# ── 1. Haupt-App bauen ────────────────────────────────────────────────────
echo "=== PyInstaller Build: PunchBuddy ==="
python3 -m PyInstaller \
  --name="PunchBuddy" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap" \
  --distpath="/tmp/dist_ap" \
  --specpath="/tmp/spec_ap" \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --add-data="$SCRIPT_DIR/PunchBuddy.png:." \
  --hidden-import=Foundation \
  --hidden-import=AppKit \
  --hidden-import=objc \
  --hidden-import=Quartz \
  --hidden-import=rumps \
  --hidden-import=ptsl \
  --hidden-import=http.server \
  --hidden-import=socketserver \
  --hidden-import=pyloudnorm \
  --hidden-import=soundfile \
  --hidden-import=numpy \
  auto_punch_in.py

# ── 2. Diagnose-App bauen ─────────────────────────────────────────────────
echo "=== PyInstaller Build: PunchBuddy Diagnose ==="
python3 -m PyInstaller \
  --name="PunchBuddy_Diagnose" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap" \
  --distpath="/tmp/dist_ap" \
  --specpath="/tmp/spec_ap" \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  collect_diagnostics.py

# ── 2b. Watchdog-App bauen ────────────────────────────────────────────────
echo "=== PyInstaller Build: Watchdog ==="
python3 -m PyInstaller \
  --name="PunchBuddy_Watchdog" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap" \
  --distpath="/tmp/dist_ap" \
  --specpath="/tmp/spec_ap" \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --hidden-import=rumps \
  watchdog.py

# ── 3. Alle Apps signieren ───────────────────────────────────────────────
echo "=== Ad-hoc Code-Signaturen ==="
codesign --force --deep --sign - /tmp/dist_ap/PunchBuddy.app
codesign --force --deep --sign - /tmp/dist_ap/PunchBuddy_Diagnose.app
codesign --force --deep --sign - /tmp/dist_ap/PunchBuddy_Watchdog.app

# ── 4. DMG-Staging-Ordner mit allen Apps erstellen ───────────────────────
echo "=== DMG vorbereiten ==="
DMG_STAGE="/tmp/dist_ap/dmg_stage"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
cp -r /tmp/dist_ap/PunchBuddy.app           "$DMG_STAGE/"
cp -r /tmp/dist_ap/PunchBuddy_Diagnose.app  "$DMG_STAGE/"
cp -r /tmp/dist_ap/PunchBuddy_Watchdog.app  "$DMG_STAGE/"
cp    "$SCRIPT_DIR/PunchBuddy_Setup.command" "$DMG_STAGE/"
chmod +x "$DMG_STAGE/PunchBuddy_Setup.command"
# Dokumentation mitkopieren (falls vorhanden)
[ -f "$SCRIPT_DIR/BEDIENUNGSANLEITUNG.md" ]      && cp "$SCRIPT_DIR/BEDIENUNGSANLEITUNG.md"      "$DMG_STAGE/"
[ -f "$SCRIPT_DIR/TECHNISCHE_DOKUMENTATION.md" ] && cp "$SCRIPT_DIR/TECHNISCHE_DOKUMENTATION.md" "$DMG_STAGE/"

# ── 5. DMG erstellen ──────────────────────────────────────────────────────
echo "=== DMG erstellen ==="
rm -f "/tmp/PunchBuddy_raw.dmg"
rm -f "$SCRIPT_DIR/PunchBuddy_Release.dmg"

# 1. Hybrid Image (unkomprimiert) erstellen (umgeht Error -1 auf manchen Systemen)
hdiutil makehybrid -hfs -o "/tmp/PunchBuddy_raw.dmg" "$DMG_STAGE"

# 2. In komprimiertes UDZO Format konvertieren
hdiutil convert -format UDZO -o "$SCRIPT_DIR/PunchBuddy_Release.dmg" "/tmp/PunchBuddy_raw.dmg"

rm -f "/tmp/PunchBuddy_raw.dmg"

echo ""
echo "✅ Fertig: PunchBuddy_Release.dmg"
echo "   Enthält: PunchBuddy.app + PunchBuddy_Diagnose.app + PunchBuddy_Watchdog.app + PunchBuddy_Setup.command"
echo "            + BEDIENUNGSANLEITUNG.md + TECHNISCHE_DOKUMENTATION.md"
