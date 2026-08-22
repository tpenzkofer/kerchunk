#!/usr/bin/env python3
"""
eSCL / AirScan front end for the HP Photosmart C4380.

The C4380 speaks SCL and nothing else. Apple's Image Capture, macOS Preview,
sane-airscan on Linux and most mobile scan apps speak eSCL. This server sits
between them: it advertises an eSCL scanner over Bonjour and translates each
job into an SCL session against the real device (see bin/scl.py).

It is the scanning counterpart of the print shim: ippeveprinter is to
Ghostscript/PCL3GUI as this is to scl.py/SCL.

Pure standard library. Runs on macOS (dns-sd) and Linux/Raspberry Pi
(avahi-publish). No SANE, no HPLIP, no vendor binaries.
"""

import argparse
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zlib
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scl

# Optional: the control page needs these, the eSCL service does not.
try:
    import maintenance
except ImportError:
    maintenance = None
try:
    import printer_status
except ImportError:
    printer_status = None

SCAN_NS = 'http://schemas.hp.com/imaging/escl/2011/05/03'
PWG_NS = 'http://www.pwg.org/schemas/2010/12/sm'
ET.register_namespace('scan', SCAN_NS)
ET.register_namespace('pwg', PWG_NS)

# eSCL expresses all geometry in 300ths of an inch, which happens to be
# exactly the C4380's own device unit -- so region values pass straight
# through without conversion.
ESCL_UNITS_PER_INCH = 300

# Lineart comes off the device as raw 1-bit raster rather than JPEG. PNG
# stores that natively and losslessly -- same row padding, same polarity --
# so BlackAndWhite1 is fully supported as long as the output format can
# carry it (PNG or PDF; see Scanner._run for the JPEG case).
COLOR_MODES = {'RGB24': 'color', 'Grayscale8': 'gray', 'BlackAndWhite1': 'lineart',
               'RGB48': 'color48'}
ADVERTISED_COLOR_MODES = ('BlackAndWhite1', 'Grayscale8', 'RGB24', 'RGB48')

# Where the scanner icon is served, and what the Bonjour "representation" TXT
# key points at so clients can show it before they fetch capabilities.
ICON_PATH = '/eSCL/icon.png'
RESOLUTIONS = [75, 100, 150, 200, 300, 600, 1200]


