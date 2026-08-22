#!/usr/bin/env python3
"""
SCL (Scanner Control Language) client for the HP Photosmart C4380.

Talks straight to the scanner over TCP port 9290. No HP driver, no HPLIP,
no binary plugin, no Rosetta -- the whole protocol is open and the device
answers it directly. See docs/scanning-findings.md for how this was
established.

Protocol reference: HPLIP scan/sane/{scl.h,scl.c,sclpml.c,mfpdtf.c}.

Usable as a library (scan_to_jpeg / Scanner) or from the command line:

    ./scl.py --probe
    ./scl.py --dpi 300 --mode color --out page.jpg
"""

import argparse
import socket
import struct
import sys
import time
import zlib

# ---------------------------------------------------------------- SCL commands

def SCL_CMD(a, b):
    """Encode an SCL command. See SCL_CMD() in HPLIP scan/sane/scl.h."""
    return (((ord('*') - ord('!') + 1) << 10)
            + ((ord(a) - ord('`') + 1) << 5)
            + (ord(b) - ord('@') + 1))


CMD_RESET                   = SCL_CMD('z', 'E')   # special-cased: bare ESC E
CMD_CLEAR_ERROR_STACK       = SCL_CMD('o', 'E')   # special-cased: no parameter
CMD_INQUIRE_PRESENT_VALUE   = SCL_CMD('s', 'R')
CMD_INQUIRE_MINIMUM_VALUE   = SCL_CMD('s', 'L')
CMD_INQUIRE_MAXIMUM_VALUE   = SCL_CMD('s', 'H')
CMD_INQUIRE_DEVICE_PARAMETER = SCL_CMD('s', 'E')

CMD_SET_OUTPUT_DATA_TYPE    = SCL_CMD('a', 'T')
CMD_SET_DATA_WIDTH          = SCL_CMD('a', 'G')
CMD_SET_MFPDTF              = SCL_CMD('m', 'S')
CMD_SET_COMPRESSION         = SCL_CMD('a', 'C')
CMD_SET_JPEG_FACTOR         = SCL_CMD('m', 'Q')
CMD_SET_X_RESOLUTION        = SCL_CMD('a', 'R')
CMD_SET_Y_RESOLUTION        = SCL_CMD('a', 'S')
CMD_SET_X_POSITION          = SCL_CMD('f', 'X')
CMD_SET_Y_POSITION          = SCL_CMD('f', 'Y')
CMD_SET_X_EXTENT            = SCL_CMD('f', 'P')
CMD_SET_Y_EXTENT            = SCL_CMD('f', 'Q')
CMD_SET_CONTRAST            = SCL_CMD('a', 'K')
CMD_SET_SHARPENING          = SCL_CMD('a', 'N')
CMD_SCAN_WINDOW             = SCL_CMD('f', 'S')   # starts the scan

# Inquiry targets that take a parameter id rather than a command id.
INQ_CURRENT_ERROR_STACK = 257
INQ_CURRENT_ERROR       = 259
INQ_PIXELS_PER_SCAN_LINE = 1024
INQ_BYTES_PER_SCAN_LINE  = 1025
INQ_NUMBER_OF_SCAN_LINES = 1026
INQ_DEVICE_PIXELS_PER_INCH = 1028

DATA_TYPE = {'lineart': 0, 'gray': 4, 'color': 5, 'color48': 5}
DATA_WIDTH = {'lineart': 1, 'gray': 8, 'color': 24, 'color48': 48}

# 48-bit colour ("billions of colours") is real on this device -- it accepts
# data widths 24, 36 and 48, and rejects 30 and 42 -- but only at its native
# optical steps. Asking for 48-bit at 75/100/150/200 dpi is refused with SCL
# error 2, even though the resolution inquiry still reports a 75-1200 range.
DEEP_COLOUR_RESOLUTIONS = (300, 600, 1200)
MIN_DEEP_COLOUR_DPI = 300

# 16-bit samples arrive little-endian; PNG and PDF both want big-endian.
DEEP_COLOUR_LITTLE_ENDIAN = True

MFPDTF_OFF, MFPDTF_ON = 0, 2
COMPRESSION_NONE, COMPRESSION_JPEG = 0, 2

