# Screenshots

All five are hand-captured on macOS, with the other printers blurred and the
Settings sidebar's personal entries mosaiced out. Check any replacement for
hostnames, internal IPs, other devices and incidental personal names before
committing it.

| file | what it shows |
|---|---|
| `printers-and-scanners.png` | the queue in System Settings, with its icon |
| `printer-details.png` | `Kind: HP Photosmart C4380-AirPrint`, no driver |
| `supply-levels.png` | the tri-colour bar — proof the `marker-*` route works |
| `image-capture.png` | scanning from Image Capture |
| `control-page.png` | the built-in status / supplies / maintenance page |

The control page can also be rendered headlessly, which avoids capturing a
real address and can be repeated after UI changes:

    ./bin/escl-server.py --ip <printer> --port 8644 --bind 127.0.0.1 --no-advertise &
    curl -s http://127.0.0.1:8644/ | sed 's/<printer>/192.168.1.50/g' > /tmp/control.html
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
        --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
        --window-size=680,660 --screenshot=/tmp/shot.png file:///tmp/control.html
    magick /tmp/shot.png -trim +repage -bordercolor white -border 24 control-page.png
