# Neat ADF scanner (USB 1f44:0050) protocol notes

Derived from a USBPcap capture of the Windows driver (connect, then scans at
300 dpi color, 600 dpi color, 300 dpi black and white). Work in progress.

## Chip family

Vendor control register protocol of the Genesys Logic GL84x family
(compare SANE `genesys` backend, `gl846`/`gl847`). Endpoints: bulk IN `0x81`,
bulk OUT `0x02`, interrupt IN `0x83` (unused in the capture).

| Operation | Request |
| --- | --- |
| Write registers (pairs `reg,val`) | OUT `0x40`, bRequest `0x04`, wValue `0x83`, wIndex 0, data = pairs |
| Read registers | IN `0xC0`, bRequest `0x04`, wValue `0x8E`, wIndex `0x22 \| reg<<8`, wLength = n+1 (last byte `0x55`) |
| Set GPIO | OUT `0x40`, bRequest `0x0C`, wValue `0x8C`, wIndex = gpio reg (`0x10`, `0x13`), 1 byte |
| Get GPIO/status | IN `0xC0`, bRequest `0x0C`, wValue `0x8E`, wIndex = reg, 1 byte |
| Bulk setup (before each bulk transfer) | OUT `0x40`, bRequest `0x04`, wValue `0x82`, 8 bytes: `[00 00 00 10 | len32le]` for reads (`0x10`), RAM writes use other prefixes |
| SET_CONFIGURATION 1 | standard, right after descriptors |

## Scan flow (from capture)

1. Standard descriptors, SET_CONFIGURATION.
2. Register dump/initialisation, serial number read (`CNLF113S2251` at reg block `0x70e2..`).
3. Motor/gamma/shading tables written with bulk OUT (RAM writes, fixed content
   across scans: 11956 B x3, 512 B x3, 258 B x2).
4. Register program for the scan mode (dpi, color), then start.
5. Loop: bulk setup (`len = 0xF810` = 63504) -> bulk IN `63488` + bulk IN `16`
   (one logical chunk) -> poll registers `0x01..0x40`, `0x40..`, `0x7f..`, `0xbe..`,
   `0xfd..`, `0x41`. The final chunk is shorter (e.g. `0xD90E`).
6. Stop sequence: register writes, GPIO writes, motor park.

## Image data (verified on hardware)

- Raw 8 bit stream, **2646 bytes per line at 300 dpi** (5292 at 600 dpi) = 882 (1764) pixels x 3 bytes.
- Only **one byte of every three** carries the sample (the other two are about 0); pick the
  phase with the highest mean.
- **Three consecutive lines are the R, G and B lines** of one image row (`rows = lines / 3`).
- The image is rotated by 180 degrees.
- The data counter in registers `0x42..0x45` (big endian, unit 12 bytes) is the number of bytes
  waiting in scanner RAM; register `0x40 & 0xC0 == 0xC0` means the scan finished.
- The black and white capture is truncated by USBPcap (65535 byte snaplen) so its layout was not
  used; black and white is derived from the color scan.

## Open questions

- Exact register differences for other native resolutions (150/200 dpi) and gray.
- ADF behavior with several sheets or an empty feeder.
- Whether the Windows driver crops or rescales (its BMP is 928 px wide, the sensor delivers 882).
