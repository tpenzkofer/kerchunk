# Scanning: what the C4380 actually speaks

Research notes. Everything below is verified against a real device
unless marked otherwise.

## The current path, and why it expires

macOS scans this device through a **third-party ICA plugin**:

    /Library/Image Capture/Devices/HPScanner.app       v1.10.3, built 2017

- It matches the device by Bonjour `ty = "Photosmart C4380 series"`
  (`Contents/Resources/DeviceMatchingInfo.plist`), so it is already scanning
  over the network — there is no USB cable attached.
- Its binaries are **x86_64 only**:
  `HPScan.framework`, `PlugIns/HPAiOScan.bundle` — no arm64 slice.
- `HPAiOScan.bundle/Contents/Resources/ModelInfo.plist` declares for
  `Photosmart C4380 series`: `ScanProtocol = HDT`, `hasFlatbed = 1`,
  `hasADF = 0`, `maxDpiX = 1200`, `maxDpiY = 2400`, `nativeDPI = 300`.

Same two clocks as the print queue: it is a `/Library` vendor plugin, and it
is Rosetta-dependent. Apple's own driverless scanner client, by contrast, is
`/System/Library/Image Capture/Devices/AirScanScanner.app` — universal
(x86_64 + arm64e), Apple-maintained. That is the target to land on.

## What the hardware speaks

Open ports on the device:

    80    embedded web server ("HP Photosmart C4380 series")
    9100  raw JetDirect print       <- the print pipeline uses this
    9220  HP GGW generic gateway    -> "220 HP GGW server (version 1.0) ready"
    9290  SCL scan channel          <- the scanner lives here
    9500  (unidentified)

Port mapping comes from HPLIP `io/hpmud/jd.c`:

    PrintPort[]   = { 0, 9100, 9101, 9102 }
    ScanPort0[]   = { 0, 9290, 9291, 9292 }   <- HPMUD_SCAN_CHANNEL
    GenericPort[] = { 0, 9220, 9221, 9222 }   <- memory card / fax / config

HPLIP `data/models/models.dat`, entry `[photosmart_c4380_series]`:

    scan-type=1        -> HPMUD_SCANTYPE_SCL  -> sclpml_open()
    scan-src=1         -> flatbed only, no ADF
    plugin=0           -> NO proprietary binary plugin required
    plugin-reason=0
    tech-class=DJGenericVIP

`plugin=0` is the important line. HPLIP's `escl`, `marvell`, `orblite`,
`soap` and `soapht` handlers all need HP's closed-source blob, which ships
as a Linux ELF and would be a dead end on arm64 macOS. **SCL does not.**
The entire protocol is open source in `scan/sane/scl.c` + `sclpml.c`.

## SCL wire format

From `scan/sane/scl.h` and `scl.c`:

    SCL_CMD(a,b) = (('*'-'!'+1)<<10) + ((a-'`'+1)<<5) + (b-'@'+1)

A command is an escape sequence `ESC <punc> <letter1> <param> <letter2>`,
with two special cases: reset is bare `ESC E`, and clear-error-stack omits
the parameter. An inquiry echoes back the same prefix with `letter2` mapped
to `letter2 - 'A' + 'a' - 1` (and `'q'` decremented to `'p'`), followed by
the value.

Relevant commands:

    inquire present value   ESC * s <cmd> R
    inquire minimum value   ESC * s <cmd> L
    inquire maximum value   ESC * s <cmd> H
    set X resolution        ESC * a <n> R      (cmd id 10323)
    set Y resolution        ESC * a <n> S      (cmd id 10324)
    set X extent            ESC * a <n> P      (cmd id 10321)
    set Y extent            ESC * a <n> Q      (cmd id 10322)
    set output data type    ESC * a <n> T      (cmd id 10325)
    set data width          ESC * a <n> G
    set compression         ESC * a <n> C
    set MFPDTF              ESC * m <n> S
    start scan window       ESC * f <n> S

## Verified live

Connected to the printer on port 9290, greeting `00`, then:

    req  \x1b*s10323R   resp  \x1b*s10323p300V     X resolution now  = 300
    req  \x1b*s10323H   resp  \x1b*s10323g1200V    X resolution max  = 1200
    req  \x1b*s10324H   resp  \x1b*s10324g2400V    Y resolution max  = 2400
    req  \x1b*s10321H   resp  \x1b*s10321g6120V    X extent max      = 6120
    req  \x1b*s10322H   resp  \x1b*s10322g8417V    Y extent max      = 8417
    req  \x1b*s10325R   resp  \x1b*s10325p0V       output data type  = 0

Extents are in 1/720", so 6120 = 8.5" and 8417 = 11.69" — a full A4 /
Letter flatbed. Resolution maxima match HP's own `ModelInfo.plist` exactly,
which confirms the reading is correct.

**The scanner is fully drivable from a plain TCP socket, today, with no
vendor code, no Rosetta, and no binary blob.**

## What is left to build

1. **SCL session + MFPDTF unwrap.** Setting parameters is trivial (above).
   The returned raster is wrapped in HP's MFPDTF framing — records with a
   header giving type and length. Reference: HPLIP `scan/sane/mfpdtf.c`.
   The device can emit JPEG directly (`set compression`), which avoids
   most of the raw-raster handling.
2. **A minimal eSCL/AirScan server.** Three endpoints are enough for
   Image Capture: `GET /eSCL/ScannerCapabilities`, `POST /eSCL/ScanJobs`,
   `GET /eSCL/ScanJobs/<id>/NextDocument`.
3. **Bonjour advertisement** of `_uscan._tcp` with TXT keys `rs=eSCL`,
   `ty`, `pdl=image/jpeg,application/pdf`, `is=platen`, `duplex=F`,
   `cs=color,grayscale`, `UUID`.

That is the exact mirror of the print side: `ippeveprinter` ⟷ eSCL server,
Ghostscript→PCL3GUI ⟷ SCL→JPEG, `_ipp._tcp` ⟷ `_uscan._tcp`.

No deadline pressure: `HPScanner.app` still works, so this can be built and
tested alongside it without breaking anything.
