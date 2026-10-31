"""Tkinter GUI: download Thingy:53 logs and analyze them in the kart frame."""

from datetime import datetime
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.widgets import SpanSelector  # noqa: E402
import numpy as np  # noqa: E402

from . import analysis, data, device, sensorcal, synth  # noqa: E402

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"
DEVICE_COLORS = ("tab:red", "tab:green", "tab:blue")
KART_COLORS = ("tab:orange", "tab:purple", "tab:gray")
GG_MAX_POINTS = 20000
NO_UNIT = "(none)"
DEMO_UNIT = "demo"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Go-kart accelerometer analyzer")
        self.geometry("1400x900")
        self.sessions = []
        self.session = None
        self.cal = None
        self.kart_raw = None
        self.kart = None
        self.source = ""
        self.worker_queue = queue.Queue()
        self._pending = None
        self._pending_range = None
        self.rate = analysis.SAMPLE_RATE_HZ
        self.acc = None
        self.sensor_cal = None
        self.unit_serial = None
        try:
            self.store = sensorcal.Store()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Calibration file", f"{exc}\nStarting without sensor calibration.")
            self.store = sensorcal.Store("/nonexistent/calibration.json")

        self._build_toolbar()
        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        body.add(self._build_controls(body), weight=0)
        body.add(self._build_plots(body), weight=1)
        self.status_var.set("Open a CSV, download from the logger, or load demo data.")
        self.refresh_ports()

    # ---------- layout ----------

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, width=28)
        self.port_box.pack(side=tk.LEFT)
        ttk.Button(bar, text="Refresh", command=self.refresh_ports).pack(side=tk.LEFT)
        ttk.Button(bar, text="Status", command=self.device_status).pack(side=tk.LEFT)
        self.download_btn = ttk.Button(bar, text="Download", command=self.device_download)
        self.download_btn.pack(side=tk.LEFT)
        self.skip_invalid = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Skip invalid pages", variable=self.skip_invalid).pack(side=tk.LEFT)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(bar, text="Open CSV…", command=self.open_csv).pack(side=tk.LEFT)
        ttk.Button(bar, text="Save CSV…", command=self.save_csv).pack(side=tk.LEFT)
        ttk.Button(bar, text="Demo data", command=self.load_demo).pack(side=tk.LEFT)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Label(bar, text="Session:").pack(side=tk.LEFT)
        self.session_var = tk.StringVar()
        self.session_box = ttk.Combobox(bar, textvariable=self.session_var, width=30, state="readonly")
        self.session_box.pack(side=tk.LEFT)
        self.session_box.bind("<<ComboboxSelected>>", lambda e: self.select_session())
        self.progress = ttk.Progressbar(bar, length=140, mode="determinate")
        self.progress.pack(side=tk.RIGHT)
        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, anchor=tk.W, padding=(6, 0)).pack(fill=tk.X)

    def _spin(self, parent, label, var, lo, hi, step, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W)
        box = ttk.Spinbox(parent, textvariable=var, from_=lo, to=hi, increment=step, width=8,
                          command=self.schedule_update)
        box.grid(row=row, column=1, sticky=tk.W)
        box.bind("<Return>", lambda e: self.schedule_update())
        box.bind("<FocusOut>", lambda e: self.schedule_update())
        return box

    def _build_controls(self, parent):
        frame = ttk.Frame(parent, padding=6, width=330)

        sens = ttk.LabelFrame(frame, text="Sensor calibration (per unit)", padding=6)
        sens.pack(fill=tk.X, pady=(0, 6))
        self.unit_var = tk.StringVar(value=NO_UNIT)
        self.unit_box = ttk.Combobox(sens, textvariable=self.unit_var, width=30, state="readonly")
        self.unit_box.pack(fill=tk.X)
        self.unit_box.bind("<<ComboboxSelected>>", lambda e: self.select_unit(self._unit_choice()))
        self.unit_info = tk.StringVar(value="No sensor calibration applied")
        ttk.Label(sens, textvariable=self.unit_info, justify=tk.LEFT, font=("Menlo", 10)).pack(
            anchor=tk.W, pady=(2, 2))
        ttk.Button(sens, text="Calibrate / check from this session…",
                   command=self.open_calibration).pack(fill=tk.X)
        self._refresh_units()

        ref = ttk.LabelFrame(frame, text="Reference (at rest = 1 g up)", padding=6)
        ref.pack(fill=tk.X)
        self.ref_start = tk.DoubleVar(value=0.5)
        self.ref_dur = tk.DoubleVar(value=2.0)
        self._spin(ref, "Start (s)", self.ref_start, 0, 1e5, 0.1, 0)
        self._spin(ref, "Duration (s)", self.ref_dur, 0.05, 600, 0.1, 1)
        ttk.Button(ref, text="Pick window on plot", command=self.pick_reference).grid(
            row=2, column=0, sticky=tk.EW, pady=(4, 0))
        ttk.Button(ref, text="Auto-find rest", command=self.auto_rest).grid(
            row=2, column=1, sticky=tk.EW, pady=(4, 0))
        self.normalize = tk.BooleanVar(value=True)
        ttk.Checkbutton(ref, text="Scale so reference reads 1 g", variable=self.normalize,
                        command=self.schedule_update).grid(row=3, column=0, columnspan=2, sticky=tk.W)
        self.ref_info = tk.StringVar()
        ttk.Label(ref, textvariable=self.ref_info, justify=tk.LEFT, font=("Menlo", 10)).grid(
            row=4, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))

        mount = ttk.LabelFrame(frame, text="Mounting", padding=6)
        mount.pack(fill=tk.X, pady=6)
        ttk.Label(mount, text="Forward axis").grid(row=0, column=0, sticky=tk.W)
        self.forward_var = tk.StringVar(value="+X")
        fwd = ttk.Combobox(mount, textvariable=self.forward_var, values=list(analysis.DEVICE_AXES),
                           width=6, state="readonly")
        fwd.grid(row=0, column=1, sticky=tk.W)
        fwd.bind("<<ComboboxSelected>>", lambda e: self.schedule_update())
        self.yaw_var = tk.DoubleVar(value=0.0)
        self._spin(mount, "Yaw trim (°)", self.yaw_var, -180, 180, 1, 1)
        ttk.Button(mount, text="Auto yaw from launch", command=self.auto_yaw).grid(
            row=2, column=0, columnspan=2, sticky=tk.EW, pady=(4, 0))
        ttk.Label(mount, text="Kart frame: +long = accelerating,\n+lat = turning left, vert = 1 g at rest",
                  foreground="gray").grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))

        disp = ttk.LabelFrame(frame, text="Display", padding=6)
        disp.pack(fill=tk.X)
        self.smooth_var = tk.DoubleVar(value=0.25)
        self._spin(disp, "Smoothing (s)", self.smooth_var, 0, 5, 0.05, 0)
        self.show_raw = tk.BooleanVar(value=True)
        ttk.Checkbutton(disp, text="Show unsmoothed trace", variable=self.show_raw,
                        command=self.schedule_update).grid(row=1, column=0, columnspan=2, sticky=tk.W)

        summ = ttk.LabelFrame(frame, text="Visible range (smoothed)", padding=6)
        summ.pack(fill=tk.BOTH, expand=True, pady=6)
        self.summary = tk.Text(summ, width=38, height=22, font=("Menlo", 10), relief=tk.FLAT,
                               background=self.cget("background"))
        self.summary.pack(fill=tk.BOTH, expand=True)
        return frame

    def _figure_tab(self, notebook, title):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text=title)
        fig = Figure(layout="constrained")
        canvas = FigureCanvasTkAgg(fig, master=tab)
        NavigationToolbar2Tk(canvas, tab).update()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        return fig, canvas

    def _build_plots(self, parent):
        nb = ttk.Notebook(parent)
        self.ts_fig, self.ts_canvas = self._figure_tab(nb, "Time series")
        self.gg_fig, self.gg_canvas = self._figure_tab(nb, "G-G diagram")
        self.tm_fig, self.tm_canvas = self._figure_tab(nb, "Timing")
        self.ts_axes = self.ts_fig.subplots(3, 1, sharex=True)
        self.span = None
        return nb

    # ---------- data sources ----------

    def refresh_ports(self):
        try:
            ports = device.list_ports()
        except ImportError:
            self.status_var.set("pyserial is not installed; see requirements.txt")
            return
        values = [f"{d}  ({desc})" for d, desc, _ in ports]
        self.port_box["values"] = values
        loggers = [v for v, p in zip(values, ports) if p[2]]
        if loggers:
            self.port_var.set(loggers[0])
        elif values and not self.port_var.get():
            self.port_var.set(values[0])

    def _port(self):
        name = self.port_var.get().split("  (")[0].strip()
        if not name:
            raise device.ProtocolError("Select a serial port")
        return name

    def device_status(self):
        try:
            self.status_var.set(device.status(self._port()))
        except Exception as exc:  # serial errors are OSError subclasses
            messagebox.showerror("Status", str(exc))

    def device_download(self):
        try:
            port = self._port()
        except device.ProtocolError as exc:
            messagebox.showerror("Download", str(exc))
            return
        self.download_btn.state(["disabled"])
        self.status_var.set(f"Downloading from {port}…")
        self.progress["value"] = 0

        def work():
            try:
                serial = device.serial_number(port)
                result = device.download(
                    port, self.skip_invalid.get(),
                    lambda done, total: self.worker_queue.put(("progress", done, total)))
                self.worker_queue.put(("done", (serial, *result)))
            except Exception as exc:
                self.worker_queue.put(("error", exc))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll_worker)

    def _poll_worker(self):
        try:
            while True:
                msg = self.worker_queue.get_nowait()
                if msg[0] == "progress":
                    self.progress["maximum"] = msg[2]
                    self.progress["value"] = msg[1]
                elif msg[0] == "error":
                    self.download_btn.state(["!disabled"])
                    self.status_var.set("Download failed")
                    messagebox.showerror("Download", str(msg[1]))
                    return
                else:
                    self.download_btn.state(["!disabled"])
                    self._download_done(*msg[1])
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll_worker)

    def _download_done(self, serial, status_line, records, warnings):
        sessions = data.sessions_from_records(records)
        note = ""
        if sessions:
            RECORDINGS_DIR.mkdir(exist_ok=True)
            now = datetime.now()
            path = RECORDINGS_DIR / f"kart-{now:%Y%m%d-%H%M%S}.csv"
            data.save_csv(path, sessions)
            sensorcal.write_meta(path, serial=serial, status=status_line,
                                 downloaded=now.isoformat(timespec="seconds"))
            note = f"; saved {path.name}"
            self.set_sessions(sessions, path.name, serial)
        self.status_var.set(f"{status_line} — {len(records)} samples{note}")
        if warnings:
            messagebox.showwarning("Download", "\n".join(warnings))

    def open_csv(self):
        path = filedialog.askopenfilename(
            initialdir=RECORDINGS_DIR if RECORDINGS_DIR.exists() else None,
            filetypes=[("CSV", "*.csv"), ("All files", "*")])
        if not path:
            return
        try:
            sessions = data.load_csv(path)
        except Exception as exc:
            messagebox.showerror("Open CSV", str(exc))
            return
        self.set_sessions(sessions, Path(path).name, sensorcal.read_meta(path).get("serial"))

    def save_csv(self):
        if not self.sessions:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)  # the dialog already confirmed overwrite
            data.save_csv(path, self.sessions)
        except Exception as exc:
            messagebox.showerror("Save CSV", str(exc))

    def load_demo(self):
        self.forward_var.set("+X")
        self.yaw_var.set(0.0)
        self.set_sessions([synth.synthetic_session()], "demo (device -Y forward, tilted)", DEMO_UNIT)

    def set_sessions(self, sessions, source, serial=None):
        """`serial` is the recording's unit; None means unknown (use the only stored unit)."""
        self.sessions = sessions
        self.source = source
        self.unit_serial = serial
        if serial is None and len(self.store.units) == 1:
            serial = next(iter(self.store.units))
        self.select_unit(serial if serial in self.store.units else None, redraw=False)
        labels = [f"{s.number}: {s.duration_s:.1f} s, {len(s.time_us)} samples" for s in sessions]
        self.session_box["values"] = labels
        if not sessions:
            self.session = None
            self.status_var.set(f"{source}: no samples")
            return
        longest = max(range(len(sessions)), key=lambda i: len(sessions[i].time_us))
        self.session_box.current(longest)
        self.select_session()

    def select_session(self):
        self.session = self.sessions[self.session_box.current()]
        self._apply_sensor_cal()
        self.rate = analysis.sample_rate(self.session.t)
        self.auto_rest(redraw=False)
        self.update_all(reset_view=True)
        self._draw_timing()

    # ---------- sensor calibration ----------

    def _refresh_units(self):
        self.unit_box["values"] = [NO_UNIT] + [
            f"{serial}  {cal.name}".rstrip() for serial, cal in self.store.units.items()]

    def _unit_choice(self):
        value = self.unit_var.get()
        return None if value == NO_UNIT else value.split()[0]

    def select_unit(self, serial, redraw=True):
        self.sensor_cal = self.store.get(serial) if serial else None
        if self.sensor_cal:
            c = self.sensor_cal
            self.unit_var.set(f"{serial}  {c.name}".rstrip())
            mismatch = ""
            if self.unit_serial not in (None, DEMO_UNIT, serial):
                mismatch = f"\n⚠ recording is from {self.unit_serial}"
            self.unit_info.set(f"{c.describe()}\nfaces {' '.join(c.faces)}, "
                               f"check rms {c.rms_error_g * 1000:.1f} mg{mismatch}")
        else:
            self.unit_var.set(NO_UNIT)
            self.unit_info.set("No sensor calibration applied")
        if redraw and self.session is not None:
            self._apply_sensor_cal()
            self.update_all()

    def _apply_sensor_cal(self):
        acc = self.session.acc
        self.acc = self.sensor_cal.apply(acc) if self.sensor_cal else acc

    def open_calibration(self):
        if self.session is None:
            messagebox.showinfo("Calibration", "Load a calibration recording first.")
            return
        CalibrationDialog(self)

    def save_unit_cal(self, serial, cal):
        self.store.put(serial, cal)
        self._refresh_units()
        self.select_unit(serial)

    # ---------- analysis ----------

    def schedule_update(self):
        if self._pending:
            self.after_cancel(self._pending)
        self._pending = self.after(150, self.update_all)

    def _num(self, var, default):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    def update_all(self, reset_view=False):
        self._pending = None
        s = self.session
        if s is None:
            return
        try:
            self.cal = analysis.calibrate(
                s.t, self.acc, self._num(self.ref_start, 0.5), self._num(self.ref_dur, 2.0),
                self.forward_var.get(), self._num(self.yaw_var, 0.0), self.normalize.get())
        except ValueError as exc:
            self.cal = None
            self.ref_info.set(f"⚠ {exc}")
        if self.cal:
            c = self.cal
            m = c.ref_mean / analysis.G
            warn = ""
            if c.noise_g > 0.05:
                warn += "\n⚠ noisy: kart may be moving"
            if abs(c.magnitude_g - 1) > 0.05:
                warn += ("\n|g| ≠ 1: readings scaled ×%.3f" % c.scale if self.normalize.get()
                         else "\n⚠ |g| far from 1: scale error?")
            self.ref_info.set(
                f"mean  x={m[0]:+.3f} y={m[1]:+.3f} z={m[2]:+.3f} g\n"
                f"|g| = {c.magnitude_g:.3f}   noise = {c.noise_g:.3f} g\n"
                f"tilt of +Z from up = {c.tilt_deg:.1f}°\n"
                f"samples = {c.samples}{warn}")
            self.kart_raw = analysis.to_kart_g(self.acc, c)
            self.kart = analysis.smooth(self.kart_raw, self._num(self.smooth_var, 0.25), self.rate)
        self._draw_timeseries(reset_view)

    def auto_yaw(self):
        if self.session is None or self.cal is None:
            return
        try:
            yaw, t0 = analysis.estimate_launch_yaw(
                self.session.t, self.kart_raw,
                after_s=self._num(self.ref_start, 0) + self._num(self.ref_dur, 0), rate=self.rate)
        except ValueError as exc:
            messagebox.showinfo("Auto yaw", str(exc))
            return
        new = (self._num(self.yaw_var, 0.0) + yaw + 180) % 360 - 180
        self.yaw_var.set(round(new, 1))
        self.status_var.set(f"Launch detected at {t0:.2f} s; yaw trim set to {new:.1f}°")
        self.update_all()

    def auto_rest(self, redraw=True):
        if self.session is None:
            return
        try:
            start, dur = analysis.find_rest(self.session.t, self.acc)
        except ValueError as exc:
            self.status_var.set(f"Auto-find rest: {exc}")
            return
        self.ref_start.set(round(start, 2))
        self.ref_dur.set(round(dur, 2))
        self.status_var.set(f"Reference window {start:.2f}–{start + dur:.2f} s (auto)")
        if redraw:
            self.update_all()

    def pick_reference(self):
        if self.span:
            self.span.set_active(True)
            self.status_var.set("Drag across a stationary section of the top plot")

    def _on_span(self, xmin, xmax):
        self.span.set_active(False)
        if xmax - xmin < 0.05:
            return
        self.ref_start.set(round(xmin, 2))
        self.ref_dur.set(round(xmax - xmin, 2))
        self.status_var.set(f"Reference window {xmin:.2f}–{xmax:.2f} s")
        self.update_all()

    # ---------- drawing ----------

    def _draw_timeseries(self, reset_view):
        s = self.session
        axes = self.ts_axes
        xlim = None if reset_view else axes[0].get_xlim()
        for ax in axes:
            ax.clear()
        t = s.t
        win = self._num(self.smooth_var, 0.25)
        dev = self.acc / analysis.G
        dev_s = analysis.smooth(dev, win, self.rate)
        for i, name in enumerate("xyz"):
            if self.show_raw.get():
                axes[0].plot(t, dev[:, i], color=DEVICE_COLORS[i], lw=0.4, alpha=0.3)
            axes[0].plot(t, dev_s[:, i], color=DEVICE_COLORS[i], lw=1, label=f"device {name}")
        axes[0].set_ylabel("device (g)")
        axes[0].set_title(f"{self.source} — session {s.number}", loc="left", fontsize=10)

        if self.kart is not None:
            for i, name in enumerate(("longitudinal", "lateral")):
                if self.show_raw.get():
                    axes[1].plot(t, self.kart_raw[:, i], color=KART_COLORS[i], lw=0.4, alpha=0.3)
                axes[1].plot(t, self.kart[:, i], color=KART_COLORS[i], lw=1.2, label=name)
            axes[1].plot(t, np.hypot(self.kart[:, 0], self.kart[:, 1]), color="black", lw=0.8,
                         alpha=0.6, label="combined")
            if self.show_raw.get():
                axes[2].plot(t, self.kart_raw[:, 2], color=KART_COLORS[2], lw=0.4, alpha=0.3)
            axes[2].plot(t, self.kart[:, 2], color="black", lw=1, label="vertical")
        axes[1].set_ylabel("kart (g)\n+accel / +left")
        axes[2].set_ylabel("vertical (g)")
        axes[2].set_xlabel("time since session start (s)")

        start, dur = self._num(self.ref_start, 0.5), self._num(self.ref_dur, 2.0)
        for ax in axes:
            ax.axvspan(start, start + dur, color="gold", alpha=0.3, lw=0)
            ax.axhline(0, color="gray", lw=0.5)
            ax.grid(True, alpha=0.3)
            if ax.get_legend_handles_labels()[0]:
                ax.legend(loc="upper right", fontsize=8, ncols=3)
        if xlim:
            axes[0].set_xlim(xlim)
        axes[0].callbacks.connect("xlim_changed", lambda ax: self._schedule_range())
        self.span = SpanSelector(axes[0], self._on_span, "horizontal", useblit=True,
                                 props=dict(facecolor="gold", alpha=0.4), interactive=False)
        self.span.set_active(False)
        self.ts_canvas.draw_idle()
        self._update_range()

    def _schedule_range(self):
        if self._pending_range:
            self.after_cancel(self._pending_range)
        self._pending_range = self.after(200, self._update_range)

    def _update_range(self):
        self._pending_range = None
        if self.session is None or self.kart is None:
            return
        t = self.session.t
        lo, hi = self.ts_axes[0].get_xlim()
        mask = (t >= lo) & (t <= hi)
        self._draw_gg(t[mask], self.kart[mask], lo, hi)
        self._write_summary(t[mask], self.kart[mask])

    def _draw_gg(self, t, kart, lo, hi):
        fig = self.gg_fig
        fig.clear()
        ax = fig.add_subplot()
        if len(t):
            step = max(1, len(t) // GG_MAX_POINTS)
            sc = ax.scatter(kart[::step, 1], kart[::step, 0], c=t[::step], s=3, cmap="viridis")
            fig.colorbar(sc, ax=ax, label="time (s)")
        lim = max(1.5, float(np.abs(kart[:, :2]).max()) * 1.1) if len(t) else 1.5
        for r in (0.5, 1.0, 1.5, 2.0):
            ax.add_patch(matplotlib.patches.Circle((0, 0), r, fill=False, color="gray", lw=0.6, ls="--"))
        ax.axhline(0, color="gray", lw=0.5)
        ax.axvline(0, color="gray", lw=0.5)
        ax.set_xlim(lim, -lim)  # left turns plot on the left
        ax.set_ylim(-lim, lim)
        ax.set_aspect("equal")
        ax.set_xlabel("lateral (g)   ← left turn | right turn →")
        ax.set_ylabel("longitudinal (g)   braking ↓ | accelerating ↑")
        ax.set_title(f"Friction circle, {lo:.1f}–{hi:.1f} s (visible time range)", fontsize=10)
        self.gg_canvas.draw_idle()

    def _write_summary(self, t, kart):
        s = self.session
        lines = [f"{len(t)} samples, {t[-1] - t[0]:.1f} s" if len(t) else "no samples"]
        if len(t):
            st = analysis.summarize(t, kart)
            fmt = {
                "max_accel": "Max accel", "max_brake": "Max braking", "max_left": "Max left",
                "max_right": "Max right", "max_combined": "Max combined",
                "vert_min": "Vertical min", "vert_max": "Vertical max",
            }
            lines.append("")
            for key, label in fmt.items():
                value, when = st[key]
                lines.append(f"{label:<13}{value:+6.2f} g @ {when:7.1f} s")
        dt = np.diff(s.time_us) / 1e3
        gaps = s.gaps()
        lines += ["", "Session timing", f"  rate {self.rate:.1f} Hz (nominal 100)", f"  dt mean {dt.mean():.2f} ms, max {dt.max():.2f} ms" if len(dt) else "",
                  f"  gaps > 15 ms: {len(gaps)}"]
        lines += [f"    {when:8.2f} s: {ms:.1f} ms" for when, ms in gaps[:10]]
        self.summary.configure(state=tk.NORMAL)
        self.summary.delete("1.0", tk.END)
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state=tk.DISABLED)

    def _draw_timing(self):
        fig = self.tm_fig
        fig.clear()
        s = self.session
        if len(s.time_us) < 2:
            self.tm_canvas.draw_idle()
            return
        dt = np.diff(s.time_us) / 1e3
        a1, a2 = fig.subplots(1, 2, width_ratios=(3, 1))
        a1.plot(s.t[1:], dt, lw=0.6)
        a1.axhline(15, color="red", lw=0.8, ls="--", label="15 ms firmware limit")
        a1.set_xlabel("time (s)")
        a1.set_ylabel("sample interval (ms)")
        a1.legend(fontsize=8)
        a1.grid(True, alpha=0.3)
        a2.hist(dt, bins=80, orientation="horizontal")
        a2.set_xscale("log")
        a2.set_xlabel("count")
        a2.sharey(a1)
        self.tm_canvas.draw_idle()


