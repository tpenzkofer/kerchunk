#!/bin/bash
#
# set-mac-icon.sh — give the macOS print queue a custom icon.
#
# Why this exists: ippeveprinter does advertise printer-icons over IPP, but it
# builds those URIs as https:// even when it is serving plaintext, e.g.
#
#     printer-icons = https://raspberrypi.local:8632/icon.png
#
# Nothing listens for TLS on that port, so macOS's fetch fails silently and
# Printers & Scanners falls back to a generic glyph. There is no ippeveprinter
# flag to change the scheme.
#
# macOS's own mechanism is the PPD keyword *APPrinterIconPath, pointing at an
# .icns on disk — the same thing it does for real AirPrint printers, whose
# downloaded icons it caches in /Library/Printers/Icons. So we set it directly.
#
# Re-run this after any lpadmin change: recreating a queue regenerates its PPD
# and drops the keyword.
#
# Usage:  sudo bin/set-mac-icon.sh [queue-name]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# macOS rewrites queue names when a printer is added through the GUI --
# "Photosmart C4380 (Kerchunk)" becomes Photosmart_C4380__Kerchunk_ -- so
# find the queue by its device URI rather than assuming a name.
if [ -n "${1:-}" ]; then
    QUEUE="$1"
else
    QUEUE=$(lpstat -v 2>/dev/null \
            | awk -F'[ :]' '/[Kk]erchunk/ {print $3}' | head -1)
    QUEUE="${QUEUE:-Photosmart_C4380_Kerchunk}"
fi
ICON_SRC="${PRINTER_ICNS:-$ROOT/share/icons/printer.icns}"
ICON_DST="/Library/Printers/Icons/Kerchunk.icns"
PPD="/etc/cups/ppd/$QUEUE.ppd"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0 $QUEUE" >&2; exit 1; }
[ -r "$ICON_SRC" ] || { echo "icon not found: $ICON_SRC" >&2; exit 1; }
[ -r "$PPD" ] || { echo "no PPD for queue '$QUEUE' at $PPD" >&2
                   echo "queues: $(lpstat -p 2>/dev/null | awk '/^printer/{print $2}' | tr '\n' ' ')" >&2
                   exit 1; }

install -d /Library/Printers/Icons
install -m 0644 "$ICON_SRC" "$ICON_DST"
echo "installed $ICON_DST"

# Replace any existing keyword, then append ours.
tmp="$(mktemp "${TMPDIR:-/tmp}/ppd.XXXXXX")"
grep -v '^\*APPrinterIconPath:' "$PPD" > "$tmp"
printf '*APPrinterIconPath: "%s"\n' "$ICON_DST" >> "$tmp"
cat "$tmp" > "$PPD"          # preserve the original file's owner and mode
rm -f "$tmp"
echo "patched $PPD"

# cupsd caches PPDs in memory; make it re-read them.
launchctl kickstart -k system/org.cups.cupsd 2>/dev/null \
  || killall -HUP cupsd 2>/dev/null \
  || true

echo
echo "Done. Close and reopen System Settings > Printers & Scanners to see it."
