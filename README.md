# Neat ADF scanner for Home Assistant

Home Assistant integration for the **Neat ADF scanner** (USB `1f44:0050`) shared over the
network with [USBAnywhere](https://github.com/oscarlosu-lab/USBAnywhere) (Android app) or any
standard `usbipd` server.

> **Status: working.** Scanning has been verified on real hardware (Neat ADF scanner shared by
> USBAnywhere on Android): 300 and 600 dpi color scans, plus derived modes. The Neat protocol is
> proprietary and not in SANE; it was reverse engineered from a USB capture of the Windows driver
> (see [docs/PROTOCOL.md](docs/PROTOCOL.md)).

## Features

- Online/offline binary sensor (polls the USB/IP server and checks the scanner is exported)
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

- The scanner natively runs 300 and 600 dpi color scans. Other DPI values are resampled from
  those (up to 300 uses the 300 dpi scan, above 300 the 600 dpi scan). Grayscale and black and
  white are converted in software.
- One scan call feeds one sheet; a 300 dpi receipt takes about 45 seconds, 600 dpi about 90 seconds
  (mostly the USB/IP link and the scanner's own warm-up).
- The image is the full sensor width (882 px at 300 dpi); there is no auto crop or deskew yet.
- If a scan is interrupted the scanner can stay stuck; unplug and replug it on the phone.

## Roadmap

- [x] Neat scan protocol (replayed register programs, see docs/PROTOCOL.md)
- [ ] Native 150/200 dpi and grayscale programs, multi-page ADF feeding, auto crop
- [ ] Direct USB connection (no USB/IP) — TODO