class CalibrationDialog(tk.Toplevel):
    """Fit or check per-unit offset/gain from still segments of the current session."""

    COLUMNS = ("use", "time", "face", "x", "y", "z", "raw", "corrected", "noise")

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Sensor calibration")
        self.geometry("900x560")
        self.fitted = None
        s = app.session
        self.segs = sensorcal.segments(s.t, s.acc)
        self.use = [seg["face"] is not None for seg in self.segs]

        ttk.Label(self, padding=6, justify=tk.LEFT, text=(
            "Record one session with the unit resting still on several faces, about 5 s each, "
            "on a flat surface.\nBest: all 6 faces (sphere fit, insensitive to surface tilt). "
            "Minimum: +X, +Y, +Z up (level-surface fit: ~12 mg and 1.3 % gain error per degree "
            "of tilt). Double-click a row to include or exclude it.\nWith a saved "
            "calibration selected, 'corrected' is the check: record a separate session with 2–3 "
            "orientations (e.g. as installed) and each should read 1.000 g.")
        ).pack(fill=tk.X)
        tree = ttk.Treeview(self, columns=self.COLUMNS, show="headings", height=12)
        heads = ("use", "time (s)", "face up", "x (g)", "y (g)", "z (g)", "|g| raw",
                 "|g| corrected", "noise (mg)")
        for col, head in zip(self.COLUMNS, heads):
            tree.heading(col, text=head)
            tree.column(col, width=130 if col == "time" else 85, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=6)
        tree.bind("<Double-1>", self._toggle)
        self.tree = tree

        self.result = tk.StringVar()
        ttk.Label(self, textvariable=self.result, font=("Menlo", 10), justify=tk.LEFT,
                  padding=6).pack(fill=tk.X)
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Fit", command=self.fit).pack(side=tk.LEFT)
        ttk.Label(bar, text="  Unit serial").pack(side=tk.LEFT)
        serial = app.unit_serial if app.unit_serial != DEMO_UNIT else None
        self.serial_var = tk.StringVar(value=serial or app._unit_choice() or "")
        ttk.Entry(bar, textvariable=self.serial_var, width=20).pack(side=tk.LEFT)
        ttk.Label(bar, text="  Name").pack(side=tk.LEFT)
        existing = app.store.get(self.serial_var.get())
        self.name_var = tk.StringVar(value=existing.name if existing else "")
        ttk.Entry(bar, textvariable=self.name_var, width=16).pack(side=tk.LEFT)
        self.save_btn = ttk.Button(bar, text="Save to calibration file", command=self.save)
        self.save_btn.pack(side=tk.LEFT, padx=6)
        self.save_btn.state(["disabled"])
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        if not self.segs:
            self.result.set("No still segments of at least 1 s found in this session.")
        self._fill(app.sensor_cal)
        if app.sensor_cal:
            self._show_check(app.sensor_cal, "Check of the selected calibration")

    def _fill(self, cal):
        self.tree.delete(*self.tree.get_children())
        for i, seg in enumerate(self.segs):
            m = seg["mean"] / analysis.G
            corrected = (f"{np.linalg.norm(cal.apply(seg['mean'])) / analysis.G:.4f}"
                         if cal else "")
            self.tree.insert("", tk.END, iid=str(i), values=(
                "✓" if self.use[i] else "", f"{seg['start']:.1f}–{seg['end']:.1f}",
                seg["face"] or "tilted?", f"{m[0]:+.4f}", f"{m[1]:+.4f}", f"{m[2]:+.4f}",
                f"{np.linalg.norm(m):.4f}", corrected,
                f"{np.linalg.norm(seg['std']) / analysis.G * 1000:.1f}"))

    def _toggle(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.segs[int(row)]["face"]:
            self.use[int(row)] = not self.use[int(row)]
            self._fill(self.fitted or self.app.sensor_cal)

    def _selected(self):
        return [seg for seg, used in zip(self.segs, self.use) if used]

    def _show_check(self, cal, title):
        segs = [s for s in self._selected() if s["face"]]
        res = sensorcal.check(cal, segs)
        worst = max((abs(r) for r in res), default=0.0)
        self.result.set(f"{title}:\n{cal.describe()}\n"
                        f"faces {' '.join(cal.faces) or '-'}; |g| error rms "
                        f"{np.sqrt(np.mean(np.square(res))) * 1000 if res else 0:.1f} mg, "
                        f"worst {worst * 1000:.1f} mg")

    def fit(self):
        try:
            cal, _ = sensorcal.fit(self._selected())
        except ValueError as exc:
            messagebox.showerror("Fit", str(exc), parent=self)
            return
        self.fitted = cal
        self._fill(cal)
        self._show_check(cal, "Fitted")
        if len(self._selected()) <= 6:
            self.result.set(self.result.get() + "\nNote: with this few orientations the fit matches "
                            "them by construction; verify it on a separate check recording.")
        missing = [a for a, ok in zip(sensorcal.AXES, cal.solved) if not ok]
        if missing:
            self.result.set(self.result.get() + f"\n⚠ axis {', '.join(missing)} not fully solved"
                            " (\"?\"): add a face with that axis pointing up or down")
        self.save_btn.state(["!disabled"])

    def save(self):
        serial = self.serial_var.get().strip()
        if not serial:
            messagebox.showerror("Save", "Enter the unit serial number", parent=self)
            return
        self.fitted.name = self.name_var.get().strip()
        self.app.save_unit_cal(serial, self.fitted)
        self.result.set(self.result.get() + f"\nSaved for {serial} in {self.app.store.path}")


def main():
    App().mainloop()