def log(msg):
    print('%s  %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


# ------------------------------------------------------------------ job model

class Job:
    def __init__(self, job_id, settings):
        self.id = job_id
        self.uuid = str(uuid.uuid4())
        self.settings = settings
        self.created = time.time()
        self.done = threading.Event()
        self.data = None
        self.error = None
        self.delivered = False
        self.cancelled = False
        # What we actually produced. Normally the requested format, but a
        # lineart job asked for as JPEG comes back as PNG instead.
        self.content_type = settings['format']

    @property
    def state(self):
        if self.cancelled:
            return 'Canceled'
        if not self.done.is_set():
            return 'Processing'
        return 'Aborted' if self.error else 'Completed'


class Scanner:
    """Serialises access to the hardware: it can only do one scan at a time."""

    def __init__(self, ip, port, model, jpeg_factor=scl.DEFAULT_JPEG_FACTOR):
        self.ip, self.port, self.model = ip, port, model
        self.jpeg_factor = jpeg_factor
        self.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                   'escl://%s/%s' % (ip, model)))
        self.icon = None                 # raw PNG bytes, if an icon was found
        self.sharpen_range = None        # (min, max) if the firmware has it
        self.sharpen = 0
        self.ipp_port = 8632             # for the control page footer only
        self.lock = threading.Lock()
        self.jobs = {}
        self._next_id = 1
        self._id_lock = threading.Lock()
        self.caps = None

    def probe(self):
        """Read the real ranges off the device once, at startup."""
        with scl.Scanner(self.ip, self.port) as s:
            s.send(scl.CMD_RESET)
            s.send(scl.CMD_CLEAR_ERROR_STACK)
            self.caps = s.capabilities()
            self.sharpen_range = s.sharpen_range()
        return self.caps

    def new_job(self, settings):
        with self._id_lock:
            job_id = self._next_id
            self._next_id += 1
        job = Job(job_id, settings)
        self.jobs[str(job_id)] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job):
        s = job.settings
        try:
            with self.lock:
                if job.cancelled:
                    return
                mode, fmt, dpi = s['mode'], s['format'], s['dpi']

                # The device only JPEG-encodes greyscale and colour; lineart
                # always comes back as raw 1-bit raster. PNG carries 1-bit
                # natively and losslessly, so that is the route for it -- but
                # a client asking for lineart *as JPEG* is asking for
                # something JPEG cannot represent, and we have no encoder of
                # our own, so scan greyscale instead and say so.
                if mode == 'lineart' and fmt == 'image/jpeg':
                    log('job %d: lineart cannot be JPEG; scanning greyscale'
                        % job.id)
                    mode = 'gray'
                raw = mode in ('lineart', 'color48') or fmt == 'image/png'

                log('job %d: scanning %s %ddpi %s%s'
                    % (job.id, mode, dpi, fmt, ' (raw)' if raw else ''))
                started = time.time()
                with scl.Scanner(self.ip, self.port, timeout=300) as dev:
                    payload, info = dev.scan(
                        dpi=dpi, mode=mode, jpeg=not raw,
                        jpeg_factor=s.get('compression', self.jpeg_factor),
                        sharpen=s.get('sharpen', self.sharpen),
                        area=s['area'])

                page = info['start_page']
                width, height = page['pixels_per_row'], page['rows']
                bpp = page['bits_per_pixel']
                if raw:
                    if fmt == 'application/pdf':
                        payload = raster_to_pdf(payload, width, height, bpp, dpi)
                    else:
                        payload = scl.png_encode(
                            payload, width, height, bpp, dpi,
                            little_endian=(bpp == 48
                                           and scl.DEEP_COLOUR_LITTLE_ENDIAN))
                        job.content_type = 'image/png'
                elif fmt == 'application/pdf':
                    payload = jpeg_to_pdf(payload, dpi)
                job.data = payload
                log('job %d: %d bytes in %.1fs' %
                    (job.id, len(payload), time.time() - started))
        except Exception as exc:                      # noqa: BLE001
            job.error = str(exc)
            log('job %d FAILED: %s' % (job.id, exc))
        finally:
            job.done.set()


# ------------------------------------------------------------------ PDF wrap

def jpeg_info(data):
    """Return (width, height, components) read from the JPEG's SOF marker.

    The embedding dictionary must agree with the actual stream: declaring
    DeviceRGB for a one-component (grayscale) JPEG makes renderers read three
    bytes per pixel and tile the image sideways.
    """
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], 'big')
        # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5:i + 7], 'big')
            width = int.from_bytes(data[i + 7:i + 9], 'big')
            return width, height, data[i + 9]
        i += 2 + seglen
    raise ValueError('no JPEG frame header found')


def jpeg_to_pdf(jpeg, dpi):
    """Wrap a JPEG in a one-page PDF without re-encoding it (DCTDecode)."""
    width, height, comps = jpeg_info(jpeg)
    colorspace = {1: '/DeviceGray', 3: '/DeviceRGB', 4: '/DeviceCMYK'}.get(comps)
    if colorspace is None:
        raise ValueError('unsupported JPEG component count %d' % comps)
    return _image_pdf(jpeg, width, height, colorspace, 8, '/DCTDecode', dpi)


def raster_to_pdf(raster, width, height, bits_per_pixel, dpi):
    """Wrap raw scanner raster in a one-page PDF, deflated.

    Used for lineart, where 1-bit-per-pixel survives intact: PDF's DeviceGray
    treats 0 as black, which is the polarity the scanner already emits, so the
    rows embed unchanged. A full page of 1-bit text deflates to a fraction of
    what the equivalent JPEG would cost, and stays crisp. Also used for 48-bit
    colour, which PDF stores as 16 bits per component.
    """
    colorspace = '/DeviceRGB' if bits_per_pixel in (24, 48) else '/DeviceGray'
    depth = {1: 1, 8: 8, 16: 16, 24: 8, 48: 16}.get(bits_per_pixel)
    if depth is None:
        raise ValueError('cannot embed %d bits per pixel' % bits_per_pixel)
    if depth == 16 and scl.DEEP_COLOUR_LITTLE_ENDIAN:
        # PDF, like PNG, reads 16-bit samples big-endian.
        buf = bytearray(raster)
        buf[0::2], buf[1::2] = buf[1::2], buf[0::2]
        raster = bytes(buf)
    return _image_pdf(zlib.compress(bytes(raster), 6), width, height,
                      colorspace, depth, '/FlateDecode', dpi)


