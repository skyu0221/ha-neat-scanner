# Neat ADF scanner for Home Assistant

Home Assistant integration for the **Neat ADF scanner** (USB `1f44:0050`) shared over the
network with [USBAnywhere](https://github.com/oscarlosu-lab/USBAnywhere) (Android app) or any
standard `usbipd` server.

> **Status: working.** Scanning has been verified on real hardware (Neat ADF scanner shared by
> USBAnywhere on Android): 300 and 600 dpi color scans, plus derived modes. The Neat protocol is
> proprietary and not in SANE; it was reverse engineered from a USB capture of the Windows driver
> (see [docs/PROTOCOL.md](docs/PROTOCOL.md)).

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

## Scanning notes

- Native programs: 600 dpi color, 300 dpi color, 150 dpi color and 300 dpi grayscale. Other DPI
  values are resampled from the closest one (up to 150 color uses the 150 dpi scan, grayscale and
  black and white use the faster 300 dpi gray scan, above 300 the 600 dpi scan).
- **Scanner calibration data**: the scan programs for 300 dpi color, 300 dpi gray and double-sided
  300 dpi contain the shading tables and exposure values the Neat app uploaded **after a calibration
  of the author's scanner**. They give even brightness across the whole width. The tables are
  specific to one unit; a different scanner may show brighter or darker regions. 150 dpi and 600 dpi
  still use older tables. See docs/PROTOCOL.md for what a calibration does.
- **Calibrate** (button and service) is an extra software white balance on top of that: scan a blank
  white letter sheet and the per column white level is stored. It matters most for the 150 and
  600 dpi programs.
- A scan call feeds **every sheet in the feeder** and produces one image per sheet. The 300 dpi
  programs keep feeding on their own, so the integration splits the stream into sheets at the short
  gap between them (the `sheets` option only caps how many pages are kept). Load the whole stack at
  once. The 150 and 600 dpi programs stop after each sheet and skip the eject step in between.
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
