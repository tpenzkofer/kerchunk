#!/bin/bash
# start-server.sh — run the local IPP Everywhere front end for the C4380.
# macOS and iOS talk driverless IPP to this; it converts to PCL3GUI and
# forwards to the printer's port 9100.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/etc/kerchunk.conf"

if [ -z "$PRINTER_IP" ]; then
  echo "PRINTER_IP is not set in $ROOT/etc/kerchunk.conf" >&2
  echo "Power the printer on and run: $ROOT/bin/find-printer.sh" >&2
  exit 1
fi

mkdir -p "$ROOT/var/log" "$ROOT/var/spool"

# AirPrint needs image/urf in the advertised formats: iOS requires "image/urf"
# in the TXT "pdl" key, and listing it here makes ippeveprinter derive the
# "urf-supported" attribute (and hence the URF TXT key) automatically.
#
# Do NOT use -a to inject URF instead: -a is mutually exclusive with -M/-m/-f/-s
# AND suppresses 39 built-in attributes, including media-col-database (iOS then
# fails with "Unsupported media-col collection value") and print-quality-supported
# (no quality selector in the print dialog).
FORMATS="application/pdf,image/jpeg"
[ "${AIRPRINT:-1}" = "1" ] && FORMATS="$FORMATS,image/urf"

# macOS ships ippeveprinter in /usr/bin; on Debian/Raspberry Pi OS it comes
# from the cups-ipp-utils package and may land in /usr/sbin.
IPPEVE="${IPPEVE_BIN:-$(command -v ippeveprinter || echo /usr/bin/ippeveprinter)}"
if [ ! -x "$IPPEVE" ]; then
  echo "ippeveprinter not found (Debian: apt install cups-ipp-utils)" >&2
  exit 1
fi

# Icon shown in the macOS/iOS print dialog. -i takes up to three files, which
# ippeveprinter serves as icon-sm/icon/icon-lg (48, 128 and 512 px) and lists
# in printer-icons. Supplying all three matters: the Add Printer dialog draws
# at 64pt — 128 px on a retina display — and letting it shrink a lone 512 px
# image there looks noticeably worse than handing it a purpose-made 128.
#
# Default to the multifunction artwork: macOS merges the _ipp._tcp and
# _uscan._tcp services (same name, same host) into one "Bonjour Multifunction"
# device, so this single icon represents the printer and the scanner together.
ICON_SET="${PRINTER_ICON_SET:-multifunction}"
ICON_DIR="$ROOT/share/icons"
ICON_OPT=()
if [ -n "${PRINTER_ICON:-}" ] && [ -r "$PRINTER_ICON" ]; then
  ICON_OPT=(-i "$PRINTER_ICON")
elif [ -r "$ICON_DIR/$ICON_SET-48.png" ]; then
  ICON_OPT=(-i "$ICON_DIR/$ICON_SET-48.png,$ICON_DIR/$ICON_SET-128.png,$ICON_DIR/$ICON_SET-512.png")
elif [ -r "$ICON_DIR/$ICON_SET.png" ]; then
  ICON_OPT=(-i "$ICON_DIR/$ICON_SET.png")
fi

# TLS is not optional in practice. ippeveprinter always advertises _ipps._tcp
# alongside _ipp._tcp and reports uri-security-supported="none,tls", and macOS
# prefers the secure entry — so without a certificate, adding the printer fails
# with "Unable to connect to ...._ipps._tcp.local.". It also builds the
# printer-icons URIs as https://, so the icon silently fails to download too.
#
# ippeveprinter cannot generate its own credentials on this build ("Unable to
# create server credentials"), so the installer pre-creates a self-signed cert
# named <hostname>.crt/.key, which is what CUPS looks for in the key path.
# Printers universally use self-signed certs, and macOS accepts them.
TLS_DIR="${TLS_KEYPATH:-$ROOT/var/ssl}"
TLS_OPT=()
[ -d "$TLS_DIR" ] && TLS_OPT=(-K "$TLS_DIR")

exec "$IPPEVE" \
  -p "${IPP_PORT:-8632}" \
  -c "$ROOT/bin/pdf2c4380.sh" \
  "${ICON_OPT[@]}" \
  "${TLS_OPT[@]}" \
  -D "socket://$PRINTER_IP:${PRINTER_PORT:-9100}" \
  -d "$ROOT/var/spool" \
  -f "$FORMATS" \
  -M "HP" \
  -m "Photosmart C4380" \
  -s "20,15" \
  -r _print,_universal \
  -v \
  "${SERVICE_NAME:-Photosmart C4380 (Native)}"