def _image_pdf(stream, width, height, colorspace, depth, filt, dpi):
    """Build a one-page PDF around one already-encoded image stream."""
    pw = width * 72.0 / dpi
    ph = height * 72.0 / dpi
    objs = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] '
         '/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>'
         % (pw, ph)).encode(),
        (('<< /Type /XObject /Subtype /Image /Width %d /Height %d '
          '/ColorSpace %s /BitsPerComponent %d /Filter %s '
          '/Length %d >>\nstream\n'
          % (width, height, colorspace, depth, filt, len(stream))).encode()
         + stream + b'\nendstream'),
    ]
    content = ('q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q' % (pw, ph)).encode()
    objs.append(b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream')

    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1)
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (len(objs) + 1, xref))
    return bytes(out)


# ------------------------------------------------------------------ eSCL XML

def _text(parent, tag, value):
    el = ET.SubElement(parent, tag)
    el.text = str(value)
    return el


def capabilities_xml(dev, base_url=''):
    caps = dev.caps
    root = ET.Element('{%s}ScannerCapabilities' % SCAN_NS)
    _text(root, '{%s}Version' % PWG_NS, '2.6')
    _text(root, '{%s}MakeAndModel' % PWG_NS, dev.model)
    _text(root, '{%s}SerialNumber' % PWG_NS, dev.ip)
    _text(root, '{%s}UUID' % SCAN_NS, dev.uuid)
    _text(root, '{%s}AdminURI' % SCAN_NS, 'http://%s/' % dev.ip)
    if dev.icon:
        # Clients fetch this to show the scanner in their device list.
        _text(root, '{%s}IconURI' % SCAN_NS, base_url + ICON_PATH)

    platen = ET.SubElement(root, '{%s}Platen' % SCAN_NS)
    caps_el = ET.SubElement(platen, '{%s}PlatenInputCaps' % SCAN_NS)
    _text(caps_el, '{%s}MinWidth' % SCAN_NS, 16)
    _text(caps_el, '{%s}MaxWidth' % SCAN_NS, caps['max_x_extent'])
    _text(caps_el, '{%s}MinHeight' % SCAN_NS, 16)
    _text(caps_el, '{%s}MaxHeight' % SCAN_NS, caps['max_y_extent'])
    _text(caps_el, '{%s}MaxScanRegions' % SCAN_NS, 1)

    profiles = ET.SubElement(caps_el, '{%s}SettingProfiles' % SCAN_NS)
    profile = ET.SubElement(profiles, '{%s}SettingProfile' % SCAN_NS)
    modes = ET.SubElement(profile, '{%s}ColorModes' % SCAN_NS)
    for m in ADVERTISED_COLOR_MODES:
        _text(modes, '{%s}ColorMode' % SCAN_NS, m)
    formats = ET.SubElement(profile, '{%s}DocumentFormats' % SCAN_NS)
    for f in ('image/jpeg', 'image/png', 'application/pdf'):
        _text(formats, '{%s}DocumentFormat' % PWG_NS, f)
        _text(formats, '{%s}DocumentFormatExt' % SCAN_NS, f)
    res_el = ET.SubElement(profile, '{%s}SupportedResolutions' % SCAN_NS)
    discrete = ET.SubElement(res_el, '{%s}DiscreteResolutions' % SCAN_NS)
    lo = caps['res_min_x'] or 75
    hi = caps['res_max_x'] or 1200
    for r in RESOLUTIONS:
        if lo <= r <= hi:
            d = ET.SubElement(discrete, '{%s}DiscreteResolution' % SCAN_NS)
            _text(d, '{%s}XResolution' % SCAN_NS, r)
            _text(d, '{%s}YResolution' % SCAN_NS, r)
    spaces = ET.SubElement(profile, '{%s}ColorSpaces' % SCAN_NS)
    _text(spaces, '{%s}ColorSpace' % SCAN_NS, 'sRGB')

    # Adjustments the hardware genuinely implements. Sharpening is a real
    # SCL command on this device (-128..127); contrast is not, and HP's
    # Descreen / Colour Restoration / Adaptive Lighting have no SCL
    # equivalent at all -- those live in HP's own driver software.
    if dev.sharpen_range:
        lo, hi_s = dev.sharpen_range
        sup = ET.SubElement(caps_el, '{%s}SharpenSupport' % SCAN_NS)
        _text(sup, '{%s}Min' % SCAN_NS, lo)
        _text(sup, '{%s}Max' % SCAN_NS, hi_s)
        _text(sup, '{%s}Normal' % SCAN_NS, 0)
        _text(sup, '{%s}Step' % SCAN_NS, 1)
    comp = ET.SubElement(caps_el, '{%s}CompressionFactorSupport' % SCAN_NS)
    _text(comp, '{%s}Min' % SCAN_NS, 0)
    _text(comp, '{%s}Max' % SCAN_NS, 100)
    _text(comp, '{%s}Normal' % SCAN_NS, dev.jpeg_factor)
    _text(comp, '{%s}Step' % SCAN_NS, 1)

    _text(caps_el, '{%s}MaxOpticalXResolution' % SCAN_NS, hi)
    _text(caps_el, '{%s}MaxOpticalYResolution' % SCAN_NS, hi)
    return ET.tostring(root, encoding='UTF-8', xml_declaration=True)


