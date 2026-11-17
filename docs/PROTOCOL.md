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

## Image data

- Raw stream, 8 bit, one **color plane per line**: `R`, `G`, `B` lines alternate.
- Line length at 300 dpi = **2646 bytes** (full sensor width, 8.82 in); 600 dpi doubles it.
- A scan of N image rows is `3*N` raw lines. The image is rotated by 180 degrees.
- The Windows driver saves a cropped image (auto crop); the raw sensor image is full width.
- Black and white mode uses different chunking (`65508`+`102`); layout not yet decoded.

## Open questions

- How the host derives the final chunk length (candidate: data-count registers around `0x42..0x45`).
- Exact register differences between modes; only the three captured modes are known.
- ADF feed/paper-present detection and multi-page behaviour (single page per capture so far).
- Black and white raw layout.
