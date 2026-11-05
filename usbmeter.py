#!/usr/bin/env python3
"""
usbmeter.py — macOS logger for FNIRSI USB power meters (FNB48, FNB48S, FNB58, C1, FNB-C2).

Talks the same USB-HID protocol as the Windows "UsbMeter" app, via hidapi
(no kernel driver needed). Streams 100 samples/sec of voltage, current,
D+/D-, and temperature; integrates energy and capacity; logs to CSV with a
live terminal readout.

Protocol per the MIT-licensed reverse engineering in
https://github.com/baryluk/fnirsi-usb-power-data-logger

Usage:
    python3 usbmeter.py                    # log to usbmeter_<timestamp>.csv
    python3 usbmeter.py -o run1.csv        # explicit output file
    python3 usbmeter.py --avg 10           # one CSV row per 10 samples (10 rows/s)
    python3 usbmeter.py --duration 60      # stop after 60 seconds
Ctrl-C stops logging and prints a summary.
"""

import argparse
import csv
import datetime
import sys
import time

try:
    import hid
except ImportError:
    sys.exit(
        "The 'hid' package is missing.\n"
        "Install with:  brew install hidapi && pip install hid\n"
        "(or run via the project venv: .venv/bin/python usbmeter.py)"
    )

# Known FNIRSI meters (VID, PID, name, fnb58-style keep-alive)
KNOWN_DEVICES = [
    (0x0483, 0x003A, "FNB48", False),
    (0x0483, 0x003B, "C1", False),
    (0x2E3C, 0x5558, "FNB58", True),
    (0x2E3C, 0x0049, "FNB48S", True),
]

SPS = 100  # device streams 100 samples/sec
SAMPLE_DT = 1.0 / SPS
TEMP_EMA_ALPHA = 0.9

CMD_INIT_1 = b"\xaa\x81" + b"\x00" * 61 + b"\x8e"
CMD_INIT_2 = b"\xaa\x82" + b"\x00" * 61 + b"\x96"
CMD_START = b"\xaa\x83" + b"\x00" * 61 + b"\x9e"


def find_device():
    """Return (device_info_dict, name, is_fnb58_family) or (None, None, None)."""
    infos = hid.enumerate()
    for vid, pid, name, fnb58 in KNOWN_DEVICES:
        for info in infos:
            if info["vendor_id"] == vid and info["product_id"] == pid:
                return info, name, fnb58
    # Fallback: match by manufacturer/product string (e.g. future FNB-C2 IDs)
    for info in infos:
        text = f"{info.get('manufacturer_string') or ''} {info.get('product_string') or ''}".upper()
        if "FNIRSI" in text or "FNB" in text:
            # Newer meters (2E3C vendor family) use the 1s keep-alive
            return info, info.get("product_string") or "FNIRSI", info["vendor_id"] == 0x2E3C
    return None, None, None


def open_device(info):
    """Open via path (robust when multiple HID interfaces exist). Supports both
    the 'hid' (ctypes) and 'hidapi' (cython) packages."""
    if hasattr(hid, "Device"):  # pyhidapi / 'hid' package
        return hid.Device(path=info["path"])
    dev = hid.device()  # cython-hidapi / 'hidapi' package
    dev.open_path(info["path"])
    return dev


def hid_write(dev, payload):
    """64-byte output report. hidapi wants the report ID (0x00, unnumbered)
    prepended; some backends accept the bare payload — try both."""
    try:
        n = dev.write(b"\x00" + payload)
        if n > 0:
            return
    except Exception:
        pass
    dev.write(payload)


def hid_read(dev, timeout_ms=1000):
    data = dev.read(64, timeout_ms) if hasattr(hid, "Device") else dev.read(64, timeout_ms)
    return bytes(data) if data else b""


def start_stream(dev, fnb58_family):
    hid_write(dev, CMD_INIT_1)
    hid_write(dev, CMD_INIT_2)
    hid_write(dev, CMD_INIT_2 if fnb58_family else CMD_START)


def decode_samples(data):
    """Yield (voltage_V, current_A, dp_V, dn_V, temp_C) for the 4 samples in a
    type-0x04 packet; empty for any other packet."""
    if len(data) < 63 or data[0] != 0xAA or data[1] != 0x04:
        # Depending on backend, the leading 0xaa may be stripped with the report ID.
        if len(data) >= 62 and data[0] == 0x04:
            data = b"\xaa" + data
        else:
            return
    for i in range(4):
        o = 2 + 15 * i
        voltage = int.from_bytes(data[o : o + 4], "little") / 100000
        current = int.from_bytes(data[o + 4 : o + 8], "little") / 100000
        dp = int.from_bytes(data[o + 8 : o + 10], "little") / 1000
        dn = int.from_bytes(data[o + 10 : o + 12], "little") / 1000
        temp_c = int.from_bytes(data[o + 13 : o + 15], "little") / 10.0
        yield voltage, current, dp, dn, temp_c


