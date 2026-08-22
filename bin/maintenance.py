#!/usr/bin/env python3
"""
Maintenance functions for the HP Photosmart C4380 -- the things HP Utility
does, without HP Utility.

Head cleaning, cartridge alignment and the printer's internal pages are not
scan or print operations: they are PML (Printer Management Language) commands
wrapped in a short PCL envelope and sent to the ordinary print port, 9100.
So they need nothing this project does not already have.

Wire format, reconstructed from HPLIP base/{pml,maint}.py and prnt/pcl.py:

    PML set packet   04 00 <oidlen> <oid bytes> <type> <len> <value>
    PCL envelope     ESC & b <n> W PML <packet>
    style 0          UEL PJL_ENTER_LANG RESET <cmd> RESET UEL
    style 1          RESET UEL PJL_JOB PJL_ENTER_LANG RESET <cmd>
                     RESET PJL_EOJ RESET UEL

HPLIP picks the style per operation; this device is clean-type=1 and
align-type=1 in HPLIP's models.dat, which map to the styles used below.

    ./maintenance.py --ip 192.168.1.50 clean
    ./maintenance.py --ip 192.168.1.50 align
    ./maintenance.py --ip 192.168.1.50 --dry-run clean --level 3
"""

import argparse
import os
import socket
import struct
import sys

PRINT_PORT = 9100

# --- PCL envelope pieces (prnt/pcl.py) --------------------------------------
ESC = b'\x1b'
RESET = b'\x1bE'
UEL = b'\x1b%-12345X'
PJL_ENTER_LANG = b'@PJL ENTER LANGUAGE=PCL3GUI\n'
PJL_BEGIN_JOB = b'@PJL JOB NAME="kerchunk"\n'
PJL_END_JOB = b'@PJL EOJ\n'

# --- PML (base/pml.py) ------------------------------------------------------
SET_REQUEST = 0x04
TYPE_OBJECT_IDENTIFIER = 0x00
TYPE_ENUMERATION = 0x04
TYPE_SIGNED_INTEGER = 0x08
TYPE_STRING = 0x10
TYPE_COLLECTION = 0x20

OID_CLEAN = '1.4.1.5.1.1'
CLEAN_LEVELS = {1: 100, 2: 200, 3: 300}       # clean, prime, wipe-and-spit
CLEAN_NAMES = {1: 'clean', 2: 'prime', 3: 'wipe and spit'}

# Alignment and the internal pages share one OID; the value selects which.
OID_INTERNAL_PAGE = '1.1.5.2'
INTERNAL_PAGES = {
    'align': 1100,                # auto alignment (align-type=1)
    'supplies': 101,              # supplies status page
    'colour-palette': 259,        # CMYK colour palette
    'colour-cal': 1102,           # colour calibration
    'print-quality': 1409,        # print-quality diagnostic
}

# Operations that consume ink or paper, so they are never a default.
CONSUMES = {'clean', 'align', 'supplies', 'colour-palette', 'colour-cal',
            'print-quality'}


def build_pml_set(oid, value, data_type):
    """One PML SET packet. Mirrors pml.buildPMLSetPacket()."""
    oid_bytes = bytes(int(b.strip()) for b in oid.split('.'))

    if data_type in (TYPE_ENUMERATION, TYPE_SIGNED_INTEGER, TYPE_COLLECTION):
        data = struct.pack('>i', int(value))
        if value > 0:
            while len(data) > 1 and data[0] == 0x00:
                data = data[1:]
        else:
            while len(data) > 1 and data[0] == 0xFF and data[1] == 0xFF:
                data = data[1:]
        payload = struct.pack('>BB', data_type, len(data)) + data
    elif data_type == TYPE_STRING:
        raw = value.encode('latin-1')
        payload = struct.pack('>BBBB', data_type, len(raw) + 2, 0x01, 0x15) + raw
    else:
        raise ValueError('unsupported PML type 0x%02x' % data_type)

    return (struct.pack('>BBB', SET_REQUEST, TYPE_OBJECT_IDENTIFIER,
                        len(oid_bytes)) + oid_bytes + payload)


def build_embedded_pml(oid, value, data_type, style=1):
    """Wrap a PML set packet in the PCL/PJL envelope the printer expects."""
    packet = b'PML ' + build_pml_set(oid, value, data_type)
    cmd = ESC + b'&b' + str(len(packet)).encode() + b'W' + packet
    if style == 0:
        return UEL + PJL_ENTER_LANG + RESET + cmd + RESET + UEL
    return (RESET + UEL + PJL_BEGIN_JOB + PJL_ENTER_LANG + RESET + cmd
            + RESET + PJL_END_JOB + RESET + UEL)


def send(ip, data, port=PRINT_PORT, timeout=15):
    sock = socket.create_connection((ip, port), timeout=timeout)
    try:
        sock.sendall(data)
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf_ip = ''
    conf_path = os.path.join(here, 'etc', 'kerchunk.conf')
    if os.path.exists(conf_path):
        for line in open(conf_path):
            if line.startswith('PRINTER_IP'):
                conf_ip = line.split('=', 1)[1].strip().strip('"').strip("'")

    ap.add_argument('--ip', default=conf_ip or None)
    ap.add_argument('--port', type=int, default=PRINT_PORT)
    ap.add_argument('--dry-run', action='store_true',
                    help='show the bytes without sending them')
    sub = ap.add_subparsers(dest='action', required=True)

    p_clean = sub.add_parser('clean', help='clean the print heads (uses ink)')
    p_clean.add_argument('--level', type=int, choices=(1, 2, 3), default=1,
                         help='1 clean, 2 prime, 3 wipe and spit; '
                              'each uses progressively more ink')
    sub.add_parser('align', help='align the cartridges (prints a page)')
    p_page = sub.add_parser('page', help="print one of the printer's own pages")
    p_page.add_argument('which', choices=[k for k in INTERNAL_PAGES
                                          if k != 'align'])
    args = ap.parse_args()

    if not args.ip:
        ap.error('no printer IP: pass --ip or set PRINTER_IP in etc/kerchunk.conf')

    if args.action == 'clean':
        value, style = CLEAN_LEVELS[args.level], 1
        oid, what = OID_CLEAN, 'clean level %d (%s)' % (args.level,
                                                        CLEAN_NAMES[args.level])
    elif args.action == 'align':
        value, style, oid, what = INTERNAL_PAGES['align'], 0, OID_INTERNAL_PAGE, \
            'cartridge alignment'
    else:
        value, style, oid = INTERNAL_PAGES[args.which], 0, OID_INTERNAL_PAGE
        what = '%s page' % args.which

    data = build_embedded_pml(oid, value, TYPE_ENUMERATION, style=style)

    if args.dry_run:
        print('%s -> OID %s value %d style %d, %d bytes'
              % (what, oid, value, style, len(data)))
        print(data.hex())
        print(repr(data))
        return 0

    print('%s: sending to %s:%d ...' % (what, args.ip, args.port))
    try:
        send(args.ip, data, args.port)
    except OSError as exc:
        print('failed: %s' % exc, file=sys.stderr)
        return 1
    print('sent. The printer works asynchronously; give it a minute.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