# SCL's JPEG knob is a COMPRESSION factor (0-100), not a quality one: 0 is the
# least compressed and best looking, 100 the most compressed and blockiest.
# HPLIP's own "SAFER" value is 10, chosen to dodge a firmware assert on the
# OfficeJet 600 series rather than for image quality. Default to 0 here --
# these are documents, and the size difference is not worth visible blocking.
DEFAULT_JPEG_FACTOR = 0

# SCL positions and extents are expressed in device units, not in pixels at
# the requested resolution: the C4380 reports a constant 2550 x 3507 maximum
# whatever DPI is selected. The unit is 1/DEVICE_PIXELS_PER_INCH inch, which
# the device reports as 300 -- so 2550 x 3507 is an 8.5" x 11.69" platen.
# Always ask rather than assume; see Scanner.capabilities().
DEFAULT_DEVICE_PPI = 300
SCAN_PORT = 9290

# MFPDTF record ids, from enum MfpdtfImageRecordID_e.
ID_START_PAGE, ID_RASTER_DATA, ID_END_PAGE = 0, 1, 2
DT_SCANNED_IMAGES = 2

# enum MfpdtfImageEncoding_e
ENCODING_NAMES = {0: 'bitmap', 1: 'graymap', 5: 'rgb', 7: 'jpeg'}


class SclError(Exception):
    pass


def _decode(cmd):
    """Split an encoded command back into its three escape-sequence chars."""
    punc = chr(((cmd >> 10) & 0x1F) + ord('!') - 1)
    letter1 = chr(((cmd >> 5) & 0x1F) + ord('`') - 1)
    letter2 = chr((cmd & 0x1F) + ord('@') - 1)
    return punc, letter1, letter2


