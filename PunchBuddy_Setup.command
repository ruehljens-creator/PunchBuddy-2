#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  PunchBuddy – macOS Berechtigungen einrichten
#
#  Doppelklick zum Ausführen.
#  Das Script muss im selben Ordner wie die drei .app-Dateien liegen.
#  Nicht direkt aus dem DMG starten – erst alle Apps kopieren!
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TCC_DB="/Library/Application Support/com.apple.TCC/TCC.db"

APPS=(
    "PunchBuddy.app"
    "PunchBuddy_Watchdog.app"
    "PunchBuddy_Diagnose.app"
)
BUNDLE_IDS=(
    "PunchBuddy"
    "PunchBuddy_Watchdog"
    "PunchBuddy_Diagnose"
)
SERVICES=(
    "kTCCServiceAccessibility"
    "kTCCServiceListenEvent"
    "kTCCServiceSystemPolicyAllFiles"
)
SERVICE_LABELS=(
    "Bedienungshilfen        (Accessibility)"
    "Eingabeüberwachung      (Input Monitoring)"
    "Festplattenvollzugriff  (Full Disk Access)"
)

ok()   { echo "  ✓  $*"; }
warn() { echo "  ⚠  $*"; }
err()  { echo "  ✗  $*"; }
step() { echo ""; echo "──── $* "; }

clear
echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║   PunchBuddy – macOS Berechtigungen einrichten  ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""

# ── Prüfen ob Script auf schreibgeschütztem Datenträger läuft (DMG) ──
if ! touch "$SCRIPT_DIR/.setup_write_test" 2>/dev/null; then
    err "Dieses Script läuft auf einem schreibgeschützten Datenträger (z.B. DMG)."
    echo ""
    echo "  Bitte zuerst alle Dateien aus dem DMG in einen beschreibbaren"
    echo "  Ordner kopieren (z.B. Programme oder Schreibtisch),"
    echo "  dann PunchBuddy_Setup.command von dort starten."
    echo ""
    echo "  Drücke Return zum Beenden..."
    read -r
    exit 1
fi
rm -f "$SCRIPT_DIR/.setup_write_test"

echo "  Richtet folgende Berechtigungen ein:"
echo "   • Quarantäne entfernen  (Gatekeeper / unsigned App)"
echo "   • Bedienungshilfen      (Accessibility)"
echo "   • Eingabeüberwachung    (Input Monitoring)"
echo "   • Festplattenvollzugriff (Full Disk Access)"
echo ""

# ═══════════════════════════════════════════════════════════════════
# Schritt 1: Quarantäne entfernen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 1: Quarantäne entfernen"
echo ""

ALL_FOUND=true
for app_name in "${APPS[@]}"; do
    app_path="$SCRIPT_DIR/$app_name"
    if [ -d "$app_path" ]; then
        xattr -rd com.apple.quarantine "$app_path" 2>/dev/null
        ok "$app_name"
    else
        warn "$app_name – nicht gefunden unter $app_path"
        ALL_FOUND=false
    fi
done

if ! $ALL_FOUND; then
    echo ""
    warn "Einige Apps nicht gefunden. Sicherstellen, dass dieses Script"
    warn "im selben Ordner wie PunchBuddy.app etc. liegt."
fi

# ═══════════════════════════════════════════════════════════════════
# Schritt 2: Admin-Passwort abfragen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 2: Admin-Passwort"
echo ""
echo "  Das Admin-Passwort wird für Gatekeeper und Berechtigungen benötigt."
echo ""

HAS_SUDO=false
if sudo -v 2>/dev/null; then
    ok "Admin-Berechtigung erteilt"
    HAS_SUDO=true
    # Credential-Timeout im Hintergrund warmhalten
    ( while true; do sudo -n true; sleep 50; done ) &
    SUDO_KEEP_PID=$!
    trap "kill $SUDO_KEEP_PID 2>/dev/null" EXIT
else
    err "Kein Admin-Zugriff – Schritte 3 und 4 werden übersprungen."
fi

# ═══════════════════════════════════════════════════════════════════
# Schritt 3: Gatekeeper-Ausnahmen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 3: Gatekeeper-Ausnahmen (spctl)"
echo ""

if $HAS_SUDO; then
    for app_name in "${APPS[@]}"; do
        app_path="$SCRIPT_DIR/$app_name"
        if [ -d "$app_path" ]; then
            if sudo spctl --add "$app_path" 2>/dev/null; then
                ok "$app_name"
            else
                warn "$app_name – spctl fehlgeschlagen (evtl. bereits eingetragen)"
            fi
        fi
    done
else
    warn "Übersprungen (kein sudo)"
fi

# ═══════════════════════════════════════════════════════════════════
# Schritt 4: TCC-Berechtigungen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 4: Datenschutz-Berechtigungen"
echo ""

TCC_DONE=false

if ! $HAS_SUDO; then
    warn "Übersprungen (kein sudo)"
