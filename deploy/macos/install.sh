#!/bin/bash
#
# install.sh — run Kerchunk on a Mac instead of (or alongside) a Raspberry Pi.
#
#     deploy/macos/install.sh          install and start
#     deploy/macos/install.sh --uninstall
#
# Installs two launchd agents that start at login and restart on failure.
# No sudo, no app bundle, no code signing: they are just the same scripts the
# Pi runs, with dns-sd standing in for avahi-publish.
#
# The Pi is the better home for this -- a Mac only serves while it is awake
# and logged in, and the AirPrint bridge has to listen on all interfaces for
# iOS to reach it, which follows a laptop onto other networks. Use this if you
# do not have a Pi, or to try things out before committing one.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
PRINT_LABEL=local.kerchunk.print
SCAN_LABEL=local.kerchunk.scan

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

unload() {
    for label in "$PRINT_LABEL" "$SCAN_LABEL"; do
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
    done
}

case "${1:-}" in
    --uninstall)
        unload
        rm -f "$AGENTS/$PRINT_LABEL.plist" "$AGENTS/$SCAN_LABEL.plist"
        echo "removed. The macOS print queue, if any, is left alone:"
        echo "  lpstat -p | grep -i kerchunk"
        exit 0 ;;
    -h|--help) usage ;;
esac

command -v gs >/dev/null || {
    echo "Ghostscript not found. brew install ghostscript" >&2; exit 1; }
command -v ippeveprinter >/dev/null || {
    echo "ippeveprinter not found (it ships with macOS)" >&2; exit 1; }

PRINTER_IP=$(sed -n 's/^PRINTER_IP="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' \
             "$ROOT/etc/kerchunk.conf" | head -1)
if [ -z "$PRINTER_IP" ]; then
    echo "PRINTER_IP is not set in $ROOT/etc/kerchunk.conf" >&2
    echo "Run $ROOT/bin/find-printer.sh with the printer switched on." >&2
    exit 1
fi

mkdir -p "$AGENTS" "$ROOT/var/log" "$ROOT/var/spool"

emit_plist() {   # label, program
    cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$1</string>
    <key>ProgramArguments</key>
    <array><string>$2</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>StandardOutPath</key><string>$ROOT/var/log/$1.log</string>
    <key>StandardErrorPath</key><string>$ROOT/var/log/$1.log</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST
    plutil -lint "$AGENTS/$1.plist" >/dev/null
}

unload
emit_plist "$PRINT_LABEL" "$ROOT/bin/start-server.sh"
emit_plist "$SCAN_LABEL"  "$ROOT/bin/escl-server.py"

for label in "$PRINT_LABEL" "$SCAN_LABEL"; do
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist" 2>/dev/null \
        || launchctl load "$AGENTS/$label.plist"
    echo "loaded $label"
done

echo
echo "Printer and scanner should appear by themselves. To add the print"
echo "queue explicitly:"
echo "  $ROOT/bin/install-queue.sh"
echo
echo "Logs: $ROOT/var/log/"
echo "Stop: $0 --uninstall"
