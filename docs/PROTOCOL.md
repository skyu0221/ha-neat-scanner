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

## Image data (verified against Windows output)

- The CIS has **three segments of 882 px** (full width 2646 px = 8.82 in at 300 dpi; 5292 at 600 dpi)
  that are read out in parallel and **interleaved byte by byte**: `line[3*i + t]` is pixel `i` of
  tap `t`. Tap to position: tap 2 = left, tap 0 = middle, tap 1 = right.
- Each segment has about 20 dummy (zero) pixels at the start and 3 falling-edge pixels at the
  end; about 860 pixels are valid (`SectorDummy=860,...`).
- **Three consecutive lines are the R, G and B lines** of one image row (`rows = lines / 3`).
- The image is rotated by 180 degrees.
- The sensor always scans the full width. A receipt only covers the middle segment, a letter
  page all three. Cropping to the paper is software (the driver has auto crop, deskew, rotate).
- Raw levels are not flat: the three segments have different gain, so a per column white level
  correction is needed (the driver uses calibration data; we estimate it from the page).
- The data counter in registers `0x42..0x45` (big endian, unit 12 bytes) is the number of bytes
  waiting in scanner RAM; register `0x40 & 0xC0 == 0xC0` means the scan finished.
- Letter and receipt scans use identical register programs and upload tables (verified).
- The earlier black and white capture is truncated by USBPcap (65535 byte snaplen).

## Sensors (verified live with a monitor)

- Feeder paper: `r40 & 0x80 == 0 and r41 & 0x08 == 0` means **paper present** (both bits are 1
  when empty: `r40=0xf0 r41=0xdc` empty, `0x70 / 0xd4` with paper). One sheet and three sheets
  read the same.
- Cover: `r40 & 0x20` set = closed (clears briefly while it is opened and closed).
- Button: `GPIO 0x21` (request `0x0C`, value `0x8E`, index `0x21`) bit `0x08`, a pulse of about
  0.5 s, so it has to be polled faster than every 0.3 s.
- With several sheets loaded the scanner pulls the next sheet in half way and then stalls
  without setting the finished bits (`r40 & 0xC0 == 0xC0`). The data counter stops changing, so
  four seconds without change after data arrived means the sheet is complete. After the stop
  sequence the next full scan program feeds the waiting sheet. The paper sensor still reads
  "paper" after sheet 1 when sheet 2 waits and "empty" after the last one.

## Stacks of sheets (round 3 capture, verified on hardware)

- After each sheet the driver checks the paper sensor. With another sheet waiting it runs the stop
  sequence **without the eject step** (the `0258` motor loop with 30-46 writes, preceded by `20ff`,
  `3500 3600 3700`, `6b8c` and a bulk OUT of the RAM table); only after the last sheet does it eject.
- Ejecting early pulls the waiting sheet in about 1.8 in too far, so its leading part is not scanned.
- Result: a stack of three receipts scanned completely (3661, 3137 and 2929 rows against 3665,
  3136 and 2932 for the Windows driver).

## Inputs and buttons (round 2 capture)

- No interrupt IN (`0x83`) traffic was ever seen; the scanner is polled with control transfers.
- `GPIO 0x21` (`c0 0c 8e 00 21 00 01 00`, 1 byte): `0x40` seen when paper was loaded in the feeder
  before a scan, `0x04` pulses right after each scan ends. The Neat app polls it about once a second.
- Registers `0x0a`, `0x40` and `0x6c` bits that change are host driven (written by the host).

## Driver configuration (from the Neat ADF scanner driver, v3.0.4.0 / MSI 2014)

The driver embeds an INI with one section per mode (`A_C_300` = ADF, color, 300 dpi; `G` = gray;
`B` = back side; `T` = text variants; `A2_` = hardware revision 2). Sections `Include=` others;
`Regx=reg,val,...` are register values, `RegMaskx=reg,mask,val,...` are masked updates
(`new = old & ~mask | val & mask`). Resolving `A_C_300` / `A_C_600` reproduces about 70 % of the
register state recorded from the Windows driver; the rest is computed by the driver:

| Registers | Meaning (GL846 layout) | Observed |
| --- | --- | --- |
| `0x10..0x15` | exposure R/G/B | per dpi |
| `0x30,0x31` / `0x32,0x33` | start / end pixel | 159 / 1041 at 300 dpi, 159 / 1923 at 600 dpi (882 / 1764 px window) |
| `0x38,0x39` | line period | in INI per dpi |
| `0x3d..0x3f` | line count | `0x5dc0` |
| `0x60..0x65`, `0x90..0x97` | motor/step parameters | computed |

The 882 px window is what the Windows scan app asked for; the driver default window is
`0,0,40800,56160` (1/4800 in = 8.5 x 11.7 in) and `MaxPixel=5184` at 600 dpi, so the scanner can
scan letter width. The driver does auto crop, auto deskew and auto rotate in software
(`WIA_NEAT_AUTOCROP/DESKEW/ROTATE`).

Native modes in the INI: 600/300/200/150/75 dpi, color and gray, front and back.

## Open questions

- Exact formulas for the computed registers and the width-dependent upload tables, so that any
  window width (letter) and native 150/200 dpi and gray can be programmed.
- ADF behavior with several sheets or an empty feeder.
- The Windows driver crops, deskews and rotates in software; our decoder does not yet.
