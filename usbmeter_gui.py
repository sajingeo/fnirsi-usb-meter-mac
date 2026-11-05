#!/usr/bin/env python3
"""
usbmeter_gui.py — live GUI for FNIRSI USB power meters (FNB48/48S/58, C1, FNB-C2)
on macOS. Mac equivalent of the Windows UsbMeter.exe tool.

Features: auto-connect/reconnect, big live readouts (V/A/W, D+/D-, temp,
energy, capacity), scrolling waveform charts with selectable time window and
V/A/W scale ranges (plus free mouse zoom/pan), pause, and start/stop CSV
logging at a selectable rate.

Run:  .venv/bin/python usbmeter_gui.py        (add --demo for synthetic data)
"""

import argparse
import csv
import datetime
import sys
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar, QVBoxLayout,
    QWidget,
)

import usbmeter as proto

UPDATE_MS = 50  # GUI refresh interval

TIME_WINDOWS = [("10 s", 10), ("30 s", 30), ("1 min", 60), ("5 min", 300),
                ("15 min", 900), ("1 h", 3600), ("All", None)]
V_SCALES = [("Auto", None), ("0–6 V", 6), ("0–12 V", 12), ("0–24 V", 24), ("0–36 V", 36)]
A_SCALES = [("Auto", None), ("0–0.5 A", 0.5), ("0–1 A", 1), ("0–2 A", 2),
            ("0–3 A", 3), ("0–5 A", 5), ("0–6.5 A", 6.5)]
W_SCALES = [("Auto", None), ("0–10 W", 10), ("0–30 W", 30), ("0–65 W", 65), ("0–100 W", 100)]
LOG_RATES = [("10 rows/s", 10), ("100 rows/s (all)", 1), ("1 row/s", 100)]  # label, samples per row


class Reader(QThread):
    """Reads the meter in a background thread; emits (n,5) arrays [V,I,D+,D-,T]."""
    samples = Signal(object)
    status = Signal(str, bool)  # message, connected

    def __init__(self, demo=False):
        super().__init__()
        self._stop = False
        self.demo = demo

    def run(self):
        if self.demo:
            self._run_demo()
            return
        while not self._stop:
            info, name, fnb58 = proto.find_device()
            if info is None:
                self.status.emit("No meter found — plug the meter's PC port into the Mac (retrying…)", False)
                time.sleep(1.0)
                continue
            dev = None
            try:
                dev = proto.open_device(info)
                self.status.emit(f"Connected: {name}  ({info['vendor_id']:04x}:{info['product_id']:04x})", True)
                proto.start_stream(dev, fnb58)
                keepalive = 1.0 if fnb58 else 0.003
                next_ka = time.time() + keepalive
                misses = 0
                while not self._stop:
                    data = proto.hid_read(dev, 500)
                    if data:
                        misses = 0
                        batch = list(proto.decode_samples(data))
                        if batch:
                            self.samples.emit(np.asarray(batch))
                    else:
                        misses += 1
                        if misses == 2:
                            proto.start_stream(dev, fnb58)  # nudge a quiet device
                        elif misses > 8:
                            raise IOError("stream stalled")
                    now = time.time()
                    if now >= next_ka:
                        proto.hid_write(dev, proto.CMD_START)
                        next_ka = now + keepalive
            except Exception as e:
                if not self._stop:
                    self.status.emit(f"Disconnected ({e}) — reconnecting…", False)
            finally:
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
            if not self._stop:
                time.sleep(1.0)

    def _run_demo(self):
        self.status.emit("DEMO MODE — synthetic data (run without --demo for the real meter)", True)
        t = 0.0
        rng = np.random.default_rng()
        while not self._stop:
            n = 4
            ts = np.arange(n) * proto.SAMPLE_DT + t
            v = 5.1 + 0.05 * np.sin(2 * np.pi * 0.2 * ts)
            i = np.clip(1.2 + 0.6 * np.sin(2 * np.pi * 0.05 * ts) + 0.01 * rng.standard_normal(n), 0, None)
            batch = np.column_stack([v, i, np.full(n, 2.7), np.full(n, 0.6), np.full(n, 31.5)])
            self.samples.emit(batch)
            t += n * proto.SAMPLE_DT
            time.sleep(n * proto.SAMPLE_DT)

    def stop(self):
        self._stop = True


