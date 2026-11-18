#!/usr/bin/env python3
"""Turn a Wireshark (USBPcap) capture of the Neat app into scan programs for the integration.

Usage:
    neat_capture.py list    capture.pcapng
    neat_capture.py extract capture.pcapng --scan 1 --out c300.json

The start of a prelude is found as the end of the Neat app's idle loop, so it may include a few
seconds of idle polling that the shipped scripts do not have (harmless, a little slower).

``list`` finds the scans in the capture (a scan is a run of large "read scanner RAM" transfers) and
prints, for each, its size, duration and the number of operations before and after the data.
``extract`` writes one scan as the JSON program the integration replays (keys ``chunk``,
``prelude``, ``tail`` and ``tail_between``) and trims the Neat app's idle loop from the tail.

Only standard Python is needed. USBPcap cuts bulk IN data at 64 KB, so the image data of a capture
is not usable; the register programs and the shading tables (bulk OUT) are complete.
"""
import argparse
import json
import struct
import sys

BIG_CHUNK = 20000  # a scan data read is at least this many bytes
SCAN_GAP_OPS = 100  # more operations than this without a data read = another scan
MAX_DELAY_MS = 3000


def packets(path):
    """Yield (number, USBPcap bytes, timestamp in microseconds) for every packet of a pcapng file."""
    n = 0
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return
            block_type, length = struct.unpack("<II", head)
            body = f.read(length - 12)
            f.read(4)
            if block_type == 6:  # enhanced packet block
                hi, lo, cap = struct.unpack_from("<III", body, 4)
                n += 1
                yield n, body[20 : 20 + cap], (hi << 32) | lo


def parse(d):
    """Decode a USBPcap packet header; returns a dict (info bit 0 set = completion)."""
    hl, _irp, status, _func, info, _bus, _dev, ep, tr, dl = struct.unpack_from("<HQIHBHHBBI", d, 0)
    return {
        "completion": bool(info & 1), "ep": ep, "transfer": tr, "status": status,
        "data": d[hl : hl + dl] if hl + dl <= len(d) else d[hl:],
    }


def build_ops(path):
    """Capture -> list of operations in the format of the scripts (see scanner._play).

    ['c', setup (8 bytes hex), data hex, delay ms]  control transfer
    ['bo', data hex]                                  bulk OUT (shading tables)
    ['bi', length]                                    bulk IN (data is not kept)
    """
    ops, last = [], None
    for _n, raw, ts in packets(path):
        p = parse(raw)
        data = p["data"]
        if p["transfer"] == 2 and not p["completion"]:
            delay = 0 if last is None else min(ts - last, MAX_DELAY_MS * 1000) // 1000
            ops.append(["c", data[:8].hex(), data[8:].hex(), delay])
            last = ts
        elif p["transfer"] == 3 and not p["completion"] and not p["ep"] & 0x80:
            ops.append(["bo", data.hex()])
            last = ts
        elif p["transfer"] == 3 and p["completion"] and p["ep"] & 0x80 and (len(data) >= 16 or len(data) == 4):
            ops.append(["bi", len(data)])
            last = ts
    return ops


def chunk_length(op):
    """Length requested by a 'read scanner RAM' setup (control 0x04/0x82 with data 00 00 00 10 LL LL LL LL)."""
    if op[0] == "c" and op[1][:6] == "400482" and op[2][:8] == "00000010":
        return int.from_bytes(bytes.fromhex(op[2][8:16]), "little")
    return 0


def signature(op):
    return (op[0], op[1], op[2] if op[0] == "c" else None)


def find_idle(ops, lo, hi, longest=False, min_repeats=5, min_length=20):
    """Find the Neat app's idle loop in ops[lo:hi]: a group of at least ``min_length`` operations
    that repeats ``min_repeats`` times or more in a row (the app polls the scanner like that while it
    waits). Returns (start, length, end) with ``end`` after the last complete repetition; the first
    such run, or the longest one when ``longest`` is true. None if there is none."""
    sig = [signature(o) for o in ops[lo:hi]]
    n = len(sig)
    runs = []
    for length in range(min_length, 121):
        i = 0
        while i + length * min_repeats <= n:
            # the first operation of a cycle may differ (it carries another delay): skip it
            if all(sig[i + 1 + k] == sig[i + 1 + k + length] for k in range(length - 1)):
                j = i
                while j + 2 * length <= n and all(
                    sig[j + 1 + k] == sig[j + 1 + k + length] for k in range(length - 1)
                ):
                    j += length
                if (j - i) // length + 1 >= min_repeats:
                    runs.append((lo + i, length, lo + j + length))
                    i = j + length
                    continue
            i += 1
    if not runs:
        return None
    return max(runs, key=lambda r: (r[2] - r[0], -r[1])) if longest else min(runs, key=lambda r: (r[0], r[1]))


