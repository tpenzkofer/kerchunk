#!/bin/bash
#
# pdf2c4380.sh — ippeveprinter print command for the HP Photosmart C4380.
#
# Contract (verified empirically against macOS 26 /usr/bin/ippeveprinter):
#   * argv[1]      = path to the spooled job file (PDF); stdin is EMPTY
#   * CONTENT_TYPE = MIME type of that file
#   * IPP_*        = job attributes (IPP_MEDIA, IPP_PRINT_QUALITY, IPP_COPIES, ...)
#   * stdout       = printer-ready data; ippeveprinter forwards it to the -D device URI
#   * stderr       = "ERROR:"/"INFO:"/"STATE:" lines are surfaced as job state
#
# Converts PDF -> PCL3GUI via Ghostscript (arm64 native). No vendor PPD, no
# /Library/Printers filter, no Rosetta — nothing in Apple's deprecation path.

set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/etc/kerchunk.conf"
LOG="$ROOT/var/log/print.log"

[ -f "$CONF" ] && . "$CONF"
GS_DEVICE="${GS_DEVICE:-chp2200}"
RESOLUTION="${RESOLUTION:-300}"

# Resolve Ghostscript rather than hard-coding a path: Homebrew on macOS,
# /usr/bin on Debian/Raspberry Pi OS.
GS="${GS_BIN:-$(command -v gs || echo /opt/homebrew/bin/gs)}"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
die() { echo "ERROR: $*" >&2; log "FAIL: $*"; exit 1; }

JOB="$1"
[ -n "$JOB" ] || die "no job file passed as argv[1]"
[ -r "$JOB" ] || die "job file not readable: $JOB"
[ -x "$GS" ]  || die "Ghostscript not found at $GS (brew install ghostscript)"

log "job=$JOB type=${CONTENT_TYPE:-?} media=${IPP_MEDIA:-default} quality=${IPP_PRINT_QUALITY:-normal} copies=${IPP_COPIES:-1} device=$GS_DEVICE"

# --- printer state ----------------------------------------------------------
# Port 9100 is write-only: it will happily swallow a job while the printer is
# jammed or out of paper, and the user would see nothing but "Printing...".
# Ask over SNMP first and publish the answer as printer-state-reasons, which
# ippeveprinter picks up from "STATE:" lines on stderr.
STATUS="$ROOT/bin/printer_status.py"
if [ "${STATUS_CHECK:-1}" = "1" ] && [ -x "$STATUS" ] && [ -n "$PRINTER_IP" ]; then
  if REASONS=$("$STATUS" --ip "$PRINTER_IP" --state 2>/dev/null); then
    [ -n "$REASONS" ] && echo "$REASONS" >&2
    log "printer state ok (${REASONS:-no reply})"
  else
    # Non-zero exit means a fault the user has to clear. Report it and stop
    # rather than pushing several megabytes into a printer that cannot print.
    [ -n "$REASONS" ] && echo "$REASONS" >&2
    DETAIL=$("$STATUS" --ip "$PRINTER_IP" 2>/dev/null | sed -n 's/^problem  *//p' | paste -sd'; ' -)
    die "printer not ready: ${DETAIL:-see printer panel}"
  fi
fi

# --- media size -> Ghostscript paper name ----------------------------------
case "${IPP_MEDIA:-}" in
  *a4*|*A4*)         PAPER="a4"     ;;
  *letter*|*Letter*) PAPER="letter" ;;
  *legal*)           PAPER="legal"  ;;
  *a5*)              PAPER="a5"     ;;
  *)                 PAPER="${DEFAULT_PAPER:-a4}" ;;
esac

# --- print quality -> resolution -------------------------------------------
case "${IPP_PRINT_QUALITY:-normal}" in
  draft)  DPI=300 ;;
  high)   DPI=600 ;;
  *)      DPI="$RESOLUTION" ;;
esac

# --- colour mode ------------------------------------------------------------
COLOR_OPTS=()
case "${IPP_PRINT_COLOR_MODE:-color}" in
  monochrome|bi-level) COLOR_OPTS=(-dBitsPerPixel=1) ;;
esac

# --- normalise input to PDF -------------------------------------------------
# ippeveprinter may hand us JPEG or PostScript depending on what the client sent.
# Spell the template out in full: BSD mktemp accepts "-t prefix", but GNU
# mktemp requires a template ending in at least six X's, so "-t c4380" fails
# on Linux. This form works on both.
WORK="$(mktemp "${TMPDIR:-/tmp}/c4380.XXXXXX")" || die "cannot create temp file"
trap 'rm -f "$WORK" "$WORK.pdf" "$WORK.prn"' EXIT

SRC="$JOB"

# iOS may send Apple Raster (URF) rather than PDF. Ghostscript cannot read it.
# Detect it explicitly so the failure is diagnosable instead of mysterious.
MAGIC=$(head -c 8 "$JOB" 2>/dev/null)
case "$MAGIC" in
  UNIRAST*) die "iOS sent Apple Raster (URF), which Ghostscript cannot decode. A URF decoder is required." ;;
  RaS2*|RaS3*) die "client sent PWG Raster, which Ghostscript cannot decode. A raster decoder is required." ;;
esac

# Some clients gzip the payload; Ghostscript cannot read that directly.
# od rather than xxd: xxd ships with vim, so it is present on macOS but not on
# a minimal Debian, where this check silently stopped detecting gzip.
if [ "$(head -c 2 "$JOB" | od -An -tx1 | tr -d ' \n')" = "1f8b" ]; then
  log "input is gzip-compressed; inflating"
  gunzip -c "$JOB" > "$WORK.pdf" 2>/dev/null || die "failed to inflate gzipped job"
  SRC="$WORK.pdf"
fi

# --- convert ----------------------------------------------------------------
log "gs device=$GS_DEVICE paper=$PAPER dpi=$DPI"
if ! "$GS" -q -dNOPAUSE -dBATCH -dSAFER \
        -sDEVICE="$GS_DEVICE" \
        -sPAPERSIZE="$PAPER" \
        -r"$DPI" \
        "${COLOR_OPTS[@]}" \
        -sOutputFile="$WORK.prn" \
        "$SRC" >> "$LOG" 2>&1; then
  die "Ghostscript conversion failed (device=$GS_DEVICE) — see $LOG"
fi

SIZE=$(wc -c < "$WORK.prn" | tr -d ' ')
[ "$SIZE" -gt 200 ] || die "conversion produced only ${SIZE} bytes — wrong gs device for this printer?"
log "converted OK: $SIZE bytes"
echo "INFO: converted to PCL3GUI (${SIZE} bytes, ${DPI}dpi, ${PAPER})" >&2

# --- emit, honouring copies -------------------------------------------------
COPIES="${IPP_COPIES:-1}"
case "$COPIES" in ''|*[!0-9]*) COPIES=1 ;; esac
[ "$COPIES" -lt 1 ] && COPIES=1

for ((c = 1; c <= COPIES; c++)); do
  cat "$WORK.prn" || die "failed writing copy $c to output"
done

log "sent $COPIES copy/copies ($((SIZE * COPIES)) bytes total)"
exit 0