class Scanner:
    """One SCL session against the scanner."""

    def __init__(self, ip, port=SCAN_PORT, timeout=30, debug=False, retries=8):
        self.ip, self.port, self.debug = ip, port, debug
        self._buf = b''
        # The JetDirect scan channel answers with a short numeric banner.
        # "00" means the channel is ours; a non-zero banner ("01") means the
        # scanner has not finished releasing the previous session yet. hpmud
        # hits the same thing -- jd.c carries the note "Delay for back-to-back
        # scanning using scanimage. Otherwise next channel_open() can fail."
        # So retry rather than failing the job.
        for attempt in range(retries):
            sock = socket.create_connection((ip, port), timeout=10)
            sock.settimeout(timeout)
            try:
                greeting = sock.recv(64)
            except socket.timeout:
                greeting = b''
            banner = greeting.strip()
            try:
                busy = bool(banner) and int(banner) != 0
            except ValueError:
                raise SclError('unexpected scan-channel greeting %r' % greeting)
            if not busy:
                self.sock = sock
                return
            sock.close()
            if self.debug:
                print('[scl] scanner busy (%r), retrying' % banner, file=sys.stderr)
            time.sleep(1.5)
        raise SclError('scanner still busy after %d attempts (last banner %r) '
                       '-- another scan may be in progress' % (retries, banner))

    # -- plumbing ---------------------------------------------------------

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _log(self, *a):
        if self.debug:
            print('[scl]', *a, file=sys.stderr)

    def _read(self, n):
        """Read exactly n bytes, drawing on anything already buffered."""
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise SclError('scanner closed the connection (wanted %d more '
                               'bytes)' % (n - len(self._buf)))
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_until(self, stop, limit=64):
        """Read one byte at a time until a byte in `stop` is seen."""
        out = b''
        while len(out) < limit:
            b = self._read(1)
            out += b
            if b in stop:
                return out
        raise SclError('no terminator in response %r' % out)

    def send(self, cmd, param=0):
        punc, letter1, letter2 = _decode(cmd)
        if cmd == CMD_RESET:
            seq = ('\x1b%s' % letter2).encode('latin-1')
        elif cmd == CMD_CLEAR_ERROR_STACK:
            seq = ('\x1b%s%s%s' % (punc, letter1, letter2)).encode('latin-1')
        else:
            seq = ('\x1b%s%s%d%s'
                   % (punc, letter1, param, letter2)).encode('latin-1')
        self._log('->', seq)
        self.sock.sendall(seq)

    def inquire(self, cmd, param):
        """Send an inquiry and parse the reply. Returns int, or None if the
        device answers 'null' (parameter unsupported)."""
        self.send(cmd, param)
        punc, letter1, letter2 = _decode(cmd)
        # The reply echoes the request with letter2 shifted into lower case;
        # 'q' is decremented to 'p'. See SclInquire() in scl.c.
        echo = chr(ord(letter2) - ord('A') + ord('a') - 1)
        if echo == 'q':
            echo = 'p'
        expected = ('\x1b%s%s%d%s' % (punc, letter1, param, echo)).encode('latin-1')
        got = self._read(len(expected))
        if got != expected:
            raise SclError('inquiry %d/%d: expected prefix %r, got %r'
                           % (cmd, param, expected, got))
        tail = self._read_until(b'VWN')
        self._log('<-', expected + tail)
        if tail == b'N':
            return None
        value = int(tail[:-1])
        if tail.endswith(b'W'):
            # Binary-data response: the integer was a byte count.
            return self._read(value)
        return value

    def check_error(self):
        depth = self.inquire(CMD_INQUIRE_DEVICE_PARAMETER, INQ_CURRENT_ERROR_STACK)
        if depth:
            code = self.inquire(CMD_INQUIRE_DEVICE_PARAMETER, INQ_CURRENT_ERROR)
            raise SclError('scanner reported error %s' % code)

    # -- capabilities -----------------------------------------------------

    def capabilities(self):
        """Read the ranges the device actually supports."""
        q = self.inquire
        caps = {
            'res_min_x':  q(CMD_INQUIRE_MINIMUM_VALUE, CMD_SET_X_RESOLUTION),
            'res_max_x':  q(CMD_INQUIRE_MAXIMUM_VALUE, CMD_SET_X_RESOLUTION),
            'res_min_y':  q(CMD_INQUIRE_MINIMUM_VALUE, CMD_SET_Y_RESOLUTION),
            'res_max_y':  q(CMD_INQUIRE_MAXIMUM_VALUE, CMD_SET_Y_RESOLUTION),
            'max_x_extent': q(CMD_INQUIRE_MAXIMUM_VALUE, CMD_SET_X_EXTENT),
            'max_y_extent': q(CMD_INQUIRE_MAXIMUM_VALUE, CMD_SET_Y_EXTENT),
            'device_ppi': q(CMD_INQUIRE_DEVICE_PARAMETER,
                            INQ_DEVICE_PIXELS_PER_INCH) or DEFAULT_DEVICE_PPI,
        }
        caps['width_in'] = caps['max_x_extent'] / caps['device_ppi']
        caps['height_in'] = caps['max_y_extent'] / caps['device_ppi']
        return caps

    def sharpen_range(self):
        """Return (min, max) for firmware sharpening, or None if unsupported.

        The C4380 answers -128..127. HP's own driver exposes this as the
        "Sharpen" menu; the other enhancement options it offers (Descreen,
        Colour Restoration, Adaptive Lighting) have no SCL equivalent and are
        done in the driver's own image processing.
        """
        lo = self.inquire(CMD_INQUIRE_MINIMUM_VALUE, CMD_SET_SHARPENING)
        hi = self.inquire(CMD_INQUIRE_MAXIMUM_VALUE, CMD_SET_SHARPENING)
        if lo is None or hi is None:
            return None
        return lo, hi

    # -- scanning ---------------------------------------------------------

    def scan(self, dpi=300, mode='color', jpeg=True, jpeg_factor=DEFAULT_JPEG_FACTOR,
             contrast=0, area=None, sharpen=0):
        """Run one flatbed scan. Returns (payload_bytes, info dict).

        With jpeg=True the payload is a JPEG file; otherwise it is raw
        raster whose geometry is described by info['start_page'].
        `area` is (x, y, w, h) in inches; default is the full platen.

        `jpeg_factor` is the SCL *compression* factor, not a quality value:
        higher means more compression and worse output. Measured on this
        device at 75 dpi colour, same page:

            factor    0 -> 406025 bytes      factor  50 ->  42793
            factor    5 -> 134260            factor  75 ->  34593
            factor   10 ->  98038            factor  90 ->  31450
            factor   25 ->  62194            factor 100 ->  29667
        """
        if mode not in DATA_TYPE:
            raise ValueError('mode must be one of %s' % sorted(DATA_TYPE))

        self.send(CMD_RESET)
        self.send(CMD_CLEAR_ERROR_STACK)
        caps = self.capabilities()

        dpi = max(caps['res_min_x'] or 75, min(dpi, caps['res_max_x'] or 1200))
        if mode == 'color48' and dpi < MIN_DEEP_COLOUR_DPI:
            self._log('48-bit colour needs >= %d dpi; raising from %d'
                      % (MIN_DEEP_COLOUR_DPI, dpi))
            dpi = MIN_DEEP_COLOUR_DPI

        ppi = caps['device_ppi']
        if area is None:
            x0 = y0 = 0
            w, h = caps['max_x_extent'], caps['max_y_extent']
        else:
            ax, ay, aw, ah = area
            x0, y0 = int(ax * ppi), int(ay * ppi)
            w, h = int(aw * ppi), int(ah * ppi)
            w = min(w, caps['max_x_extent'] - x0)
            h = min(h, caps['max_y_extent'] - y0)
        if w <= 0 or h <= 0:
            raise SclError('scan area is empty')

        self.send(CMD_SET_OUTPUT_DATA_TYPE, DATA_TYPE[mode])
        self.send(CMD_SET_DATA_WIDTH, DATA_WIDTH[mode])
        self.send(CMD_SET_MFPDTF, MFPDTF_ON)
        # Lineart has no meaningful JPEG representation.
        # The device JPEG-encodes only 8-bit-per-channel data: lineart and
        # 48-bit colour always come back as raw raster.
        use_jpeg = jpeg and mode not in ('lineart', 'color48')
        self.send(CMD_SET_COMPRESSION,
                  COMPRESSION_JPEG if use_jpeg else COMPRESSION_NONE)
        if use_jpeg:
            self.send(CMD_SET_JPEG_FACTOR, jpeg_factor)
        self.send(CMD_SET_X_RESOLUTION, dpi)
        self.send(CMD_SET_Y_RESOLUTION, dpi)
        self.send(CMD_SET_X_POSITION, x0)
        self.send(CMD_SET_Y_POSITION, y0)
        self.send(CMD_SET_X_EXTENT, w)
        self.send(CMD_SET_Y_EXTENT, h)
        # The C4380 has no contrast control and pushes an error onto the stack
        # if asked to set one, which aborts the job. Only send it when the
        # device admits to supporting the parameter.
        if contrast and self.inquire(CMD_INQUIRE_MAXIMUM_VALUE,
                                     CMD_SET_CONTRAST) is not None:
            self.send(CMD_SET_CONTRAST, contrast)
        # Sharpening, unlike contrast, IS implemented in this scanner's
        # firmware: it reports a -128..127 range and the effect is visible.
        # Guard it the same way regardless, so other models degrade quietly.
        if sharpen:
            limits = self.sharpen_range()
            if limits:
                lo, hi = limits
                self.send(CMD_SET_SHARPENING, max(lo, min(hi, sharpen)))
        self.check_error()

        info = {
            'dpi': dpi, 'mode': mode, 'jpeg': use_jpeg,
            'pixels_per_line': self.inquire(CMD_INQUIRE_DEVICE_PARAMETER,
                                            INQ_PIXELS_PER_SCAN_LINE),
            'bytes_per_line': self.inquire(CMD_INQUIRE_DEVICE_PARAMETER,
                                           INQ_BYTES_PER_SCAN_LINE),
            'lines': self.inquire(CMD_INQUIRE_DEVICE_PARAMETER,
                                  INQ_NUMBER_OF_SCAN_LINES),
        }
        self._log('scan geometry', info)

        self.send(CMD_SCAN_WINDOW, 0)          # go
        payload, page = self._read_mfpdtf()

        # The C4380 sends a start-of-page record with every geometry field
        # zeroed, so the SCL inquiries above are the real source of truth.
        # (HPLIP calls the same situation "simulated image headers".) The
        # end-of-page row count, when present, beats the predicted one.
        page = page or {}
        if not page.get('pixels_per_row'):
            page['pixels_per_row'] = info['pixels_per_line']
        if not page.get('bits_per_pixel'):
            page['bits_per_pixel'] = DATA_WIDTH[mode]
        if not page.get('rows'):
            page['rows'] = info['lines']
        page.setdefault('encoding', 'jpeg' if use_jpeg else 'raster')
        page.setdefault('xres', dpi)
        page.setdefault('yres', dpi)
        info['start_page'] = page
        return payload, info

    def _read_mfpdtf(self):
        """Unwrap the MFPDTF stream into a single payload.

        Layout, from mfpdtf.h: an 8-byte fixed header (blockLength[4],
        headerLength[2], dataType, pageFlags), then headerLength-8 bytes of
        variant header, then inner records each introduced by a one-byte id.
        """
        data = bytearray()
        page = None
        while True:
            try:
                head = self._read(8)
            except SclError:
                break                                   # stream ended
            block_len, header_len, dtype, flags = struct.unpack('<IHBB', head)
            if header_len < 8 or block_len < header_len:
                raise SclError('bad MFPDTF header: block=%d header=%d'
                               % (block_len, header_len))
            self._read(header_len - 8)                  # variant header
            remaining = block_len - header_len
            if dtype != DT_SCANNED_IMAGES:
                self._read(remaining)                   # not ours; skip
                continue

            end_of_page = False
            while remaining > 0:
                rec_id = self._read(1)[0]
                remaining -= 1
                if rec_id == ID_RASTER_DATA:
                    _traits, count = struct.unpack('<BH', self._read(3))
                    remaining -= 3
                    data += self._read(count)
                    remaining -= count
                elif rec_id == ID_START_PAGE:
                    rec = self._read(35)
                    remaining -= 35
                    encoding = rec[0]
                    black = struct.unpack('<HHIII', rec[3:19])
                    color = struct.unpack('<HHIII', rec[19:35])
                    src = color if color[0] else black
                    page = {
                        'encoding': ENCODING_NAMES.get(encoding, encoding),
                        'pixels_per_row': src[0], 'bits_per_pixel': src[1],
                        'rows': src[2], 'xres': src[3], 'yres': src[4],
                    }
                    self._log('start page', page)
                elif rec_id == ID_END_PAGE:
                    rec = self._read(11)
                    remaining -= 11
                    black_rows, color_rows = struct.unpack('<II', rec[3:11])
                    if page is not None:
                        page['rows'] = color_rows or black_rows or page['rows']
                    self._log('end page rows=%d/%d' % (black_rows, color_rows))
                    end_of_page = True
                else:
                    raise SclError('unknown MFPDTF record id %d' % rec_id)
            if end_of_page:
                break
        if not data:
            raise SclError('scanner returned no image data')
        return bytes(data), page