def main():
    ap = argparse.ArgumentParser(description="FNIRSI USB power meter logger for macOS")
    ap.add_argument("-o", "--output", help="CSV output path (default: usbmeter_<timestamp>.csv)")
    ap.add_argument("--avg", type=int, default=10, metavar="N",
                    help="average N samples per CSV row (default 10 → 10 rows/s; 1 = every sample)")
    ap.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    ap.add_argument("--quiet", action="store_true", help="no live readout, CSV only")
    args = ap.parse_args()

    info, name, fnb58_family = find_device()
    if info is None:
        seen = {(i["vendor_id"], i["product_id"], i.get("product_string")) for i in hid.enumerate()}
        print("No FNIRSI meter found. Plug the meter's PC/data port into the Mac "
              "(use the meter's dedicated PC port, not the pass-through port).", file=sys.stderr)
        print("HID devices currently visible:", file=sys.stderr)
        for vid, pid, prod in sorted(seen):
            print(f"  {vid:04x}:{pid:04x}  {prod or ''}", file=sys.stderr)
        sys.exit(1)

    out_path = args.output or datetime.datetime.now().strftime("usbmeter_%Y%m%d_%H%M%S.csv")
    print(f"Found {name} ({info['vendor_id']:04x}:{info['product_id']:04x}) — logging to {out_path}")

    dev = open_device(info)
    start_stream(dev, fnb58_family)

    # If no data in 2s, retry init once (covers report-ID quirks across backends)
    first = hid_read(dev, 2000)
    if not first:
        start_stream(dev, fnb58_family)
        first = hid_read(dev, 2000)
        if not first:
            sys.exit("Meter found but no data received. Unplug/replug the meter and retry.")

    keepalive_interval = 1.0 if fnb58_family else 0.003
    next_keepalive = time.time() + keepalive_interval

    energy_ws = 0.0      # watt-seconds
    capacity_as = 0.0    # amp-seconds
    temp_ema = None
    n_samples = 0
    t_start = time.time()
    last_display = 0.0

    # averaging accumulators
    acc = [0.0, 0.0, 0.0, 0.0, 0.0]  # V, I, dp, dn, temp
    acc_n = 0

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "elapsed_s", "voltage_V", "current_A", "power_W",
                    "dp_V", "dn_V", "temp_C", "energy_Wh", "capacity_mAh"])

        def flush_row():
            nonlocal acc, acc_n
            if acc_n == 0:
                return
            v, i, dp, dn, tc = (x / acc_n for x in acc)
            w.writerow([datetime.datetime.now().isoformat(timespec="milliseconds"),
                        f"{time.time() - t_start:.3f}",
                        f"{v:.5f}", f"{i:.5f}", f"{v * i:.5f}",
                        f"{dp:.3f}", f"{dn:.3f}", f"{tc:.1f}",
                        f"{energy_ws / 3600:.6f}", f"{capacity_as / 3.6:.4f}"])
            acc = [0.0, 0.0, 0.0, 0.0, 0.0]
            acc_n = 0

        data = first
        try:
            while True:
                for v, i, dp, dn, tc in decode_samples(data):
                    temp_ema = tc if temp_ema is None else tc * (1 - TEMP_EMA_ALPHA) + temp_ema * TEMP_EMA_ALPHA
                    energy_ws += v * i * SAMPLE_DT
                    capacity_as += i * SAMPLE_DT
                    n_samples += 1
                    acc = [a + b for a, b in zip(acc, (v, i, dp, dn, temp_ema))]
                    acc_n += 1
                    if acc_n >= args.avg:
                        flush_row()

                now = time.time()
                if now >= next_keepalive:
                    hid_write(dev, CMD_START)
                    next_keepalive = now + keepalive_interval

                if not args.quiet and now - last_display >= 0.25 and n_samples:
                    sys.stdout.write(
                        f"\r{v:8.4f} V  {i:8.4f} A  {v * i:8.4f} W  "
                        f"D+ {dp:5.3f} V  D- {dn:5.3f} V  {temp_ema:5.1f} °C  "
                        f"{energy_ws / 3600:9.5f} Wh  {capacity_as / 3.6:9.3f} mAh  "
                        f"[{now - t_start:7.1f} s]   ")
                    sys.stdout.flush()
                    last_display = now

                if args.duration and now - t_start >= args.duration:
                    break

                data = hid_read(dev, 1000)
        except KeyboardInterrupt:
            pass
        finally:
            flush_row()
            f.flush()

    dur = time.time() - t_start
    print(f"\n\nDone: {n_samples} samples over {dur:.1f} s → {out_path}")
    print(f"Totals: {energy_ws / 3600:.5f} Wh, {capacity_as / 3.6:.3f} mAh")
    try:
        dev.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