class Buf:
    """Growable sample store: columns [t, V, I, D+, D-, temp]."""

    def __init__(self, cap=120000):
        self.a = np.zeros((cap, 6))
        self.n = 0

    def extend(self, rows):
        need = self.n + len(rows)
        while need > len(self.a):
            self.a = np.vstack([self.a, np.zeros_like(self.a)])
        self.a[self.n:need] = rows
        self.n = need

    def view(self):
        return self.a[: self.n]

    def clear(self):
        self.n = 0


def value_tile(title, color, big=False):
    box = QVBoxLayout()
    box.setSpacing(0)
    t = QLabel(title)
    t.setStyleSheet(f"color: {color}; font-size: 12px;")
    v = QLabel("—")
    v.setStyleSheet(f"color: {color};")
    f = QFont("Menlo")
    f.setPointSize(34 if big else 18)
    f.setBold(big)
    v.setFont(f)
    box.addWidget(t)
    box.addWidget(v)
    w = QWidget()
    w.setLayout(box)
    return w, v


class MainWindow(QMainWindow):
    def __init__(self, demo=False):
        super().__init__()
        self.setWindowTitle("FNIRSI USB Meter (mac)")
        self.resize(1150, 750)

        self.buf = Buf()
        self.total_samples = 0
        self.energy_ws = 0.0
        self.capacity_as = 0.0
        self.temp_ema = None
        self.t0_wall = None
        self.paused = False
        self.pending = []

        # logging state
        self.log_file = None
        self.log_writer = None
        self.log_path = None
        self.log_acc = np.zeros(5)
        self.log_acc_n = 0
        self.log_rows = 0

        self._build_ui()

        self.reader = Reader(demo=demo)
        self.reader.samples.connect(self._on_samples)
        self.reader.status.connect(self._on_status)
        self.reader.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(UPDATE_MS)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # Readout tiles
        tiles = QHBoxLayout()
        self.lbl = {}
        for key, title, color, big in [
            ("v", "VOLTAGE (V)", "#f5d90a", True),
            ("i", "CURRENT (A)", "#2dd4bf", True),
            ("p", "POWER (W)", "#f472b6", True),
        ]:
            w, lab = value_tile(title, color, big)
            tiles.addWidget(w, stretch=2)
            self.lbl[key] = lab
        for key, title, color in [
            ("dp", "D+ (V)", "#a3a3a3"), ("dn", "D− (V)", "#a3a3a3"),
            ("temp", "TEMP (°C)", "#fb923c"), ("wh", "ENERGY (Wh)", "#93c5fd"),
            ("mah", "CAPACITY (mAh)", "#93c5fd"), ("time", "RUNTIME", "#d4d4d4"),
        ]:
            w, lab = value_tile(title, color)
            tiles.addWidget(w, stretch=1)
            self.lbl[key] = lab
        root.addLayout(tiles)

        # Controls
        ctrl = QHBoxLayout()

        def combo(items):
            c = QComboBox()
            for label, _ in items:
                c.addItem(label)
            return c

        self.cmb_window = combo(TIME_WINDOWS); self.cmb_window.setCurrentIndex(1)
        self.cmb_v = combo(V_SCALES)
        self.cmb_a = combo(A_SCALES)
        self.cmb_w = combo(W_SCALES)
        self.chk_dpdn = QCheckBox("Show D+/D−")
        self.btn_pause = QPushButton("Pause display")
        self.btn_pause.setCheckable(True)
        self.btn_reset = QPushButton("Reset Wh/mAh")
        self.btn_clear = QPushButton("Clear graphs")
        self.cmb_lograte = combo(LOG_RATES)
        self.btn_log = QPushButton("● Start logging")
        self.btn_log.setStyleSheet("color: #4ade80; font-weight: bold;")

        for label, widget in [("Window:", self.cmb_window), ("V scale:", self.cmb_v),
                              ("A scale:", self.cmb_a), ("W scale:", self.cmb_w)]:
            ctrl.addWidget(QLabel(label))
            ctrl.addWidget(widget)
        ctrl.addWidget(self.chk_dpdn)
        ctrl.addWidget(self.btn_pause)
        ctrl.addWidget(self.btn_reset)
        ctrl.addWidget(self.btn_clear)
        ctrl.addStretch(1)
        ctrl.addWidget(QLabel("Log rate:"))
        ctrl.addWidget(self.cmb_lograte)
        ctrl.addWidget(self.btn_log)
        root.addLayout(ctrl)

        # Plots
        pg.setConfigOptions(antialias=False)
        glw = pg.GraphicsLayoutWidget()
        glw.setBackground("#111111")
        self.plot_v = glw.addPlot(row=0, col=0)
        self.plot_i = glw.addPlot(row=1, col=0)
        self.plot_w = glw.addPlot(row=2, col=0)
        for p, label in [(self.plot_v, "V"), (self.plot_i, "A"), (self.plot_w, "W")]:
            p.showGrid(x=True, y=True, alpha=0.25)
            p.setLabel("left", label)
            p.setMouseEnabled(x=True, y=True)
        self.plot_w.setLabel("bottom", "time", units="s")
        self.plot_i.setXLink(self.plot_v)
        self.plot_w.setXLink(self.plot_v)

        def curve(plot, color, width=1.5):
            c = plot.plot(pen=pg.mkPen(color, width=width))
            c.setDownsampling(auto=True, method="peak")
            c.setClipToView(True)
            return c

        self.cur_v = curve(self.plot_v, "#f5d90a")
        self.cur_dp = curve(self.plot_v, "#60a5fa", 1.0)
        self.cur_dn = curve(self.plot_v, "#a78bfa", 1.0)
        self.cur_i = curve(self.plot_i, "#2dd4bf")
        self.cur_w = curve(self.plot_w, "#f472b6")
        root.addWidget(glw, stretch=1)

        self.setStatusBar(QStatusBar())
        self.status_conn = QLabel("Starting…")
        self.status_log = QLabel("Not logging")
        self.statusBar().addWidget(self.status_conn, 1)
        self.statusBar().addPermanentWidget(self.status_log)

        self.btn_log.clicked.connect(self._toggle_logging)
        self.btn_reset.clicked.connect(self._reset_totals)
        self.btn_clear.clicked.connect(self._clear_graphs)
        self.setCentralWidget(central)
        self.setStyleSheet("QMainWindow, QWidget { background: #1b1b1f; color: #e5e5e5; }"
                           "QComboBox, QPushButton { background: #2a2a30; padding: 3px 8px; }")

    # ---------- data ----------
    def _on_samples(self, batch):
        self.pending.append(batch)

    def _on_status(self, msg, connected):
        self.status_conn.setText(msg)
        self.status_conn.setStyleSheet("color: #4ade80;" if connected else "color: #f87171;")

    def _refresh(self):
        if self.pending:
            batch = np.vstack(self.pending)
            self.pending.clear()
            if self.t0_wall is None:
                self.t0_wall = time.time()
            n = len(batch)
            t = (self.total_samples + np.arange(n)) * proto.SAMPLE_DT
            self.total_samples += n
            self.buf.extend(np.column_stack([t, batch]))

            v, i = batch[:, 0], batch[:, 1]
            self.energy_ws += float(np.sum(v * i)) * proto.SAMPLE_DT
            self.capacity_as += float(np.sum(i)) * proto.SAMPLE_DT
            for tc in batch[:, 4]:
                self.temp_ema = tc if self.temp_ema is None else tc * 0.1 + self.temp_ema * 0.9

            if self.log_writer:
                self._log_batch(batch)

            last = batch[-1]
            self.lbl["v"].setText(f"{last[0]:8.4f}")
            self.lbl["i"].setText(f"{last[1]:8.4f}")
            self.lbl["p"].setText(f"{last[0] * last[1]:8.4f}")
            self.lbl["dp"].setText(f"{last[2]:5.3f}")
            self.lbl["dn"].setText(f"{last[3]:5.3f}")
            self.lbl["temp"].setText(f"{self.temp_ema:5.1f}")
            self.lbl["wh"].setText(f"{self.energy_ws / 3600:9.5f}")
            self.lbl["mah"].setText(f"{self.capacity_as / 3.6:9.3f}")
            secs = int(self.total_samples * proto.SAMPLE_DT)
            self.lbl["time"].setText(f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}")

        if not self.btn_pause.isChecked():
            self._redraw()

    def _redraw(self):
        data = self.buf.view()
        if not len(data):
            return
        window = TIME_WINDOWS[self.cmb_window.currentIndex()][1]
        tmax = data[-1, 0]
        if window is not None:
            start = np.searchsorted(data[:, 0], tmax - window)
            data = data[start:]
            self.plot_v.setXRange(max(0.0, tmax - window), tmax, padding=0)
        else:
            self.plot_v.setXRange(0, tmax, padding=0.02)

        t = data[:, 0]
        self.cur_v.setData(t, data[:, 1])
        show_dpdn = self.chk_dpdn.isChecked()
        self.cur_dp.setData(t, data[:, 3]) if show_dpdn else self.cur_dp.setData([], [])
        self.cur_dn.setData(t, data[:, 4]) if show_dpdn else self.cur_dn.setData([], [])
        self.cur_i.setData(t, data[:, 2])
        self.cur_w.setData(t, data[:, 1] * data[:, 2])

        for plot, scales, cmb in [(self.plot_v, V_SCALES, self.cmb_v),
                                  (self.plot_i, A_SCALES, self.cmb_a),
                                  (self.plot_w, W_SCALES, self.cmb_w)]:
            top = scales[cmb.currentIndex()][1]
            if top is None:
                plot.enableAutoRange(axis="y")
            else:
                plot.disableAutoRange(axis="y")
                plot.setYRange(0, top, padding=0)

    # ---------- logging ----------
    def _toggle_logging(self):
        if self.log_writer:
            self._stop_logging()
            return
        default = datetime.datetime.now().strftime("usbmeter_%Y%m%d_%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Save log as", default, "CSV files (*.csv)")
        if not path:
            return
        self.log_file = open(path, "w", newline="")
        self.log_writer = csv.writer(self.log_file)
        self.log_writer.writerow(["timestamp", "elapsed_s", "voltage_V", "current_A", "power_W",
                                  "dp_V", "dn_V", "temp_C", "energy_Wh", "capacity_mAh"])
        self.log_path = path
        self.log_acc = np.zeros(5)
        self.log_acc_n = 0
        self.log_rows = 0
        self.btn_log.setText("■ Stop logging")
        self.btn_log.setStyleSheet("color: #f87171; font-weight: bold;")

    def _log_batch(self, batch):
        per_row = LOG_RATES[self.cmb_lograte.currentIndex()][1]
        for row in batch:
            self.log_acc += row
            self.log_acc_n += 1
            if self.log_acc_n >= per_row:
                v, i, dp, dn, tc = self.log_acc / self.log_acc_n
                self.log_writer.writerow(
                    [datetime.datetime.now().isoformat(timespec="milliseconds"),
                     f"{self.total_samples * proto.SAMPLE_DT:.3f}",
                     f"{v:.5f}", f"{i:.5f}", f"{v * i:.5f}", f"{dp:.3f}", f"{dn:.3f}",
                     f"{tc:.1f}", f"{self.energy_ws / 3600:.6f}", f"{self.capacity_as / 3.6:.4f}"])
                self.log_acc[:] = 0
                self.log_acc_n = 0
                self.log_rows += 1
        self.status_log.setText(f"Logging → {self.log_path}  ({self.log_rows} rows)")
        self.status_log.setStyleSheet("color: #4ade80;")

    def _stop_logging(self):
        self.log_file.close()
        self.status_log.setText(f"Saved {self.log_rows} rows → {self.log_path}")
        self.status_log.setStyleSheet("color: #e5e5e5;")
        self.log_file = self.log_writer = None
        self.btn_log.setText("● Start logging")
        self.btn_log.setStyleSheet("color: #4ade80; font-weight: bold;")

    def _reset_totals(self):
        self.energy_ws = 0.0
        self.capacity_as = 0.0

    def _clear_graphs(self):
        self.buf.clear()
        self.total_samples = 0

    def closeEvent(self, event):
        self.reader.stop()
        self.reader.wait(2000)
        if self.log_writer:
            self._stop_logging()
        event.accept()


def main():
    ap = argparse.ArgumentParser(description="FNIRSI USB power meter GUI for macOS")
    ap.add_argument("--demo", action="store_true", help="run with synthetic data (no meter needed)")
    args = ap.parse_args()
    app = QApplication(sys.argv)
    win = MainWindow(demo=args.demo)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
