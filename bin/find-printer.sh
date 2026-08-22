#!/bin/bash
# find-printer.sh — locate the Photosmart C4380 on the LAN and record its IP.
# The C4380 advertises _pdl-datastream._tcp (raw JetDirect on port 9100).

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/etc/kerchunk.conf"

echo "Browsing mDNS for _pdl-datastream._tcp (8s)..."
FOUND=$(ippfind _pdl-datastream._tcp -T 8 2>/dev/null | head -5)
if [ -n "$FOUND" ]; then
  echo "mDNS results:"; echo "$FOUND" | sed 's/^/  /'
fi

# Resolve any Photosmart-looking mDNS name to an address
IP=""
for name in $(echo "$FOUND" | sed -n 's#^.*://\([^:/]*\).*#\1#p'); do
  a=$(ping -c 1 -t 2 "$name" 2>/dev/null | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -1)
  [ -n "$a" ] && { IP="$a"; break; }
done

# Fallback: sweep the local /24 for anything answering on port 9100
if [ -z "$IP" ]; then
  echo "No mDNS hit. Sweeping local subnet for open port 9100..."
  SELF=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
  [ -z "$SELF" ] && { echo "No active network interface."; exit 1; }
  BASE="${SELF%.*}"
  for i in $(seq 1 254); do
    ( nc -z -G 1 -w 1 "$BASE.$i" 9100 2>/dev/null && echo "  $BASE.$i has port 9100 open" ) &
  done
  wait
  echo
  echo "If exactly one address above is your C4380, set it in $CONF"
  exit 0
fi

echo "Found printer at: $IP"
if grep -q '^PRINTER_IP=' "$CONF" 2>/dev/null; then
  sed -i '' "s|^PRINTER_IP=.*|PRINTER_IP=\"$IP\"|" "$CONF"
  echo "Recorded PRINTER_IP=\"$IP\" in $CONF"
fi