def status_xml(dev):
    root = ET.Element('{%s}ScannerStatus' % SCAN_NS)
    _text(root, '{%s}Version' % PWG_NS, '2.6')
    busy = dev.lock.locked()
    _text(root, '{%s}State' % PWG_NS, 'Processing' if busy else 'Idle')
    jobs_el = ET.SubElement(root, '{%s}Jobs' % SCAN_NS)
    for job in sorted(dev.jobs.values(), key=lambda j: j.id)[-8:]:
        info = ET.SubElement(jobs_el, '{%s}JobInfo' % SCAN_NS)
        _text(info, '{%s}JobUri' % PWG_NS, '/eSCL/ScanJobs/%d' % job.id)
        _text(info, '{%s}JobUuid' % PWG_NS, job.uuid)
        _text(info, '{%s}Age' % SCAN_NS, int(time.time() - job.created))
        done = job.done.is_set() and not job.error
        _text(info, '{%s}ImagesCompleted' % PWG_NS, 1 if done else 0)
        _text(info, '{%s}ImagesToTransfer' % PWG_NS,
              0 if (job.delivered or not done) else 1)
        _text(info, '{%s}JobState' % PWG_NS, job.state)
        reasons = ET.SubElement(info, '{%s}JobStateReasons' % PWG_NS)
        _text(reasons, '{%s}JobStateReason' % PWG_NS,
              'JobCompletedSuccessfully' if done else job.state)
    return ET.tostring(root, encoding='UTF-8', xml_declaration=True)


def parse_settings(body, dev):
    """Turn a posted ScanSettings document into arguments for scl.py."""
    caps = dev.caps
    settings = {'dpi': 300, 'mode': 'color', 'format': 'image/jpeg', 'area': None}
    if not body:
        return settings
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError('malformed ScanSettings: %s' % exc)

    def find(tag):
        for ns in (PWG_NS, SCAN_NS):
            el = root.find('.//{%s}%s' % (ns, tag))
            if el is not None and el.text:
                return el.text.strip()
        return None

    res = find('XResolution')
    if res:
        settings['dpi'] = int(res)
    mode = find('ColorMode')
    if mode:
        settings['mode'] = COLOR_MODES.get(mode, 'color')
    fmt = find('DocumentFormatExt') or find('DocumentFormat')
    if fmt:
        settings['format'] = fmt.lower()

    sharpen = find('Sharpen')
    if sharpen is not None:
        try:
            settings['sharpen'] = int(sharpen)
        except ValueError:
            pass
    compression = find('CompressionFactor')
    if compression is not None:
        try:
            settings['compression'] = max(0, min(100, int(compression)))
        except ValueError:
            pass

    region = root.find('.//{%s}ScanRegion' % PWG_NS)
    if region is not None:
        def val(tag, default=0):
            el = region.find('{%s}%s' % (PWG_NS, tag))
            try:
                return int(el.text)
            except (AttributeError, TypeError, ValueError):
                return default
        x, y = val('XOffset'), val('YOffset')
        w = val('Width', caps['max_x_extent'])
        h = val('Height', caps['max_y_extent'])
        # eSCL units are 300ths of an inch; scl.py takes inches.
        full = (x == 0 and y == 0
                and w >= caps['max_x_extent'] and h >= caps['max_y_extent'])
        if not full:
            settings['area'] = (x / ESCL_UNITS_PER_INCH, y / ESCL_UNITS_PER_INCH,
                                w / ESCL_UNITS_PER_INCH, h / ESCL_UNITS_PER_INCH)
    return settings