def png_encode(raster, width, height, bits_per_pixel, dpi=None,
               little_endian=False):
    """Encode raw scanner raster as PNG. Pure stdlib, no re-sampling.

    The device emits 1, 8 or 24 bits per pixel with rows padded to whole
    bytes -- exactly PNG's own row layout, so the rows pass through unchanged
    behind a per-row filter byte. Lineart polarity matches too: the scanner
    sets a bit for white, and in PNG greyscale 0 is black, so no inversion.
    """
    colour_type = {1: 0, 8: 0, 16: 0, 24: 2, 48: 2}.get(bits_per_pixel)
    if colour_type is None:
        raise SclError('cannot PNG-encode %d bits per pixel' % bits_per_pixel)
    depth = {1: 1, 8: 8, 16: 16, 24: 8, 48: 16}[bits_per_pixel]
    stride = (width * bits_per_pixel + 7) // 8

    # The scanner emits 16-bit samples little-endian; PNG requires network
    # byte order. Established by measuring which byte varies smoothly along a
    # scanline -- the high byte does, the low byte is noise.
    if depth == 16 and little_endian:
        buf = bytearray(raster)
        buf[0::2], buf[1::2] = buf[1::2], buf[0::2]
        raster = bytes(buf)

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))

    out = bytearray(b'\x89PNG\r\n\x1a\n')
    out += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth,
                                      colour_type, 0, 0, 0))
    if dpi:
        # pHYs is in pixels per metre.
        ppm = int(round(dpi / 0.0254))
        out += chunk(b'pHYs', struct.pack('>IIB', ppm, ppm, 1))

    raw = bytearray()
    for y in range(height):
        row = raster[y * stride:(y + 1) * stride]
        if len(row) < stride:                    # tolerate a short final row
            row = row + b'\x00' * (stride - len(row))
        raw += b'\x00' + row                     # filter type 0 (None)
    out += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
    out += chunk(b'IEND', b'')
    return bytes(out)


