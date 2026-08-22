# Screenshots

`control-page.png` is generated, not hand-captured, so it can be regenerated
after UI changes and contains no real addresses:

    ./bin/escl-server.py --ip <printer> --port 8644 --bind 127.0.0.1 --no-advertise &
    curl -s http://127.0.0.1:8644/ | sed 's/<printer>/192.168.1.50/g' > /tmp/control.html
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
        --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
        --window-size=680,660 --screenshot=/tmp/shot.png file:///tmp/control.html
    magick /tmp/shot.png -trim +repage -bordercolor white -border 24 control-page.png

`printers-and-scanners.png` and `printer-details.png` are hand-captured, with
the other printers blurred and cropped to the relevant pane -- the full window
also showed a Settings sidebar naming the machine's owner, which no technical
screenshot needs.

Still worth adding: **Options & Supplies > Supply Levels**, showing the
tri-colour bar. That is the one that demonstrates the `marker-*` plumbing.

Check any hand-captured shot for hostnames, internal IPs, other devices and
incidental personal names before committing it.
