#!/usr/bin/env python3
"""
Printer status for the HP Photosmart C4380, over SNMP.

Without this the print path is fire-and-forget: pdf2c4380.sh pipes PCL3GUI at
port 9100 and never asks the printer anything, so a paper jam looks exactly
like a successful job. The printer answers the standard Printer MIB (RFC 3805)
on UDP 161, which carries paper-out, jam, door-open and ink levels -- the same
route HPLIP takes (see GetSnmp() in io/hpmud/jd.c).

Pure standard library: a minimal SNMPv1 GET rather than a net-snmp dependency.

    ./printer_status.py                      # uses etc/kerchunk.conf
    ./printer_status.py --ip HP014507.local  # or name it explicitly
    ./printer_status.py --state              # STATE:/ATTR: lines for CUPS
"""

import argparse
import os
import socket
import sys

SNMP_PORT = 161

# ---------------------------------------------------------------- OIDs

OID_DEVICE_DESCR = '1.3.6.1.2.1.25.3.2.1.3.1'
OID_PRINTER_STATUS = '1.3.6.1.2.1.25.3.5.1.1.1'
OID_ERROR_STATE = '1.3.6.1.2.1.25.3.5.1.2.1'
OID_SUPPLY_DESCR = '1.3.6.1.2.1.43.11.1.1.6.1'
OID_SUPPLY_LEVEL = '1.3.6.1.2.1.43.11.1.1.9.1'
OID_SUPPLY_MAX = '1.3.6.1.2.1.43.11.1.1.8.1'
# Lifetime page count. The serial number and printer-name OIDs are not
# implemented on this device (SNMP error 2), so they are not queried.
OID_PAGE_COUNT = '1.3.6.1.2.1.43.10.2.1.4.1.1'

PRINTER_STATUS = {1: 'other', 2: 'unknown', 3: 'idle', 4: 'printing',
                  5: 'warmup'}

# hrPrinterDetectedErrorState, RFC 3805: a bit string, MSB first within each
# byte. Mapped to IPP printer-state-reasons keywords (RFC 8011).
ERROR_BITS = [
    # (byte, mask, ipp keyword, human text, is_fatal)
    (0, 0x80, 'media-low',            'paper low',            False),
    (0, 0x40, 'media-empty',          'out of paper',         True),
    (0, 0x20, 'marker-supply-low',    'ink low',              False),
    (0, 0x10, 'marker-supply-empty',  'out of ink',           True),
    (0, 0x08, 'door-open',            'cover or door open',   True),
    (0, 0x04, 'jam',                  'paper jam',            True),
    (0, 0x02, 'offline',              'offline',              True),
    (0, 0x01, 'service-requested',    'service requested',    True),
    (1, 0x80, 'input-tray-missing',   'input tray missing',   True),
    (1, 0x40, 'output-tray-missing',  'output tray missing',  True),
    (1, 0x20, 'marker-supply-empty',  'ink cartridge missing', True),
    (1, 0x10, 'output-area-almost-full', 'output tray nearly full', False),
    (1, 0x08, 'output-area-full',     'output tray full',     True),
    (1, 0x04, 'media-empty',          'input tray empty',     True),
    (1, 0x02, 'other',                'maintenance overdue',  False),
]

INK_LOW_PERCENT = 10
MAX_ADVERTISED_SUPPLIES = 5


class SnmpError(Exception):
    pass


# ---------------------------------------------------------------- BER

def _enc_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, val):
    return bytes([tag]) + _enc_len(len(val)) + val


def _enc_oid(oid):
    parts = [int(x) for x in oid.split('.')]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p == 0:
            out += b'\x00'
            continue
        chunk = b''
        while p:
            chunk = bytes([(p & 0x7F) | (0x80 if chunk else 0)]) + chunk
            p >>= 7
        out += chunk
    return _tlv(0x06, out)