def write_pnm(path, raster, page):
    """Write raw raster out as a PNM so it is viewable without extra tools."""
    bpp = page['bits_per_pixel']
    width, rows = page['pixels_per_row'], page['rows']
    if bpp == 48:
        raise SclError('PNM output does not cover 48-bit; use PNG')
    if bpp == 24:
        magic, maxval = b'P6', b'255\n'
    elif bpp == 8:
        magic, maxval = b'P5', b'255\n'
    elif bpp == 1:
        # PNM P4 is the opposite polarity to the scanner: there a set bit
        # means black, here it means white. Invert on the way out.
        magic, maxval = b'P4', b''
        raster = bytes(b ^ 0xFF for b in raster)
    else:
        raise SclError('cannot write %d-bit raster as PNM' % bpp)
    with open(path, 'wb') as fh:
        fh.write(magic + b'\n' + (b'%d %d\n' % (width, rows)) + maxval)
        fh.write(raster)


def scan_to_jpeg(ip, dpi=300, mode='color', port=SCAN_PORT, **kw):
    """Convenience wrapper: one full-platen scan, returned as JPEG bytes."""
    with Scanner(ip, port) as s:
        payload, info = s.scan(dpi=dpi, mode=mode, jpeg=True, **kw)
    return payload, info


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ip', required=True, help='scanner IP address')
    ap.add_argument('--port', type=int, default=SCAN_PORT)
    ap.add_argument('--probe', action='store_true',
                    help='report capabilities and exit without scanning')
    ap.add_argument('--dpi', type=int, default=300)
    ap.add_argument('--mode', default='color', choices=sorted(DATA_TYPE),
                    help="'color48' is 16 bits per channel; 300 dpi minimum, "
                         'uncompressed, and slow')
    ap.add_argument('--raw', action='store_true',
                    help='request uncompressed raster instead of JPEG')
    ap.add_argument('--jpeg-factor', type=int, default=DEFAULT_JPEG_FACTOR,
                    help='SCL compression factor 0-100; LOWER is better quality')
    ap.add_argument('--sharpen', type=int, default=0,
                    help='firmware sharpening, -128..127 (0 = off)')
    ap.add_argument('--area', help='x,y,w,h in inches (default: full platen)')
    ap.add_argument('--out', help='output file')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    try:
        with Scanner(args.ip, args.port, debug=args.debug) as s:
            if args.probe:
                s.send(CMD_RESET)
                for k, v in s.capabilities().items():
                    print('%-14s %s' % (k, v))
                print('%-14s %s' % ('sharpen_range', s.sharpen_range()))
                return 0
            area = None
            if args.area:
                area = tuple(float(x) for x in args.area.split(','))
                if len(area) != 4:
                    ap.error('--area needs four comma-separated numbers')
            payload, info = s.scan(dpi=args.dpi, mode=args.mode,
                                   jpeg=not args.raw,
                                   jpeg_factor=args.jpeg_factor, area=area,
                                   sharpen=args.sharpen)
    except (SclError, OSError) as exc:
        print('scan failed: %s' % exc, file=sys.stderr)
        return 1

    page = info.get('start_page') or {}
    print('%d bytes, encoding=%s %sx%s @ %s dpi'
          % (len(payload), page.get('encoding'), page.get('pixels_per_row'),
             page.get('rows'), info['dpi']), file=sys.stderr)

    out = args.out
    if not out:
        out = 'scan.jpg' if info['jpeg'] else 'scan.pnm'
    if info['jpeg']:
        with open(out, 'wb') as fh:
            fh.write(payload)
    else:
        write_pnm(out, payload, page)
    print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