else
    # Prüfen ob sqlite3 die TCC.db öffnen kann
    # (klappt nur wenn das aufrufende Terminal Festplattenvollzugriff hat)
    if sudo sqlite3 "$TCC_DB" "SELECT count(*) FROM access;" >/dev/null 2>&1; then
        echo "  TCC-Datenbank zugänglich – setze Berechtigungen automatisch..."
        echo ""

        # Schema dynamisch auslesen, damit INSERT auf macOS 12–15+ passt
        SCHEMA_COLS=$(sudo sqlite3 "$TCC_DB" "PRAGMA table_info(access);" 2>/dev/null | awk -F'|' '{print $2}')
        HAS_BOOT_UUID=false
        echo "$SCHEMA_COLS" | grep -q "boot_uuid" && HAS_BOOT_UUID=true

        NOW=$(date +%s)

        tcc_insert() {
            local service="$1" bundle="$2"
            if $HAS_BOOT_UUID; then
                sudo sqlite3 "$TCC_DB" \
                  "INSERT OR REPLACE INTO access
                     (service, client, client_type, auth_value, auth_reason, auth_version,
                      indirect_object_identifier, boot_uuid, last_modified)
                   VALUES
                     ('$service','$bundle',0,2,4,1,'UNUSED','UNUSED',$NOW);" 2>/dev/null
            else
                sudo sqlite3 "$TCC_DB" \
                  "INSERT OR REPLACE INTO access
                     (service, client, client_type, auth_value, auth_reason, auth_version,
                      indirect_object_identifier, last_modified)
                   VALUES
                     ('$service','$bundle',0,2,4,1,'UNUSED',$NOW);" 2>/dev/null
            fi
        }

        for bundle in "${BUNDLE_IDS[@]}"; do
            FAIL=false
            for i in "${!SERVICES[@]}"; do
                tcc_insert "${SERVICES[$i]}" "$bundle" || FAIL=true
            done
            if $FAIL; then
                warn "$bundle – ein oder mehrere Einträge fehlgeschlagen"
            else
                ok "$bundle  →  alle drei Berechtigungen gesetzt"
            fi
        done

        TCC_DONE=true

    else
        echo "  TCC-Datenbank nicht direkt beschreibbar."
        echo ""
        echo "  Grund:    Das Terminal hat keinen Festplattenvollzugriff (FDA)."
        echo "  Lösung A: Terminal.app unter Systemeinstellungen → Datenschutz &"
        echo "            Sicherheit → Festplattenvollzugriff hinzufügen,"
        echo "            dann dieses Script erneut starten."
        echo ""
        echo "  Lösung B: Berechtigungen jetzt manuell einrichten (wird geführt)."
        echo ""
        printf "  Manuelle Einrichtung starten? [j/N] "
        read -r MANUAL
        if [[ "$MANUAL" =~ ^[jJyY] ]]; then
            TCC_DONE=false
        else
            echo ""
            warn "Datenschutz-Berechtigungen müssen manuell eingerichtet werden."
            TCC_DONE=skip
        fi
    fi
fi

# ── Geführte manuelle TCC-Einrichtung ────────────────────────────
if [ "$TCC_DONE" = "false" ]; then
    echo ""
    echo "  Systemeinstellungen werden geöffnet."
    echo "  In jedem Panel: PunchBuddy, PunchBuddy_Watchdog und"
    echo "  PunchBuddy_Diagnose mit dem (+)-Button hinzufügen."
    echo ""

    for i in "${!SERVICES[@]}"; do
        service="${SERVICES[$i]}"
        label="${SERVICE_LABELS[$i]}"
        echo "  [$((i+1))/3]  $label"

        case "$service" in
            kTCCServiceAccessibility)
                open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null ;;
            kTCCServiceListenEvent)
                open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent" 2>/dev/null ;;
            kTCCServiceSystemPolicyAllFiles)
                open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null ;;
        esac

        if [ $i -lt $(( ${#SERVICES[@]} - 1 )) ]; then
            echo "       Alle drei Apps eingetragen?"
            printf "       [Return → nächster Bereich] "
            read -r
        fi
    done
fi

# ═══════════════════════════════════════════════════════════════════
# Zusammenfassung
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "  ══════════════════════════════════════════════════════"
echo "  Ergebnis:"
echo ""
ok  "Quarantäne entfernt"
$HAS_SUDO        && ok   "Gatekeeper-Ausnahmen eingetragen" \
                || warn  "Gatekeeper – übersprungen (kein sudo)"
[ "$TCC_DONE" = "true" ] \
                && ok   "Berechtigungen automatisch gesetzt" \
                || warn  "Berechtigungen – bitte in Systemeinstellungen prüfen"
echo ""
if [ "$TCC_DONE" = "true" ]; then
    echo "  → Empfehlung: macOS kurz ab- und anmelden (oder neu starten)"
    echo "    damit TCC-Änderungen sofort aktiv werden."
else
    echo "  → Nach manueller Einrichtung PunchBuddy neu starten."
fi
echo ""
echo "  Drücke Return zum Beenden..."
read -r
