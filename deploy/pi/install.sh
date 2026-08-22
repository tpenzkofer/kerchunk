#!/bin/bash
#
# install.sh — set up the C4380 print + scan bridges on a Raspberry Pi.
#
# Run ON the Pi, as root, from a copy of this repository:
#
#     sudo deploy/pi/install.sh
#
# Afterwards the Pi advertises two services on the LAN, and the Mac, iPhone
# and iPad talk to it instead of to any one computer:
#
#     _ipp._tcp   + _universal   -> AirPrint  (port 8632)
#     _uscan._tcp                -> AirScan   (port 8633)
#
# Nothing HP-supplied is installed: no HPLIP, no SANE, no vendor plugin.

set -euo pipefail

PREFIX=/opt/kerchunk
SERVICE_USER=kerchunk
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }

echo "==> installing packages"
apt-get update -qq
# ghostscript      : PDF -> PCL3GUI for printing
# cups-ipp-utils   : provides ippeveprinter (the AirPrint front end)
# avahi-daemon +
#   avahi-utils    : Bonjour, and avahi-publish for the scanner advert
# python3          : the eSCL bridge (standard library only)
apt-get install -y --no-install-recommends \
    ghostscript cups-ipp-utils avahi-daemon avahi-utils python3

echo "==> creating $SERVICE_USER user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> installing to $PREFIX"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$PREFIX" "$PREFIX/bin" "$PREFIX/etc" "$PREFIX/docs" \
    "$PREFIX/share/icons" "$PREFIX/var/log" "$PREFIX/var/spool"

install -m 0755 -o root -g root \
    "$SRC/bin/scl.py" "$SRC/bin/escl-server.py" \
    "$SRC/bin/pdf2c4380.sh" "$SRC/bin/start-server.sh" \
    "$SRC/bin/find-printer.sh" "$SRC/bin/printer_status.py" \
    "$SRC/bin/maintenance.py" "$PREFIX/bin/"