def _enc_int(i):
    return _tlv(0x02, i.to_bytes(max(1, (i.bit_length() + 8) // 8), 'big',
                                 signed=True))


def _read_tlv(b, i):
    tag = b[i]
    i += 1
    length = b[i]
    i += 1
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(b[i:i + n], 'big')
        i += n
    return tag, b[i:i + length], i + length


def _dec_oid(b):
    out = [b[0] // 40, b[0] % 40]
    v = 0
    for c in b[1:]:
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            out.append(v)
            v = 0
    return '.'.join(map(str, out))


def snmp_get(ip, oid, community='public', timeout=2.0, next_=False):
    """One SNMPv1 GET (or GETNEXT). Returns (oid, tag, value_bytes)."""
    varbind = _tlv(0x30, _enc_oid(oid) + _tlv(0x05, b''))
    pdu = _tlv(0xA1 if next_ else 0xA0,
               _enc_int(1) + _enc_int(0) + _enc_int(0) + _tlv(0x30, varbind))
    msg = _tlv(0x30, _enc_int(0) + _tlv(0x04, community.encode()) + pdu)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, (ip, SNMP_PORT))
        data, _ = sock.recvfrom(4096)
    except socket.timeout:
        raise SnmpError('no SNMP reply from %s' % ip)
    finally:
        sock.close()

    _, seq, _ = _read_tlv(data, 0)
    i = 0
    _, _, i = _read_tlv(seq, i)                       # version
    _, _, i = _read_tlv(seq, i)                       # community
    _, pdu_in, _ = _read_tlv(seq, i)
    j = 0
    _, _, j = _read_tlv(pdu_in, j)                    # request id
    _, err, j = _read_tlv(pdu_in, j)                  # error status
    _, _, j = _read_tlv(pdu_in, j)                    # error index
    if int.from_bytes(err, 'big'):
        raise SnmpError('SNMP error %d for %s' % (int.from_bytes(err, 'big'), oid))
    _, vbl, _ = _read_tlv(pdu_in, j)
    _, vb, _ = _read_tlv(vbl, 0)
    k = 0
    _, oid_b, k = _read_tlv(vb, k)
    tag, val, _ = _read_tlv(vb, k)
    return _dec_oid(oid_b), tag, val


def _as_int(tag, val):
    return int.from_bytes(val, 'big') if val else None


def _walk(ip, root, limit=12, retries=2, **kw):
    """Minimal GETNEXT walk under one subtree.

    SNMP rides on UDP, so any single request can be lost -- and the walk has
    only two ways to stop: the next OID leaving the subtree, which is the real
    end, or an error, which is not. Conflating the two silently truncates the
    result: a dropped packet half way through the supplies table yields one
    cartridge instead of two, and we would then publish that as the truth.
    So retry each step, and raise rather than return a partial table -- the
    caller treats missing data as "say nothing", which leaves whatever the
    client already knows intact.
    """
    out, oid = [], root
    for _ in range(limit):
        for attempt in range(retries + 1):
            try:
                oid, tag, val = snmp_get(ip, oid, next_=True, **kw)
                break
            except SnmpError:
                if attempt == retries:
                    raise
        if not oid.startswith(root + '.'):
            break
        out.append((oid, tag, val))
    return out


# ---------------------------------------------------------------- status

def decode_errors(raw):
    """Turn hrPrinterDetectedErrorState into (keywords, human, fatal)."""
    keywords, human, fatal = [], [], False
    for index, mask, keyword, text, is_fatal in ERROR_BITS:
        if index < len(raw) and raw[index] & mask:
            if keyword not in keywords:
                keywords.append(keyword)
            human.append(text)
            fatal = fatal or is_fatal
    return keywords, human, fatal


def read_supplies(ip, **kw):
    """Ink levels as percentages.

    HP reports these already scaled 0-100 -- the printer's own web UI shows
    the same numbers as prtMarkerSuppliesLevel, while MaxCapacity reads a
    meaningless 254. Anything outside 0-100 (the ink blotter reports 170) is
    not a cartridge percentage, so it is reported without one.
    """
    descrs = {o.rsplit('.', 1)[1]: v.decode('latin-1', 'replace').strip()
              for o, t, v in _walk(ip, OID_SUPPLY_DESCR, **kw)}
    levels = {o.rsplit('.', 1)[1]: _as_int(t, v)
              for o, t, v in _walk(ip, OID_SUPPLY_LEVEL, **kw)}
    out = []
    for key in sorted(descrs, key=lambda x: int(x)):
        if key not in levels:
            # Description without a level: the two walks disagree, so this
            # entry is incomplete rather than merely unmeasured.
            continue
        level = levels[key]
        percent = level if level is not None and 0 <= level <= 100 else None
        out.append({'name': descrs[key], 'level': level, 'percent': percent})
    return out


def read_status(ip, **kw):
    """Everything worth knowing, in one call."""
    status = {'ip': ip, 'reachable': False, 'model': None, 'state': None,
              'reasons': [], 'problems': [], 'fatal': False, 'supplies': [],
              'pages': None}
    try:
        _, tag, val = snmp_get(ip, OID_PRINTER_STATUS, **kw)
        status['reachable'] = True
        status['state'] = PRINTER_STATUS.get(_as_int(tag, val), 'unknown')
    except SnmpError as exc:
        status['error'] = str(exc)
        return status

    try:
        _, tag, val = snmp_get(ip, OID_DEVICE_DESCR, **kw)
        status['model'] = val.decode('latin-1', 'replace').strip()
    except SnmpError:
        pass

    try:
        _, tag, raw = snmp_get(ip, OID_ERROR_STATE, **kw)
        status['reasons'], status['problems'], status['fatal'] = decode_errors(raw)
    except SnmpError:
        pass

    try:
        _, tag, val = snmp_get(ip, OID_PAGE_COUNT, **kw)
        status['pages'] = _as_int(tag, val)
    except SnmpError:
        pass

    try:
        status['supplies'] = read_supplies(ip, **kw)
    except SnmpError:
        pass

    # The error bitmask reports ink low only once the printer decides so;
    # fold in our own threshold so the warning is not a surprise.
    for supply in status['supplies']:
        pct = supply['percent']
        if pct is not None and pct <= INK_LOW_PERCENT:
            if 'marker-supply-low' not in status['reasons']:
                status['reasons'].append('marker-supply-low')
                status['problems'].append('%s low (%d%%)' % (supply['name'], pct))
    return status


def _colorant(name):
    """Map HP's supply description to a PWG colorant keyword."""
    low = name.lower()
    if 'black' in low:
        return 'black'
    if 'tri-color' in low or 'tri-colour' in low or 'color' in low:
        return 'multi-color'
    return 'unknown'


def _supply_label(name):
    """A space-free label for the supply.

    ippeveprinter parses an ATTR line as space-separated name=value pairs, so
    any value containing a space is truncated at it -- "Black Ink Cartridge"
    arrives as "Black", and the attribute collapses from a set to a single
    value. Verified against the real binary, so this is not paranoia.
    """
    low = name.lower()
    if 'black' in low:
        return 'Black'
    if 'tri-color' in low or 'tri-colour' in low:
        return 'Tri-Color'
    return name.title().replace(' ', '-')


# CUPS marker-colors takes one or more #rrggbb triplets per supply, simply
# concatenated. That is how a tri-colour cartridge is meant to be described,
# and it is why the macOS bar can be drawn in three colours rather than the
# flat black ippeveprinter's own supplies page falls back to for a colorant
# name it does not recognise.
MARKER_COLORS = {
    'black': '#000000',
    'multi-color': '#00FFFF#FF00FF#FFFF00',
    'unknown': '#808080',
}


def supply_attrs(supplies):
    """Build ippeveprinter ATTR: lines describing the real ink levels.

    Without these, ippeveprinter serves its own built-in demo values -- a
    four-colour set reading black 75 / cyan 50 / magenta 33 / yellow 67 --
    which is not this printer and not these levels. ATTR: is the only hook
    it offers for overriding them (-a would work too, but it suppresses 39
    other attributes and breaks iOS; see the notes in start-server.sh).

    Descriptions must not contain spaces; see _supply_label().
    """
    entries, descrs = [], []
    index = 0
    # ippeveprinter's supplies page indexes a fixed five-entry colour table by
    # supply position -- backgrounds[i] in tools/ippeveprinter.c -- with no
    # bounds check, so a sixth supply reads past the end of the array. Cap it.
    # (The same positional lookup is why the tri-colour bar renders black there
    # whatever colorantname we use; the macOS pane uses marker-colors instead
    # and gets it right.)
    for supply in supplies[:MAX_ADVERTISED_SUPPLIES]:
        if supply['percent'] is None:
            continue                       # e.g. the ink blotter, which is
        index += 1                         # not reported as a percentage
        entries.append(
            'index=%d;class=supplyThatIsConsumed;type=ink;unit=percent;'
            'maxcapacity=100;level=%d;colorantname=%s;'
            % (index, supply['percent'], _colorant(supply['name'])))
        descrs.append(_supply_label(supply['name']))
    if not entries:
        return []

    # printer-supply drives ippeveprinter's own supplies web page. The
    # marker-* attributes are what CUPS and the macOS "Supply Levels" pane
    # render natively -- which matters twice over, because that pane
    # otherwise embeds printer-supply-info-uri, and ippeveprinter builds that
    # as an https:// URL served with a self-signed certificate that a WebView
    # will not silently accept, leaving the dialog spinning on "Gathering
    # Supplies Information". printer-supply-info-uri cannot be set via ATTR,
    # so supplying markers is the way out.
    levels = [str(s['percent']) for s in supplies if s['percent'] is not None]
    colors = [MARKER_COLORS.get(_colorant(s['name']), MARKER_COLORS['unknown'])
              for s in supplies if s['percent'] is not None]
    return [
        'ATTR: printer-supply=%s' % ','.join(entries),
        'ATTR: printer-supply-description=%s' % ','.join(descrs),
        'ATTR: marker-names=%s' % ','.join(descrs),
        'ATTR: marker-levels=%s' % ','.join(levels),
        'ATTR: marker-colors=%s' % ','.join(colors),
        'ATTR: marker-types=%s' % ','.join(['ink'] * len(levels)),
        'ATTR: marker-low-levels=%s' % ','.join([str(INK_LOW_PERCENT)] * len(levels)),
        'ATTR: marker-high-levels=%s' % ','.join(['100'] * len(levels)),
    ]


def conf_printer():
    """PRINTER_IP from etc/kerchunk.conf, so --ip is optional here too.

    It may be a hostname rather than an address, and usually should be: DHCP
    moves printers, and a .local name follows them. Everything downstream
    passes this straight to socket calls, which resolve names happily.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, 'etc', 'kerchunk.conf')
    if not os.path.exists(path):
        return ''
    with open(path) as fh:
        for line in fh:
            if line.startswith('PRINTER_IP'):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ip', default=conf_printer() or None,
                    help='printer address or hostname; defaults to '
                         'PRINTER_IP in etc/kerchunk.conf')
    ap.add_argument('--community', default='public')
    ap.add_argument('--timeout', type=float, default=2.0)
    ap.add_argument('--state', action='store_true',
                    help='emit the "STATE:" and "ATTR:" lines ippeveprinter '
                         'reads from a print command, instead of a report')
    args = ap.parse_args()
    if not args.ip:
        ap.error('no printer address: pass --ip or set PRINTER_IP in '
                 'etc/kerchunk.conf')

    st = read_status(args.ip, community=args.community, timeout=args.timeout)

    if args.state:
        # Silence is correct when unreachable: better no claim than a false one.
        if st['reachable']:
            print('STATE: %s' % (','.join(st['reasons']) if st['reasons'] else 'none'))
            for line in supply_attrs(st['supplies']):
                print(line)
        return 0 if not st['fatal'] else 1

    if not st['reachable']:
        print('unreachable over SNMP: %s' % st.get('error', 'no reply'))
        return 2
    print('%-12s %s' % ('printer', st['model'] or '?'))
    print('%-12s %s' % ('state', st['state']))
    if st.get('pages') is not None:
        print('%-12s %s' % ('pages', st['pages']))
    if st['problems']:
        for p in st['problems']:
            print('%-12s %s' % ('problem', p))
    else:
        print('%-12s none' % 'problems')
    for s in st['supplies']:
        level = '%d%%' % s['percent'] if s['percent'] is not None \
            else '%s (not a percentage)' % s['level']
        print('%-12s %-24s %s' % ('supply', s['name'], level))
    return 1 if st['fatal'] else 0


if __name__ == '__main__':
    sys.exit(main())