# ------------------------------------------------------------ control page

# Cache the SNMP read briefly: the page can be refreshed freely, but each poll
# is a handful of UDP round trips to a 2007 printer.
_status_cache = {'at': 0.0, 'value': None}
STATUS_TTL = 15.0


def _status(dev):
    if printer_status is None or not dev.ip:
        return None
    now = time.time()
    if _status_cache['value'] and now - _status_cache['at'] < STATUS_TTL:
        return _status_cache['value']
    try:
        value = printer_status.read_status(dev.ip)
    except Exception:                                     # noqa: BLE001
        return None
    _status_cache.update(at=now, value=value)
    return value


def _bar(percent, colours):
    """One supply bar. Multiple colours are striped, so a tri-colour cartridge
    reads as tri-colour -- ippeveprinter's own page cannot do this, because it
    picks a single gradient per supply from the colorant name and renders any
    name it does not know (including "multi-color") as flat black."""
    if len(colours) == 1:
        fill = colours[0]
    else:
        step = 100.0 / len(colours)
        stops = ', '.join(
            '%s %.2f%% %.2f%%' % (c, i * step, (i + 1) * step)
            for i, c in enumerate(colours))
        fill = 'linear-gradient(to bottom, %s)' % stops
    return ('<div class="track"><div class="fill" style="width:%d%%;'
            'background:%s"></div></div>' % (max(0, min(100, percent)), fill))


def control_page(dev, notice=None):
    st = _status(dev)
    rows = []
    if st and st.get('supplies'):
        for supply in st['supplies']:
            pct = supply['percent']
            if pct is None:
                rows.append('<tr><th>%s</th><td colspan="2" class="dim">'
                            'reported as %s, not a percentage</td></tr>'
                            % (_esc(supply['name'].title()), supply['level']))
                continue
            colorant = printer_status._colorant(supply['name'])
            colours = printer_status.MARKER_COLORS.get(
                colorant, '#808080').replace('#', ' #').split()
            rows.append('<tr><th>%s</th><td class="meter">%s</td>'
                        '<td class="pct">%d%%</td></tr>'
                        % (_esc(printer_status._supply_label(supply['name'])),
                           _bar(pct, colours), pct))
    supplies = ('<table>%s</table>' % ''.join(rows)) if rows else \
        '<p class="dim">No supply information (is the printer awake?)</p>'

    if st and st.get('reachable'):
        problems = st['problems'] or ['none']
        facts = [('Model', st.get('model') or '?'),
                 ('State', st.get('state') or '?'),
                 ('Pages printed', st.get('pages') if st.get('pages') is not None else '?'),
                 ('Problems', '; '.join(problems))]
    else:
        facts = [('Printer', dev.ip), ('State', 'not answering SNMP')]
    facts_html = ''.join('<tr><th>%s</th><td>%s</td></tr>' % (_esc(str(k)), _esc(str(v)))
                         for k, v in facts)

    msg = ''
    if notice:
        kind, text = notice
        msg = '<p class="notice %s">%s</p>' % (kind, _esc(text))

    buttons = ''
    if maintenance is not None:
        def form(action, label, extra='', cls=''):
            return ('<form method="POST" action="/maintenance" '
                    'onsubmit="return confirm(\'%s?\\n\\nThis uses ink or paper.\')">'
                    '<input type="hidden" name="action" value="%s">%s'
                    '<button class="%s">%s</button></form>'
                    % (label, action, extra, cls, label))
        buttons = ''.join([
            form('clean', 'Clean heads',
                 '<input type="hidden" name="level" value="1">'),
            form('clean', 'Clean harder',
                 '<input type="hidden" name="level" value="2">'),
            form('clean', 'Clean hardest',
                 '<input type="hidden" name="level" value="3">'),
            form('align', 'Align cartridges'),
            form('supplies', 'Print supplies page'),
            form('print-quality', 'Print-quality page'),
        ])

    return ("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kerchunk - %s</title><style>
:root{color-scheme:light dark}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 margin:0;background:Canvas;color:CanvasText}
.wrap{max-width:640px;margin:0 auto;padding:24px 20px 48px}
h1{font-size:20px;margin:0 0 2px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;opacity:.6;
 margin:28px 0 8px;font-weight:600}