install -m 0644 -o root -g root "$SRC/docs/scanning-findings.md" "$PREFIX/docs/" 2>/dev/null || true
# Device icons shown in the macOS/iOS printer and scanner pickers.
# Ship every size: ippeveprinter is handed 48/128/512 so the Add Printer
# dialog (64pt = 128px retina) gets a purpose-made image rather than a
# shrunken 512.
for f in "$SRC"/share/icons/*.png; do
    [ -f "$f" ] || continue
    install -m 0644 -o root -g root "$f" "$PREFIX/share/icons/"
done
install -m 0644 -o root -g root "$SRC/README.md" "$PREFIX/" 2>/dev/null || true

# Never clobber an existing config -- it holds the printer's address.
if [ -f "$PREFIX/etc/kerchunk.conf" ]; then
    echo "    keeping existing $PREFIX/etc/kerchunk.conf"
    install -m 0644 "$SRC/etc/kerchunk.conf" "$PREFIX/etc/kerchunk.conf.new"
    echo "    new version written to kerchunk.conf.new — merge by hand if needed"
else
    install -m 0644 -o root -g root "$SRC/etc/kerchunk.conf" "$PREFIX/etc/"
fi

echo "==> setting up TLS for the print bridge"
# ippeveprinter advertises _ipps._tcp whether or not it can actually do TLS,
# and macOS prefers that entry — so without a certificate "Add Printer" fails
# with "Unable to connect to ..._ipps._tcp.local.". This build also cannot
# generate credentials itself, so create a self-signed cert named after the
# host, which is the filename CUPS looks for in the key path.
SSL_DIR="$PREFIX/var/ssl"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SSL_DIR"

# CUPS names its credential files after the hostname it resolves for its OWN
# address, which is not necessarily $(hostname): on a box running Pi-hole the
# reverse lookup answers "pi.hole", and CUPS then ignores a cert filed under
# any other name. Meanwhile macOS connects by the mDNS name. So: build one
# certificate whose SAN list covers every name the device answers to, and file
# it under each of them.
SHORT="$(hostname)"
PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
RDNS="$(getent hosts "$PRIMARY_IP" 2>/dev/null | awk '{print $2}' | head -1)"

NAMES="$SHORT.local $SHORT"
SAN="DNS:$SHORT.local,DNS:$SHORT"
if [ -n "$RDNS" ] && [ "$RDNS" != "$SHORT" ] && [ "$RDNS" != "$SHORT.local" ]; then
    NAMES="$NAMES $RDNS"
    SAN="$SAN,DNS:$RDNS"
fi
if [ -n "$PRIMARY_IP" ]; then
    SAN="$SAN,IP:$PRIMARY_IP"
fi

# Validate the file CUPS will actually reach for -- the one named after the
# reverse-resolved name -- not the one we would have preferred it to use.
CUPS_NAME="${RDNS:-$SHORT}"
if [ ! -s "$SSL_DIR/$CUPS_NAME.crt" ] || \
   ! openssl x509 -in "$SSL_DIR/$CUPS_NAME.crt" -noout \
       -checkhost "$SHORT.local" >/dev/null 2>&1
then
    # Clear stale credentials first. Do it here, as root: a glob against this
    # 0700 directory from an unprivileged shell silently expands to nothing.
    find "$SSL_DIR" -maxdepth 1 -type f \( -name '*.crt' -o -name '*.key' \) -delete
    tmpcrt="$(mktemp "${TMPDIR:-/tmp}/crt.XXXXXX")"
    tmpkey="$(mktemp "${TMPDIR:-/tmp}/key.XXXXXX")"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$tmpkey" -out "$tmpcrt" \
        -subj "/CN=$SHORT.local/O=Kerchunk" \
        -addext "subjectAltName=$SAN" >/dev/null 2>&1
    for n in $NAMES; do
        install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$tmpcrt" "$SSL_DIR/$n.crt"
        install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$tmpkey" "$SSL_DIR/$n.key"
    done
    rm -f "$tmpcrt" "$tmpkey"
    echo "    generated certificate for: $NAMES"
    echo "    SAN: $SAN"
else
    echo "    keeping existing certificate (covers $SHORT.local)"
fi

echo "==> installing systemd units"
install -m 0644 "$SRC/deploy/pi/kerchunk-print.service" \
                "$SRC/deploy/pi/kerchunk-scan.service" /etc/systemd/system/
systemctl daemon-reload

PRINTER_IP=$(sed -n 's/^PRINTER_IP="\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' \
             "$PREFIX/etc/kerchunk.conf" | head -1)
if [ -z "$PRINTER_IP" ]; then
    echo
    echo "!! PRINTER_IP is not set in $PREFIX/etc/kerchunk.conf."
    echo "   Set it (or run $PREFIX/bin/find-printer.sh), then:"
    echo "     systemctl enable --now kerchunk-print kerchunk-scan"
    exit 0
fi

echo "==> checking the printer at $PRINTER_IP"
for port in 9100 9290; do
    if timeout 3 bash -c "</dev/tcp/$PRINTER_IP/$port" 2>/dev/null; then
        echo "    port $port open"
    else
        echo "    WARNING: port $port unreachable — is the printer awake?"
    fi
done

echo "==> enabling services"
systemctl enable --now kerchunk-print kerchunk-scan
sleep 2
systemctl --no-pager --lines=5 status kerchunk-print kerchunk-scan || true

cat <<EOF

Done. On the Pi you can verify with:

    avahi-browse -rt _ipp._tcp
    avahi-browse -rt _uscan._tcp
    journalctl -u kerchunk-scan -f

On the Mac, the printer and scanner should now appear by themselves.
Remove the old Mac-hosted launchd agents if they are still loaded:

    launchctl unload ~/Library/LaunchAgents/local.photosmart-c4380.plist
EOF
