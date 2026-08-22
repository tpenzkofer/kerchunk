#!/bin/bash
# install-queue.sh — add the driverless macOS print queue pointing at our local shim.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/etc/kerchunk.conf"

QUEUE="${QUEUE_NAME:-Photosmart_C4380_Native}"
URI="ipp://localhost:${IPP_PORT:-8632}/ipp/print"

echo "Adding driverless queue '$QUEUE' -> $URI"
lpadmin -p "$QUEUE" -E -v "$URI" -m everywhere \
        -D "HP Photosmart C4380 (native, driverless)" \
        -L "local"
echo "Done. Queue uses IPP Everywhere — no vendor PPD, no /Library/Printers filter."
lpstat -p "$QUEUE" -v 2>/dev/null | sed 's/^/  /'
