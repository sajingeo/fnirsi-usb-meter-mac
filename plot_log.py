#!/usr/bin/env python3
"""Plot a CSV produced by usbmeter.py. Requires matplotlib (pip install matplotlib).

Usage: python3 plot_log.py usbmeter_20261105_120000.csv [-o plot.png]
"""
import argparse
import csv
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("-o", "--output", help="save PNG instead of showing a window")
    args = ap.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib missing: pip install matplotlib")

    t, v, i, p, temp = [], [], [], [], []
    with open(args.csv_file) as f:
        for row in csv.DictReader(f):
            t.append(float(row["elapsed_s"]))
            v.append(float(row["voltage_V"]))
            i.append(float(row["current_A"]))
            p.append(float(row["power_W"]))
            temp.append(float(row["temp_C"]))

    if not t:
        sys.exit("No rows in CSV")

    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(11, 8))
    axes[0].plot(t, v)
    axes[0].set_ylabel("Voltage (V)")
    axes[1].plot(t, i)
    axes[1].set_ylabel("Current (A)")
    axes[2].plot(t, p)
    axes[2].set_ylabel("Power (W)")
    axes[2].set_xlabel("Time (s)")
    for ax in axes:
        ax.grid(True, alpha=0.3)
    fig.suptitle(args.csv_file)
    fig.tight_layout()

    if args.output:
        fig.savefig(args.output, dpi=150)
        print(f"Saved {args.output}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
