# Neat ADF scanner for Home Assistant

Home Assistant integration for the **Neat ADF scanner** (USB `1f44:0050`) shared over the
network with [USBAnywhere](https://github.com/oscarlosu-lab/USBAnywhere) (Android app) or any
standard `usbipd` server.

> **Status: working.** Scanning has been verified on real hardware (Neat ADF scanner shared by
> USBAnywhere on Android): 300 and 600 dpi color scans, plus derived modes. The Neat protocol is
> proprietary and not in SANE; it was reverse engineered from a USB capture of the Windows driver
> (see [docs/PROTOCOL.md](docs/PROTOCOL.md)).

> **Calibration is per scanner.** The 300 dpi scan programs contain the shading tables of the
> author's own unit. They work on other units but the brightness may be uneven. See
> [Use your own calibration](#use-your-own-calibration) to capture and install the values of your
> scanner.

## Features

- **Scanner button**: an event entity (`pressed`) plus the `neat_scanner_button` event; by default
  pressing the button on the scanner starts a scan (option *Scan when the scanner button is pressed*)
- **Paper in feeder** and **cover** binary sensors, polled four times a second
- **Double-sided scanning** (switch, option and `duplex` service field): each sheet gives a front
  page followed by a back page, up to 300 dpi
- Online/offline binary sensor (polls the USB/IP server and checks the scanner is exported)
- **Calibrate** button and `neat_scanner.calibrate` service: scan a blank white letter sheet to level
  the sensor segments (fixes a dim left edge and brightness steps between regions)
- **Straighten skewed pages** (default on) and an experimental **upside-down** correction (default off)
- Scan button plus **DPI** (any value from 75 to 600) and **color mode** (color/grayscale/black and white) selectors
- `neat_scanner.scan` service with `dpi`, `color`, `format` (png/jpeg/pdf) and `save_dir`
- Configurable save directory, default settings and polling interval (integration options)
- `neat_scanner_scan_complete` event with the saved file paths
- No root, no `vhci_hcd` and no privileged container: the integration contains its own USB/IP
  client and talks to the server on TCP 3240 directly

## Install with HACS

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/skyu0221/ha-neat-scanner`, type **Integration**.
2. Download **Neat ADF scanner**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Neat ADF scanner*.

## Phone setup (USBAnywhere)

Plug the scanner into the Android phone, start the USBAnywhere Android server and share the
device. The integration finds it by USB ID automatically. Only one client can use the scanner
at a time, so don't keep it attached from another computer.

## Save directory

The directory must be allowed by Home Assistant. `/config/...` always is; for others add to
`configuration.yaml`:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /media/scans
```

## Use your own calibration

The Neat app calibrates the scanner (white paper, "cleaning" and "calibration") and then sends the
result with every scan. The integration replays one such scan, so to use **your** calibration you
capture the Neat app once and turn the capture into scan programs. You need a Windows PC for this.

1. **Install** [Wireshark](https://www.wireshark.org) with the **USBPcap** option, the Neat app
   and a USB/IP client for Windows that can attach the phone's USBAnywhere share.
2. **Prepare paper**: one blank white Letter sheet, plus a few pages and a receipt that are fine to
   keep in a capture (the image data is stored in it; USBPcap cuts it at 64 KB per transfer, so
   the pictures are incomplete, but do not share a capture of private documents).
3. **Start the capture** with the white sheet already in the feeder: start Wireshark on the USBPcap
   interface that the scanner appears on (the root hub of the virtual USB/IP device; start it
   before attaching, so the connection is included), then attach the scanner.
4. In the Neat app run **Scanner cleaning**, then **Scanner calibration** with the white sheet (if it
   reports an error, retry; the sheet must be pulled in).
5. Right after the calibration, scan **in this order** with the Neat app: a **single-sided color**
   page, a **double-sided color** page, then a **receipt in grayscale**. Note the order.
6. Stop the capture and save it as `.pcapng` (File > Save as, not pcap).
7. Extract the programs (Python 3, no packages needed):

   ```bash
   python3 tools/neat_capture.py list capture.pcapng
   ```

   It lists every scan in the capture with its size. The cleaning and the calibration also show up
   as scans; the three you want are the last ones, in the order you scanned them (the gray one
   reads smaller blocks, 158760 bytes, instead of 261954). Then, with `N` the scan number:

   ```bash
   python3 tools/neat_capture.py extract capture.pcapng --scan N --out custom_components/neat_scanner/scripts/c300.json   # single-sided color
   python3 tools/neat_capture.py extract capture.pcapng --scan N --out custom_components/neat_scanner/scripts/d300.json   # double-sided color
   python3 tools/neat_capture.py extract capture.pcapng --scan N --out custom_components/neat_scanner/scripts/g300.json   # grayscale
   ```

8. Copy the `scripts` folder into your Home Assistant `custom_components/neat_scanner/scripts`
   (HACS updates overwrite it, so redo this after an update) and restart Home Assistant. Scan a
   white sheet to check that the brightness is even.

Capture again whenever you run the Neat app calibration again and want the new values (a scanner that
got dirty or old). The 600 dpi program (`c600.json`) can be replaced the same way from a 600 dpi
scan made with a Windows scan application that lets you pick the resolution. See
[docs/PROTOCOL.md](docs/PROTOCOL.md) for what the programs contain.

## Scanning notes

- Native programs: 600 dpi color, 300 dpi color and 300 dpi grayscale. **Any resolution from 75 to
  600 dpi** can be set (the Resolution number entity, the options or the `dpi` service field): up to
  300 dpi the 300 dpi scan is scaled down in software, grayscale and black and white use the faster
  300 dpi gray scan (a third of the data, and the paper keeps moving without pauses), above 300 the
  600 dpi scan.
- The **Estimated scan size** sensor shows the expected file size of a Letter page (attributes: pixels
  and the size of a 3.1 x 10 in receipt) for the selected resolution, color mode and file format. It
  is based on real receipt scans, so busy pages compress less.
- **Scanner calibration data**: the scan programs for 300 dpi color, 300 dpi gray and double-sided
  300 dpi contain the shading tables and exposure values the Neat app uploaded **after a calibration
  of the author's scanner**. They give even brightness across the whole width. The tables are
  specific to one unit; a different scanner may show brighter or darker regions. 150 dpi and 600 dpi
  still use older tables (150 dpi no longer has its own program). See docs/PROTOCOL.md for what a calibration does.
- **Calibrate** (button and service) is an extra software white balance on top of that: scan a blank
  white letter sheet and the per column white level is stored. It matters most for the 600 dpi
  program.
- A scan call feeds **every sheet in the feeder** and produces one image per sheet. The 300 dpi
  programs keep feeding on their own, so the integration splits the stream into sheets at the short
  gap between them (the `sheets` option only caps how many pages are kept). Load the whole stack at
  once. A piece shorter than 1 inch is never a sheet and is joined to its neighbour. Turn the option *Split a
  stack into one page per sheet* off to keep a stack as one page. The 600 dpi program stops after each sheet and skip the eject step in between.
- A 300 dpi receipt takes about 45 seconds, 600 dpi about 90 seconds
  (mostly the USB/IP link and the scanner's own warm-up).
- Letter size works: the sensor always scans the full 8.8 in width and the page is cropped
  automatically to the paper and straightened (skew up to 5 degrees).
- The sensor monitor keeps one USB/IP connection open (needed to catch the 0.5 s button pulse).
  It pauses during scans; turn it off in the options if you need to share the scanner with
  another computer.
- If a scan is interrupted the scanner can stay stuck; unplug and replug it on the phone.

## Roadmap

- [x] Neat scan protocol (replayed register programs, see docs/PROTOCOL.md)
- [x] Native 150 dpi color and 300 dpi gray, multi-page ADF feeding, auto crop, deskew
- [ ] Hardware calibration (shading tables and AFE gain/offset, needs a capture of the Neat app)
- [ ] Direct USB connection (no USB/IP) — TODO