def find_scans(ops):
    """Return [{'first': i, 'last': i, 'chunk': bytes, 'bytes': total}] for each scan."""
    scans, cur = [], None
    for i, op in enumerate(ops):
        length = chunk_length(op)
        if length >= BIG_CHUNK and (cur is None or i - cur["last_big"] > SCAN_GAP_OPS):
            cur = {"first": i, "last_big": i, "last": i, "chunk": length, "bytes": 0}
            scans.append(cur)
        if cur is not None and length >= BIG_CHUNK:
            cur["last_big"] = i
        if cur is not None and length >= 1000 and i - cur["last_big"] <= SCAN_GAP_OPS:
            cur["last"] = i
            cur["bytes"] += length
    return scans


def split_scan(ops, scans, k):
    """Prelude, tail and the index bounds of scan number ``k`` (0 based)."""
    s = scans[k]
    start = 0
    if k > 0:
        prev_end = scans[k - 1]["last"]
        idle = find_idle(ops, prev_end, s["first"], longest=True)
        start = idle[2] if idle else prev_end + 1
    else:
        idle = find_idle(ops, 0, s["first"], longest=True)
        start = idle[2] if idle else 0
    j = s["last"] + 1
    while j < len(ops) and ops[j][0] == "bi":
        j += 1
    end = scans[k + 1]["first"] if k + 1 < len(scans) else len(ops)
    tail_idle = find_idle(ops, j, end, longest=True)
    tail_end = tail_idle[0] + tail_idle[1] if tail_idle else end  # keep one idle cycle
    return ops[start : s["first"]], ops[j:tail_end], start, j


def cmd_list(args):
    ops = build_ops(args.capture)
    scans = find_scans(ops)
    print(f"{len(ops)} operations, {len(scans)} scan(s)")
    for k, s in enumerate(scans):
        pre, tail, _a, _b = split_scan(ops, scans, k)
        print(
            f"scan {k + 1}: chunk {s['chunk']} bytes, data {s['bytes'] / 1e6:.1f} MB "
            f"(~{s['bytes'] / 7938:.0f} rows at 300 dpi color), prelude {len(pre)} ops, tail {len(tail)} ops"
        )


def cmd_extract(args):
    ops = build_ops(args.capture)
    scans = find_scans(ops)
    if not 1 <= args.scan <= len(scans):
        sys.exit(f"--scan must be 1..{len(scans)}")
    pre, tail, _a, _b = split_scan(ops, scans, args.scan - 1)
    between = tail
    if args.tail_between_from:
        with open(args.tail_between_from) as fh:
            between = json.load(fh)["tail_between"]
    program = {"chunk": scans[args.scan - 1]["chunk"], "prelude": pre, "tail": tail, "tail_between": between}
    with open(args.out, "w") as fh:
        json.dump(program, fh, separators=(",", ":"))
    print(f"wrote {args.out}: chunk {program['chunk']}, prelude {len(pre)} ops, tail {len(tail)} ops")
    if not args.tail_between_from:
        print("tail_between is a copy of tail (fine for c300, d300 and g300 stacks). Use --tail-between-from")
        print("with a shipped script if a stack stops after the first sheet.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="show the scans found in a capture")
    p.add_argument("capture")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("extract", help="write one scan as a JSON program")
    p.add_argument("capture")
    p.add_argument("--scan", type=int, default=1, help="scan number from 'list' (default 1)")
    p.add_argument("--out", required=True)
    p.add_argument("--tail-between-from", metavar="FILE", help="take tail_between from this script instead")
    p.set_defaults(func=cmd_extract)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