.sub{opacity:.6;margin:0 0 8px;font-size:13px}
table{border-collapse:collapse;width:100%%}
th{text-align:left;font-weight:500;padding:6px 12px 6px 0;white-space:nowrap;
 vertical-align:middle;width:1%%}
td{padding:6px 0;vertical-align:middle}
td.pct{text-align:right;width:1%%;padding-left:12px;font-variant-numeric:tabular-nums}
.track{background:color-mix(in srgb,CanvasText 12%%,transparent);
 border-radius:4px;height:16px;overflow:hidden;min-width:160px}
.fill{height:100%%;border-radius:4px}
.dim{opacity:.55}
.buttons{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.buttons form{margin:0}
button{font:inherit;padding:7px 14px;border-radius:7px;cursor:pointer;
 border:1px solid color-mix(in srgb,CanvasText 25%%,transparent);
 background:color-mix(in srgb,CanvasText 6%%,transparent);color:inherit}
button:hover{background:color-mix(in srgb,CanvasText 14%%,transparent)}
.notice{padding:10px 12px;border-radius:8px;margin:16px 0 0}
.notice.ok{background:color-mix(in srgb,#2c8 22%%,transparent)}
.notice.error{background:color-mix(in srgb,#e44 22%%,transparent)}
footer{margin-top:32px;font-size:12px;opacity:.5}
</style></head><body><div class="wrap">
<h1>%s</h1>
<p class="sub">Kerchunk &middot; %s</p>
%s
<h2>Status</h2><table>%s</table>
<h2>Supplies</h2>%s
<h2>Maintenance</h2>
<p class="sub">Each of these consumes ink or paper.</p>
<div class="buttons">%s</div>
<footer>Scanner: eSCL on this port. Printer: IPP on port %s.</footer>
</div></body></html>""" % (
        _esc(dev.model), _esc(dev.model), _esc(dev.ip), msg, facts_html,
        supplies, buttons, dev.ipp_port)).encode('utf-8')


def _esc(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ------------------------------------------------------------------ HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'kerchunk-escl/1.0'
    device = None                      # set by serve()

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler's default line is noisy; log just the request
        # so it is visible which endpoints clients actually reach for.
        log('%s %s %s' % (self.client_address[0], self.command, self.path))

    # -- helpers ----------------------------------------------------------

    def _send(self, code, body=b'', ctype='text/xml; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != 'HEAD':
            self.wfile.write(body)

    def _job_from_path(self):
        m = re.match(r'^/eSCL/ScanJobs/(\d+)', self.path)
        if not m:
            return None
        return self.device.jobs.get(m.group(1))

    # -- verbs ------------------------------------------------------------

    def do_GET(self):
        dev = self.device
        path = self.path.split('?', 1)[0].rstrip('/')
        if path in ('/eSCL/ScannerCapabilities', '/ScannerCapabilities'):
            host = self.headers.get('Host') or ''
            return self._send(200, capabilities_xml(
                dev, 'http://%s' % host if host else ''))
        if path in ('/eSCL/ScannerStatus', '/ScannerStatus'):
            return self._send(200, status_xml(dev))
        if path in (ICON_PATH, '/icon.png') and dev.icon:
            return self._send(200, dev.icon, 'image/png')
        if path.endswith('/NextDocument'):
            return self._next_document()
        if path in ('', '/', '/eSCL'):
            return self._send(200, control_page(dev, self.pop_notice()),
                              'text/html; charset=utf-8')
        return self._send(404)

    # -- control page -----------------------------------------------------

    notice = None            # class-level: survives the redirect after a POST

    def pop_notice(self):
        text, Handler.notice = Handler.notice, None
        return text

    def do_maintenance(self, body):
        """Run one maintenance action on behalf of the control page."""
        fields = urllib.parse.parse_qs(body.decode('utf-8', 'replace'))
        action = (fields.get('action') or [''])[0]
        if maintenance is None:
            Handler.notice = ('error', 'maintenance.py is not available')
            return
        try:
            if action == 'clean':
                level = int((fields.get('level') or ['1'])[0])
                value = maintenance.CLEAN_LEVELS[level]
                data = maintenance.build_embedded_pml(
                    maintenance.OID_CLEAN, value,
                    maintenance.TYPE_ENUMERATION, style=1)
                what = 'head clean, level %d (%s)' % (
                    level, maintenance.CLEAN_NAMES[level])
            elif action in maintenance.INTERNAL_PAGES:
                style = 0
                data = maintenance.build_embedded_pml(
                    maintenance.OID_INTERNAL_PAGE,
                    maintenance.INTERNAL_PAGES[action],
                    maintenance.TYPE_ENUMERATION, style=style)
                what = ('cartridge alignment' if action == 'align'
                        else '%s page' % action)
            else:
                Handler.notice = ('error', 'unknown action %r' % action)
                return
            maintenance.send(self.device.ip, data)
            log('maintenance: %s sent to %s' % (what, self.device.ip))
            Handler.notice = ('ok', 'Sent: %s. The printer works on its own '
                                    'schedule, so give it a minute.' % what)
        except Exception as exc:                          # noqa: BLE001
            log('maintenance FAILED: %s' % exc)
            Handler.notice = ('error', 'Failed: %s' % exc)

    do_HEAD = do_GET

    def _next_document(self):
        job = self._job_from_path()
        if job is None:
            return self._send(404)
        job.done.wait(timeout=300)
        if not job.done.is_set():
            return self._send(503)
        if job.error:
            return self._send(500)
        if job.delivered:
            # eSCL signals "no more pages in this job" with 404. The platen
            # only ever produces one image, so the second ask always ends it.
            return self._send(404)
        job.delivered = True
        ctype = job.content_type
        log('job %d: delivering %d bytes as %s' % (job.id, len(job.data), ctype))
        return self._send(200, job.data, ctype)

    def do_POST(self):
        dev = self.device
        path = self.path.split('?', 1)[0].rstrip('/')
        if path == '/maintenance':
            length = int(self.headers.get('Content-Length') or 0)
            self.do_maintenance(self.rfile.read(length) if length else b'')
            return self._send(303, b'', 'text/plain', {'Location': '/'})
        if path not in ('/eSCL/ScanJobs', '/ScanJobs'):
            return self._send(404)
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        try:
            settings = parse_settings(body, dev)
        except ValueError as exc:
            log('rejected job: %s' % exc)
            return self._send(409)
        job = dev.new_job(settings)
        host = self.headers.get('Host') or ('%s:%d' % self.server.server_address)
        location = 'http://%s/eSCL/ScanJobs/%d' % (host, job.id)
        log('job %d: created (%s)' % (job.id, settings))
        return self._send(201, b'', 'text/plain', {'Location': location})

    def do_DELETE(self):
        job = self._job_from_path()
        if job is None:
            return self._send(404)
        job.cancelled = True
        log('job %d: cancelled by client' % job.id)
        return self._send(200)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------ Bonjour

def advertise(name, port, dev):
    """Publish _uscan._tcp, using whichever tool this platform provides.

    macOS ships dns-sd; Debian/Raspberry Pi OS ship avahi-publish. Returns the
    child process, or None if neither tool is present.
    """
    txt = {
        'txtvers': '1', 'vers': '2.6', 'ty': dev.model, 'rs': 'eSCL',
        'representation': ICON_PATH if dev.icon else '',
        'pdl': 'image/jpeg,image/png,application/pdf',
        'cs': 'color,grayscale,binary', 'is': 'platen', 'duplex': 'F',
        'uuid': dev.uuid, 'adminurl': 'http://%s/' % dev.ip, 'note': '',
    }
    if shutil.which('dns-sd'):
        cmd = ['dns-sd', '-R', name, '_uscan._tcp', 'local', str(port)]
        cmd += ['%s=%s' % kv for kv in txt.items()]
    elif shutil.which('avahi-publish'):
        cmd = ['avahi-publish', '-s', name, '_uscan._tcp', str(port)]
        cmd += ['%s=%s' % kv for kv in txt.items()]
    else:
        log('WARNING: neither dns-sd nor avahi-publish found — the scanner '
            'will not be discoverable. Install avahi-utils on Linux.')
        return None
    log('advertising "%s" as _uscan._tcp on port %d via %s' % (name, port, cmd[0]))
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)


# ------------------------------------------------------------------ entry

def load_conf(path):
    """Read the shell-style etc/kerchunk.conf without sourcing it."""
    conf = {}
    if not os.path.exists(path):
        return conf
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            conf[key.strip()] = value.strip().strip('"').strip("'")
    return conf


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf = load_conf(os.path.join(here, 'etc', 'kerchunk.conf'))

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ip', default=conf.get('PRINTER_IP'),
                    help='scanner IP address')
    ap.add_argument('--scan-port', type=int,
                    default=int(conf.get('SCAN_PORT') or scl.SCAN_PORT))
    ap.add_argument('--port', type=int, default=int(conf.get('ESCL_PORT') or 8633),
                    help='port for this eSCL server')
    ap.add_argument('--bind', default=conf.get('ESCL_BIND', ''),
                    help='address to bind (default: all interfaces)')
    ap.add_argument('--name', default=conf.get('SCAN_SERVICE_NAME')
                    or 'Photosmart C4380 (Scanner)')
    ap.add_argument('--model', default=conf.get('SCAN_MODEL')
                    or 'HP Photosmart C4380')
    ap.add_argument('--jpeg-quality', type=int,
                    default=int(conf.get('SCAN_JPEG_QUALITY') or 100),
                    help='0-100, higher is better. Sent to the device as its '
                         'inverse, because SCL takes a compression factor '
                         'where higher means blockier.')
    ap.add_argument('--no-advertise', action='store_true',
                    help='serve HTTP but do not publish over Bonjour')
    ap.add_argument('--sharpen', type=int, default=int(conf.get('SCAN_SHARPEN') or 0),
                    help='firmware sharpening default, -128..127 (0 = off)')
    ap.add_argument('--icon', default=conf.get('SCANNER_ICON')
                    or os.path.join(here, 'share', 'icons', 'scanner.png'),
                    help='PNG shown by clients in their scanner list')
    args = ap.parse_args()

    if not args.ip:
        ap.error('no scanner IP: pass --ip or set PRINTER_IP in etc/kerchunk.conf')

    # SCL wants a compression factor, where higher means worse. Invert the
    # user-facing quality value so the config reads the way people expect.
    compression = max(0, min(100, 100 - args.jpeg_quality))
    dev = Scanner(args.ip, args.scan_port, args.model, compression)
    dev.sharpen = args.sharpen
    dev.ipp_port = conf.get('IPP_PORT', '8632')
    if args.icon and os.path.isfile(args.icon):
        with open(args.icon, 'rb') as fh:
            dev.icon = fh.read()
        log('serving icon %s (%d bytes) at %s'
            % (args.icon, len(dev.icon), ICON_PATH))
    try:
        caps = dev.probe()
    except (scl.SclError, OSError) as exc:
        print('cannot reach the scanner at %s:%d — %s'
              % (args.ip, args.scan_port, exc), file=sys.stderr)
        return 1
    log('scanner ready: %.2f x %.2f in platen, %d-%d dpi, sharpening %s'
        % (caps['width_in'], caps['height_in'],
           caps['res_min_x'], caps['res_max_x'],
           '%d..%d' % dev.sharpen_range if dev.sharpen_range else 'unsupported'))

    Handler.device = dev
    httpd = ThreadedHTTPServer((args.bind, args.port), Handler)

    advert = None
    if not args.no_advertise:
        advert = advertise(args.name, args.port, dev)

    log('eSCL server listening on %s:%d'
        % (args.bind or '0.0.0.0', args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log('shutting down')
    finally:
        if advert:
            advert.terminate()
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
