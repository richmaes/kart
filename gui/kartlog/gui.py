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

from . import alignment, analysis, data, device, gyrocal, laps, laptimes, path, sensorcal, synth  # noqa: E402
from .alignment_dialog import AlignmentDialog  # noqa: E402
from .launch_dialog import LaunchDialog  # noqa: E402
from .photo_import import PhotoImportDialog  # noqa: E402

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
        self.laps_by_session = {}
        self.gyro_intervals_by_session = {}
        self.gyro_intervals = []
        self.laps = []
        self.session_index = None
        self.recording_path = None
        self.external_times = {}
        self.alignments = {}
        self.external_time_drafts = {}
        self._loading_external_times = False
        self._path_estimated = False
        self._filter_pending = None
        self.filtered_acc = None
        self.span_mode = "reference"
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
        self._refresh_external_times()
        self.protocol("WM_DELETE_WINDOW", self._close)

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
        container = ttk.Frame(parent)
        self.controls_canvas = tk.Canvas(container, highlightthickness=0, width=330,
                                         background=self.cget("background"), yscrollincrement=20)
        self.controls_scrollbar = ttk.Scrollbar(
            container, orient=tk.VERTICAL, command=self.controls_canvas.yview)
        self.controls_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.controls_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        frame = ttk.Frame(self.controls_canvas, padding=6, width=330)
        window = self.controls_canvas.create_window(0, 0, window=frame, anchor=tk.NW)

        def resize(_event=None):
            width = max(frame.winfo_reqwidth(), self.controls_canvas.winfo_width())
            height = max(frame.winfo_reqheight(), self.controls_canvas.winfo_height())
            self.controls_canvas.itemconfigure(window, width=width, height=height)
            self.controls_canvas.configure(scrollregion=(0, 0, width, height))

        frame.bind("<Configure>", resize)
        self.controls_canvas.bind("<Configure>", resize)

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
        ttk.Button(sens, text="Calibration checklist…",
                   command=self.open_calibration_checklist).pack(fill=tk.X, pady=(4, 0))
        ttk.Button(sens, text="Gyro / temperature calibration…",
                   command=self.open_gyro_calibration).pack(fill=tk.X, pady=(4, 0))
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
        self.forward_var = tk.StringVar(value="+Z")
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
        ttk.Button(mount, text="Inspect launch...", command=self.inspect_launch).grid(
            row=4, column=0, columnspan=2, sticky=tk.EW, pady=(4, 0))

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
        # A sidebar-only tag runs before widget classes (e.g. Spinbox/Combobox).
        tag = f"SidebarWheel:{self.controls_canvas}"
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_class(tag, sequence, self._scroll_controls)

        def bind_wheel(widget):
            widget.bindtags((tag, *widget.bindtags()))
            for child in widget.winfo_children():
                bind_wheel(child)

        bind_wheel(container)
        return container

    def _scroll_controls(self, event):
        if getattr(event, "num", None) in (4, 5):
            units = -1 if event.num == 4 else 1
        else:
            delta = event.delta
            if not delta:
                return "break"
            if self.tk.call("tk", "windowingsystem") == "aqua":
                units = -int(delta)
            else:
                units = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.controls_canvas.yview_scroll(units, "units")
        return "break"

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
        self._build_laps_tab(nb)
        self._build_lap_times_tab(nb)
        self._build_path_tab(nb)
        self.span = None
        return nb

    def _build_path_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Apparent path (experimental)")
        ttk.Label(tab, wraplength=900, justify=tk.LEFT, text=(
            "Experimental inertial estimate, NOT a track map or racing line. Assume zero "
            "velocity and no rotation during the initial interval, with the device already "
            "rigidly mounted. Initial heading is relative to the Forward axis/Yaw trim. "
            "Bias and tilt errors accumulate rapidly; there is no GPS, distance validation, "
            "or forced lap closure."
        )).pack(anchor=tk.W, pady=(0, 6))
        controls = ttk.Frame(tab)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Initial stationary seconds").pack(side=tk.LEFT)
        self.path_rest_var = tk.StringVar(value="3.0")
        rest_entry = ttk.Entry(controls, textvariable=self.path_rest_var, width=8)
        rest_entry.pack(side=tk.LEFT, padx=6)
        self.path_rest_var.trace_add("write", lambda *_args: self._invalidate_path())
        settling = ttk.Frame(tab)
        settling.pack(fill=tk.X, pady=4)
        ttk.Label(settling, text="Startup settling seconds (excluded from bias/tilt fit)").pack(
            side=tk.LEFT)
        self.path_settle_var = tk.StringVar(value="1.0")
        ttk.Entry(settling, textvariable=self.path_settle_var, width=8).pack(side=tk.LEFT, padx=6)
        self.path_settle_var.trace_add("write", lambda *_args: self._invalidate_path())
        vibration = ttk.Frame(tab)
        vibration.pack(fill=tk.X, pady=4)
        ttk.Label(vibration, text="Startup acceleration variation limit (g)").pack(side=tk.LEFT)
        self.path_startup_acc_var = tk.StringVar(value="0.05")
        ttk.Entry(vibration, textvariable=self.path_startup_acc_var, width=8).pack(
            side=tk.LEFT, padx=6)
        self.path_startup_acc_var.trace_add("write", lambda *_args: self._invalidate_path())
        ttk.Label(vibration, text="Acceleration low-pass cutoff (Hz; 0 = Off)").pack(
            side=tk.LEFT, padx=(12, 0))
        self.vibration_cutoff_var = tk.StringVar(value="0")
        ttk.Spinbox(vibration, textvariable=self.vibration_cutoff_var,
                    from_=0, to=40, increment=0.5, width=8).pack(side=tk.LEFT, padx=6)
        self.vibration_cutoff_var.trace_add("write", lambda *_args: self._schedule_filter())
        gyro_limit = ttk.Frame(tab)
        gyro_limit.pack(fill=tk.X, pady=4)
        ttk.Label(gyro_limit, text="Startup gyro variation limit (rad/s)").pack(side=tk.LEFT)
        self.path_startup_gyro_var = tk.StringVar(value="0.03")
        ttk.Spinbox(gyro_limit, textvariable=self.path_startup_gyro_var,
                    from_=0.001, to=1, increment=0.01, width=8).pack(side=tk.LEFT, padx=6)
        self.path_startup_gyro_var.trace_add("write", lambda *_args: self._invalidate_path())
        fusion = ttk.Frame(tab)
        fusion.pack(fill=tk.X, pady=4)
        ttk.Label(fusion, text="Orientation estimator").pack(side=tk.LEFT)
        self.path_fusion_var = tk.StringVar(value="Cautious tilt fusion")
        fusion_box = ttk.Combobox(
            fusion, textvariable=self.path_fusion_var,
            values=("Cautious tilt fusion", "Gyro only"), state="readonly", width=24)
        fusion_box.pack(side=tk.LEFT, padx=6)
        fusion_box.bind("<<ComboboxSelected>>", lambda _event: self._invalidate_path())
        ttk.Label(fusion, text="Temperature compensation").pack(side=tk.LEFT, padx=(8, 0))
        self.path_temperature_var = tk.StringVar(value="Off")
        temperature_box = ttk.Combobox(
            fusion, textvariable=self.path_temperature_var, values=("Off", "Saved per-unit model"),
            state="readonly", width=20)
        temperature_box.pack(side=tk.LEFT, padx=4)
        temperature_box.bind("<<ComboboxSelected>>", lambda _event: self._invalidate_path())
        self.path_axes = []
        for name, axis in zip("XYZ", ("-Y", "+X", "+Z")):
            ttk.Label(controls, text=f"Accel {name} = gyro").pack(side=tk.LEFT, padx=(8, 0))
            variable = tk.StringVar(value=axis)
            self.path_axes.append(variable)
            box = ttk.Combobox(controls, textvariable=variable, values=list(analysis.DEVICE_AXES),
                               state="readonly", width=4)
            box.pack(side=tk.LEFT, padx=4)
            box.bind("<<ComboboxSelected>>", self._path_mapping_changed)
        ttk.Button(tab, text="Estimate apparent path", command=self.estimate_path).pack(anchor=tk.W)
        self.path_status = tk.StringVar(value="Load a gyro-enabled session, then estimate using the selected mapping.")
        ttk.Label(tab, textvariable=self.path_status, wraplength=900, justify=tk.LEFT).pack(
            anchor=tk.W, pady=6)
        self.path_fig = Figure(layout="constrained")
        self.path_canvas = FigureCanvasTkAgg(self.path_fig, master=tab)
        NavigationToolbar2Tk(self.path_canvas, tab).update()
        self.path_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _path_mapping_changed(self, _event=None):
        self._invalidate_path()

    def _invalidate_path(self):
        self._path_estimated = False
        if not hasattr(self, "path_fig"):
            return
        self.path_fig.clear()
        self.path_canvas.draw_idle()
        self.path_status.set("Estimate cleared; click Estimate apparent path using the selected mapping.")

    def estimate_path(self):
        self._invalidate_path()
        if self.session is None:
            self.path_status.set("Load a recording session first.")
            return
        try:
            rest_s = float(self.path_rest_var.get())
            settle_s = float(self.path_settle_var.get())
            startup_acc_limit = float(self.path_startup_acc_var.get())
            startup_gyro_limit = float(self.path_startup_gyro_var.get())
            cutoff = float(self.vibration_cutoff_var.get())
            yaw = float(self.yaw_var.get())
            if not np.isfinite(yaw):
                raise ValueError("Yaw trim must be finite")
            model = None
            if self.path_temperature_var.get() == "Saved per-unit model":
                if self.unit_serial in (None, DEMO_UNIT):
                    raise ValueError("Temperature compensation needs the recording's unit serial")
                model = gyrocal.Store().model(self.unit_serial)
                if model is None:
                    raise ValueError("No fitted temperature model for this unit; collect calibration points first")
            result = path.estimate(
                self.session, self.acc, rest_s, self.forward_var.get(), yaw,
                tuple(variable.get() for variable in self.path_axes), settle_s=settle_s,
                fuse_tilt=self.path_fusion_var.get() == "Cautious tilt fusion",
                stationary_intervals=self.gyro_intervals, temperature_model=model,
                startup_accel_variation_limit_g=startup_acc_limit,
                vibration_cutoff_hz=cutoff,
                startup_gyro_variation_limit_rad_s=startup_gyro_limit)
        except (ValueError, OSError, KeyError, TypeError, tk.TclError) as exc:
            self.path_status.set(f"Cannot estimate path: {exc}")
            messagebox.showerror("Apparent path", str(exc), parent=self)
            return
        ax = self.path_fig.add_subplot()
        positions = result.position[result.rest_end - 1:]
        times = self.session.t[result.rest_end - 1:] - self.session.t[0]
        step = max(1, len(times) // GG_MAX_POINTS)
        ax.plot(positions[:, 0], positions[:, 1], color="gray", lw=0.6)
        points = ax.scatter(positions[::step, 0], positions[::step, 1],
                            c=times[::step], cmap="viridis", s=5)
        self.path_fig.colorbar(points, ax=ax, label="seconds since first sample")
        ax.scatter([0], [0], marker="o", color="green", label="Start (assumed at rest)")
        ax.scatter([positions[-1, 0]], [positions[-1, 1]], marker="x", color="red", label="End")
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xlabel("initial forward direction (estimated m)")
        ax.set_ylabel("initial left direction (estimated m)")
        ax.set_title("Apparent path — unbounded drift; not a measured course")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        bias = ", ".join(f"{value:+.4f}" for value in result.gyro_bias)
        self.path_status.set(
            f"Initial {rest_s:g} s held at zero velocity. Residual gyro bias in accel axes: [{bias}] rad/s. "
            f"Bias/tilt fitted over {settle_s:g}–{rest_s:g} s after settling. "
            f"Startup acceleration variation limit: {startup_acc_limit:g} g. "
            f"Startup gyro variation limit: {startup_gyro_limit:g} rad/s. "
            f"Acceleration low-pass: {cutoff:g} Hz (0 = Off). "
            f"{self.path_fusion_var.get()}: {result.tilt_corrections} gravity-correction updates. "
            f"{result.bias_updates} explicit stationary bias updates; temperature: "
            f"{self.path_temperature_var.get()}. "
            "Orientation integrated in 3D; gravity removed; velocity/position integrated. "
            "Gravity does not correct yaw drift. Displayed meters are unvalidated. "
            "No closure or end-speed correction.")
        self.path_canvas.draw_idle()
        self._path_estimated = True

    def _schedule_filter(self):
        if self._filter_pending:
            self.after_cancel(self._filter_pending)
        self._filter_pending = self.after(250, self._update_filter)

    def _update_filter(self):
        self._filter_pending = None
        regenerate = self._path_estimated
        self.update_all()
        if regenerate and self.filtered_acc is not None:
            self.estimate_path()

    def _build_laps_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Lap comparison")
        controls = ttk.Frame(tab)
        controls.pack(fill=tk.X)
        ttk.Button(controls, text="Select on time series", command=self.mark_lap_range).grid(
            row=0, column=0, padx=(0, 6))
        ttk.Label(controls, text="Start (s)").grid(row=0, column=1)
        self.lap_start_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.lap_start_var, width=9).grid(row=0, column=2, padx=4)
        ttk.Label(controls, text="End (s)").grid(row=0, column=3)
        self.lap_end_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.lap_end_var, width=9).grid(row=0, column=4, padx=4)
        ttk.Button(controls, text="Add lap", command=self.add_lap).grid(row=0, column=5, padx=4)
        ttk.Button(controls, text="Update selected", command=self.update_lap).grid(
            row=0, column=6, padx=4)
        ttk.Button(controls, text="Remove selected", command=self.remove_lap).grid(
            row=0, column=7, padx=4)
        self.lap_mode_var = tk.StringVar(value="elapsed")
        ttk.Label(controls, text="Align by").grid(row=0, column=8, padx=(12, 2))
        ttk.Radiobutton(controls, text="Elapsed time", value="elapsed",
                        variable=self.lap_mode_var, command=self._draw_lap_comparison).grid(
                            row=0, column=9)
        ttk.Radiobutton(controls, text="Lap progress", value="progress",
                        variable=self.lap_mode_var, command=self._draw_lap_comparison).grid(
                            row=0, column=10)

        columns = ("lap", "start", "end", "duration", "include", "flags")
        self.lap_tree = ttk.Treeview(tab, columns=columns, show="headings", height=5,
                                     selectmode="browse")
        for column, heading, width in (
            ("lap", "Lap", 55), ("start", "Start (s)", 85), ("end", "End (s)", 85),
            ("duration", "Duration (s)", 100), ("include", "Overlay", 75),
            ("flags", "Flags", 260),
        ):
            self.lap_tree.heading(column, text=heading)
            self.lap_tree.column(column, width=width, anchor=tk.CENTER)
        self.lap_tree.pack(fill=tk.X, pady=(6, 2))
        self.lap_tree.bind("<<TreeviewSelect>>", self._select_lap)
        self.lap_tree.bind("<Double-1>", self._toggle_lap)

        self.lap_fig = Figure(layout="constrained")
        self.lap_axes = self.lap_fig.subplots(3, 1, sharex=True)
        self.lap_canvas = FigureCanvasTkAgg(self.lap_fig, master=tab)
        NavigationToolbar2Tk(self.lap_canvas, tab).update()
        self.lap_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _build_lap_times_tab(self, notebook):
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="Track lap times")
        ttk.Label(tab, text=(
            "Enter timing-system durations in order for the selected session (up to 40). "
            "Use seconds, e.g. 42.310, or m:ss.cc, e.g. 1:02.310 (up to three decimals). "
            "Leave unused trailing laps blank.\n"
            "Printed decimal places are reporting resolution, not guaranteed accuracy. These times are NOT "
            "aligned until you use Auto-align laps and confirm a preview."
        ), wraplength=1000, justify=tk.LEFT).pack(fill=tk.X, pady=(0, 8))
        self.external_times_title = tk.StringVar()
        ttk.Label(tab, textvariable=self.external_times_title).pack(anchor=tk.W)
        body = ttk.Frame(tab)
        body.pack(fill=tk.BOTH, expand=True, pady=8)
        self.external_time_vars = []
        self.external_time_entries = []
        for group in range(4):
            frame = ttk.Frame(body, padding=(0, 0, 12, 0))
            frame.grid(row=0, column=group, sticky=tk.N)
            for row in range(10):
                index = group * 10 + row
                ttk.Label(frame, text=f"Lap {index + 1}").grid(row=row, column=0, padx=4, pady=4)
                variable = tk.StringVar()
                entry = ttk.Entry(frame, textvariable=variable, width=11)
                entry.grid(row=row, column=1, pady=4)
                self.external_time_vars.append(variable)
                self.external_time_entries.append(entry)
                variable.trace_add("write", lambda *_args: self._external_times_edited())
        table_frame = ttk.Frame(body)
        table_frame.grid(row=0, column=4, sticky=tk.NSEW)
        body.columnconfigure(4, weight=1)
        body.rowconfigure(0, weight=1)
        self.external_times_tree = ttk.Treeview(
            table_frame, columns=("lap", "duration", "total"), show="headings", height=15)
        for name, title in (("lap", "Lap"), ("duration", "Duration (s)"),
                            ("total", "Cumulative (s)")):
            self.external_times_tree.heading(name, text=title)
            self.external_times_tree.column(name, width=105, anchor=tk.CENTER)
        scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.external_times_tree.yview)
        self.external_times_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.external_times_tree.pack(fill=tk.BOTH, expand=True)
        actions = ttk.Frame(tab)
        actions.pack(fill=tk.X)
        self.external_times_save = ttk.Button(actions, text="Save session lap times",
                                             command=self.save_external_times)
        self.external_times_save.pack(side=tk.LEFT)
        ttk.Button(actions, text="Import photo...", command=self.import_lap_photo).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="Auto-align laps...", command=self.auto_align_laps).pack(side=tk.LEFT)
        ttk.Button(actions, text="Revert to saved", command=self._refresh_external_times).pack(
            side=tk.LEFT, padx=8)
        ttk.Button(actions, text="Clear entries", command=self._clear_external_times).pack(side=tk.LEFT)
        self.external_times_status = tk.StringVar()
        ttk.Label(tab, textvariable=self.external_times_status, wraplength=1000,
                  justify=tk.LEFT).pack(fill=tk.X, pady=8)

    def _external_times_edited(self):
        if self._loading_external_times:
            return
        if self.session is not None:
            self.external_time_drafts[str(self.session.number)] = [
                variable.get() for variable in self.external_time_vars]
        self._preview_external_times()

    def auto_align_laps(self):
        try:
            if self.session is None:
                raise ValueError("Load a recording session first")
            values = laptimes.parse_entries([v.get() for v in self.external_time_vars])
            alignment.durations(values)
            if self.session.gyro is None or not np.isfinite(self.session.gyro).any():
                raise ValueError("Auto alignment needs a gyro-enabled recording")
            if self.filtered_acc is None:
                raise ValueError("Correct the acceleration filter settings before aligning")
            AlignmentDialog(self, values)
        except ValueError as exc:
            messagebox.showerror("Auto alignment", str(exc), parent=self)

    def apply_alignment(self, values, offset_s):
        complete, _partial = alignment.boundaries(self.session, values, offset_s)
        times = dict(self.external_times)
        saved = dict(self.alignments)
        number = str(self.session.number)
        times[number] = list(values)
        saved[number] = alignment.record(self.session, values, offset_s)
        if self.recording_path is not None:
            laptimes.save(self.recording_path, times, saved)
        self.external_times = times
        self.alignments = saved
        self.laps = complete
        self.laps_by_session[self.session_index] = complete
        self._refresh_external_times()
        self._refresh_lap_table()
        self._draw_timeseries(reset_view=False)
        self.status_var.set("Confirmed lap alignment applied. " + (
            "Saved beside the recording." if self.recording_path is not None else
            "In memory only; use Save CSV to persist it."))

    def _forget_alignment(self):
        """Detach manual edits before changing boundaries, including the disk copy."""
        number = str(self.session.number)
        if number not in self.alignments:
            return True
        saved = dict(self.alignments)
        del saved[number]
        try:
            if self.recording_path is not None:
                laptimes.save(self.recording_path, self.external_times, saved)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Remove saved alignment", str(exc), parent=self)
            return False
        self.alignments = saved
        self._preview_external_times()
        self.status_var.set("Manual boundary edit removed the saved auto alignment; manual edits are not persisted.")
        return True

    def import_lap_photo(self):
        if self.session is None:
            messagebox.showinfo("Photo import", "Load a recording session first.", parent=self)
            return
        filename = filedialog.askopenfilename(
            title="Select lap-time photo", filetypes=[("PNG/JPG photos", "*.png *.jpg *.jpeg"),
                                                     ("All files", "*")])
        if not filename:
            return
        try:
            PhotoImportDialog(self, filename)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Photo import", str(exc), parent=self)

    def _preview_external_times(self):
        self.external_times_tree.delete(*self.external_times_tree.get_children())
        if self.session is None:
            self.external_times_status.set("Load a recording session first.")
            return
        try:
            values = laptimes.parse_entries([v.get() for v in self.external_time_vars])
        except ValueError as exc:
            self.external_times_status.set(f"Cannot save: {exc}")
            return
        total = 0
        for number, value in enumerate(values, 1):
            total += value
            self.external_times_tree.insert("", tk.END, values=(
                number, laptimes.format_time(value), laptimes.format_time(total)))
        summary = f"{len(values)}/40 laps; total {laptimes.format_time(total)} s."
        if values:
            best = min(values)
            summary += f" Fastest: lap {values.index(best) + 1}, {laptimes.format_time(best)} s."
        saved = values == self.external_times.get(str(self.session.number), [])
        summary += " Saved." if saved else " Unsaved changes."
        if str(self.session.number) in self.alignments:
            summary += (" Confirmed alignment." if saved else
                        " Editing durations; saving will remove the previous alignment.")
        summary += (f" Companion file: {laptimes.sidecar(self.recording_path).name}."
                    if self.recording_path is not None else
                    " No recording file; use Save CSV to keep lap times on disk.")
        self.external_times_status.set(summary)

    def _refresh_external_times(self, use_draft=False):
        number = str(self.session.number) if self.session is not None else None
        values = ([laptimes.format_time(v) for v in self.external_times.get(number, [])]
                  if number is not None else [])
        if use_draft:
            values = self.external_time_drafts.get(number, values)
        self._loading_external_times = True
        try:
            for index, variable in enumerate(self.external_time_vars):
                variable.set(values[index] if index < len(values) else "")
                self.external_time_entries[index].configure(
                    state="normal" if self.session is not None else "disabled")
        finally:
            self._loading_external_times = False
        if number is not None:
            self.external_time_drafts[number] = [v.get() for v in self.external_time_vars]
        self.external_times_title.set(f"Session {number}" if number else "No session selected")
        self.external_times_save.configure(state="normal" if self.session is not None else "disabled")
        self._preview_external_times()

    def _clear_external_times(self):
        for variable in self.external_time_vars:
            variable.set("")

    def save_external_times(self):
        if self.session is None:
            return
        try:
            values = laptimes.parse_entries([v.get() for v in self.external_time_vars])
            saved = dict(self.external_times)
            saved[str(self.session.number)] = values
            if self.recording_path is not None:
                laptimes.save(self.recording_path, saved, self.alignments)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Save lap times", str(exc), parent=self)
            return
        self.external_times = saved
        number = str(self.session.number)
        if number in self.alignments and self.alignments[number]["durations_ms"] != values:
            del self.alignments[number]
            self.laps = []
            self.laps_by_session[self.session_index] = []
            self._refresh_lap_table()
            if self.filtered_acc is not None:
                self._draw_timeseries(reset_view=False)
        self._refresh_external_times()

    def _confirm_discard_external_times(self):
        if self.recording_path is None and any(self.external_times.values()):
            return messagebox.askyesno(
                "Unsaved lap times", "These lap times are only in memory. Use Save CSV to keep them.\n"
                "Discard them before continuing?", parent=self)
        for number, draft in self.external_time_drafts.items():
            try:
                unchanged = laptimes.parse_entries(draft) == self.external_times.get(number, [])
            except ValueError:
                unchanged = False
            if not unchanged:
                return messagebox.askyesno(
                    "Unsaved lap times", "Discard unsaved lap-time edits before continuing?", parent=self)
        return True

    def _close(self):
        if self._confirm_discard_external_times():
            self.destroy()

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
            if not self.set_sessions(sessions, path.name, serial, recording_path=path):
                note += "; current recording kept open"
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
            times = laptimes.load(path)
            saved_alignments = laptimes.load_alignments(path)
            meta = sensorcal.read_meta(path)
        except Exception as exc:
            messagebox.showerror("Open CSV", str(exc))
            return
        self.set_sessions(sessions, Path(path).name, meta.get("serial"),
                          recording_path=Path(path), external_times=times,
                          alignments=saved_alignments)

    def save_csv(self):
        if not self.sessions:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            times = dict(self.external_times)
            for number, draft in self.external_time_drafts.items():
                try:
                    times[number] = laptimes.parse_entries(draft)
                except ValueError as exc:
                    raise ValueError(f"Session {number}: {exc}") from exc
            meta = sensorcal.read_meta(self.recording_path) if self.recording_path else {}
            if self.unit_serial not in (None, DEMO_UNIT):
                meta["serial"] = self.unit_serial
            Path(path).unlink(missing_ok=True)  # the dialog already confirmed overwrite
            data.save_csv(path, self.sessions)
            sensorcal.write_meta(path, **meta)
            laptimes.save(path, times, self.alignments)
        except Exception as exc:
            messagebox.showerror("Save CSV", str(exc))
            return
        self.recording_path = Path(path)
        self.external_times = times
        for index, session in enumerate(self.sessions):
            number = str(session.number)
            if number in self.alignments and self.alignments[number]["durations_ms"] != times.get(number):
                self.laps_by_session[index] = []
                if index == self.session_index:
                    self.laps = []
        self.alignments = {number: saved for number, saved in self.alignments.items()
                           if saved["durations_ms"] == times.get(number)}
        self.external_time_drafts = {}
        self._refresh_external_times()
        self._refresh_lap_table()
        if self.filtered_acc is not None:
            self._draw_timeseries(reset_view=False)

    def load_demo(self):
        previous_forward, previous_yaw = self.forward_var.get(), self.yaw_var.get()
        self.forward_var.set("+X")
        self.yaw_var.set(0.0)
        if not self.set_sessions([synth.synthetic_session()],
                                 "demo (device -Y forward, tilted)", DEMO_UNIT):
            self.forward_var.set(previous_forward)
            self.yaw_var.set(previous_yaw)

    def set_sessions(self, sessions, source, serial=None, recording_path=None, external_times=None,
                     alignments=None):
        """`serial` is the recording's unit; None means unknown (use the only stored unit)."""
        if not self._confirm_discard_external_times():
            return False
        self.recording_path = recording_path
        self.external_times = external_times if external_times is not None else {}
        self.alignments = dict(alignments) if alignments is not None else {}
        self.external_time_drafts = {}
        self.sessions = sessions
        self.source = source
        self.unit_serial = serial
        self.laps_by_session = {}
        self.gyro_intervals_by_session = {}
        self.gyro_intervals = []
        self.laps = []
        self.session_index = None
        stale = []
        for index, session in enumerate(sessions):
            number = str(session.number)
            if number in self.alignments:
                try:
                    self.laps_by_session[index] = alignment.restore(
                        session, self.external_times.get(number, []), self.alignments[number])
                except ValueError as exc:
                    stale.append(f"Session {number}: {exc}")
                    del self.alignments[number]
        if stale:
            messagebox.showwarning("Saved lap alignment", "\n".join(stale), parent=self)
        if serial is None and len(self.store.units) == 1:
            serial = next(iter(self.store.units))
        self.select_unit(serial if serial in self.store.units else None, redraw=False)
        labels = [f"{s.number}: {s.duration_s:.1f} s, {len(s.time_us)} samples" for s in sessions]
        self.session_box["values"] = labels
        if not sessions:
            self.session = None
            self._refresh_external_times()
            self._invalidate_path()
            self.status_var.set(f"{source}: no samples")
            return True
        longest = max(range(len(sessions)), key=lambda i: len(sessions[i].time_us))
        self.session_box.current(longest)
        self.select_session()
        return True

    def select_session(self):
        if self.session_index is not None:
            self.laps_by_session[self.session_index] = self.laps
            self.gyro_intervals_by_session[self.session_index] = self.gyro_intervals
        self.session_index = self.session_box.current()
        self.session = self.sessions[self.session_index]
        self.laps = self.laps_by_session.get(self.session_index, [])
        self.gyro_intervals = self.gyro_intervals_by_session.get(self.session_index, [])
        self._refresh_external_times(use_draft=True)
        self._apply_sensor_cal()
        self.rate = analysis.sample_rate(self.session.t)
        self.auto_rest(redraw=False)
        self.update_all(reset_view=True)
        self._refresh_lap_table()
        self._draw_timing()

    def _select_lap(self, _event=None):
        selected = self.lap_tree.selection()
        if selected and self.session is not None and int(selected[0]) < len(self.laps):
            lap = self.laps[int(selected[0])]
            self.lap_start_var.set(f"{lap.start_us / 1e6:.3f}")
            self.lap_end_var.set(f"{lap.end_us / 1e6:.3f}")
        self._draw_lap_comparison()

    def _lap_from_entries(self, include=True):
        try:
            start_us = round(float(self.lap_start_var.get()) * 1e6)
            end_us = round(float(self.lap_end_var.get()) * 1e6)
        except ValueError as exc:
            raise ValueError("Enter numeric lap start and end times") from exc
        return laps.Lap(start_us, end_us, include)

    def add_lap(self):
        if self.session is None:
            return
        try:
            lap = self._lap_from_entries()
            laps.validate(self.session, lap, self.laps)
        except ValueError as exc:
            messagebox.showerror("Add lap", str(exc))
            return
        if not self._forget_alignment():
            return
        self.laps.append(lap)
        self.laps.sort(key=lambda item: item.start_us)
        self._refresh_lap_table()
        self._draw_timeseries(reset_view=False)
        self._draw_lap_comparison()

    def update_lap(self):
        selected = self.lap_tree.selection()
        if not selected or self.session is None:
            return
        index = int(selected[0])
        current = self.laps[index]
        try:
            updated = self._lap_from_entries(current.include)
            laps.validate(self.session, updated, self.laps[:index] + self.laps[index + 1:])
        except ValueError as exc:
            messagebox.showerror("Update lap", str(exc))
            return
        if not self._forget_alignment():
            return
        self.laps[index] = updated
        self.laps.sort(key=lambda item: item.start_us)
        self._refresh_lap_table()
        self._draw_timeseries(reset_view=False)
        self._draw_lap_comparison()

    def remove_lap(self):
        selected = self.lap_tree.selection()
        if not selected:
            return
        if not self._forget_alignment():
            return
        del self.laps[int(selected[0])]
        self._refresh_lap_table()
        self._draw_timeseries(reset_view=False)
        self._draw_lap_comparison()

    def _toggle_lap(self, event):
        if self.lap_tree.identify_column(event.x) != "#5":
            return
        row = self.lap_tree.identify_row(event.y)
        if not row:
            return
        lap = self.laps[int(row)]
        lap.include = not lap.include
        self._refresh_lap_table()
        self.lap_tree.selection_set(row)
        self._draw_lap_comparison()

    def _refresh_lap_table(self):
        self.lap_tree.delete(*self.lap_tree.get_children())
        if self.session is None:
            return
        for index, lap in enumerate(self.laps):
            flags = ", ".join(laps.flags(self.session, lap, self.laps)) or "—"
            self.lap_tree.insert("", tk.END, iid=str(index), values=(
                lap.external_number or index + 1, f"{lap.start_us / 1e6:.3f}", f"{lap.end_us / 1e6:.3f}",
                f"{lap.duration_s:.2f}", "✓" if lap.include else "",
                flags))
        self._draw_lap_comparison()

    def _draw_lap_comparison(self):
        if not hasattr(self, "lap_axes"):
            return
        for ax in self.lap_axes:
            ax.clear()
        included = [(lap.external_number or i + 1, lap) for i, lap in enumerate(self.laps) if lap.include]
        if self.session is None or self.kart is None or not included:
            self.lap_axes[1].text(0.5, 0.5, "Select laps to compare",
                                  ha="center", va="center", transform=self.lap_axes[1].transAxes)
            self.lap_axes[1].set_axis_off()
            self.lap_canvas.draw_idle()
            return
        mode = self.lap_mode_var.get()
        selected = self.lap_tree.selection()
        highlighted = None
        if selected and int(selected[0]) < len(self.laps):
            index = int(selected[0])
            highlighted = self.laps[index].external_number or index + 1
        try:
            result = laps.comparison(self.session, self.kart, included, mode)
        except ValueError as exc:
            self.lap_axes[1].text(0.5, 0.5, str(exc), ha="center", va="center",
                                  transform=self.lap_axes[1].transAxes)
            self.lap_canvas.draw_idle()
            return
        names = ("longitudinal", "lateral", "vertical")
        for axis_index, (ax, name) in enumerate(zip(self.lap_axes, names)):
            for curve_index, (number, x, values) in enumerate(result["curves"]):
                is_selected = number == highlighted
                style = ({"color": "#ff0000", "lw": 2.5, "alpha": 1.0, "zorder": 5}
                         if is_selected else {"lw": 0.8, "alpha": 0.35})
                ax.plot(x, values[:, axis_index], **style,
                        label=f"Lap {number}" if axis_index == 0
                        and (curve_index < 12 or is_selected) else None)
            ax.fill_between(result["x"], result["low"][:, axis_index],
                            result["high"][:, axis_index], color="black", alpha=0.15,
                            label="25–75% range" if axis_index == 0 else None)
            ax.plot(result["x"], result["median"][:, axis_index], color="black", lw=1.8,
                    label="Typical lap (median)" if axis_index == 0 else None)
            ax.axhline(0, color="gray", lw=0.5)
            ax.set_ylabel(f"{name} (g)")
            ax.grid(True, alpha=0.3)
        if mode == "progress":
            self.lap_axes[-1].set_xlabel("lap progress (%)")
            self.lap_axes[-1].set_xlim(0, 1)
            self.lap_axes[-1].set_xticks(np.linspace(0, 1, 6), labels=[f"{n}%" for n in range(0, 101, 20)])
        else:
            self.lap_axes[-1].set_xlabel("elapsed time since lap start (s)")
        self.lap_axes[0].set_title(
            f"{len(included)} included lap(s) — {mode} alignment; median template and IQR",
            loc="left", fontsize=10)
        self.lap_axes[0].legend(loc="upper right", fontsize=8, ncols=3)
        self.lap_canvas.draw_idle()

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

    def open_calibration_checklist(self):
        CalibrationChecklist(self)

    def open_gyro_calibration(self):
        if self.session is None or self.session.gyro is None:
            messagebox.showinfo("Gyro calibration", "Load a gyro-enabled recording first.")
            return
        try:
            store = gyrocal.Store()
            GyroCalibrationDialog(self, store)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("Gyro calibration", str(exc), parent=self)

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
        self._invalidate_path()
        s = self.session
        if s is None:
            return
        try:
            self.filtered_acc = analysis.vibration_filter(
                s.t, self.acc, float(self.vibration_cutoff_var.get()))
        except (ValueError, tk.TclError) as exc:
            self.filtered_acc = None
            self.cal = None
            self.kart_raw = None
            self.kart = None
            for ax in self.ts_axes:
                ax.clear()
            self.ts_axes[0].text(0.05, 0.5, f"Cannot filter: {exc}", transform=self.ts_axes[0].transAxes)
            self.ts_canvas.draw_idle()
            self.gg_fig.clear()
            self.gg_canvas.draw_idle()
            self.summary.configure(state=tk.NORMAL)
            self.summary.delete("1.0", tk.END)
            self.summary.insert("1.0", f"Cannot filter: {exc}")
            self.summary.configure(state=tk.DISABLED)
            self._draw_lap_comparison()
            self.status_var.set(f"Vibration filter: {exc}")
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
            filtered_kart = analysis.to_kart_g(self.filtered_acc, c)
            self.kart = analysis.smooth(filtered_kart, self._num(self.smooth_var, 0.25), self.rate)
        else:
            self.kart_raw = None
            self.kart = None
        self._draw_timeseries(reset_view)
        self._draw_lap_comparison()

    def inspect_launch(self):
        if self.session is None:
            messagebox.showinfo("Launch inspection", "Load a recording session first.", parent=self)
            return
        LaunchDialog(self)

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
            self.span_mode = "reference"
            self.span.set_active(True)
            self.status_var.set("Drag across a stationary section of the top plot")

    def mark_lap_range(self):
        if self.span:
            self.span_mode = "lap"
            self.span.set_active(True)
            self.status_var.set("Drag across one lap on the Time series tab")

    def _on_span(self, xmin, xmax):
        self.span.set_active(False)
        if xmax - xmin < 0.05:
            return
        if self.span_mode == "lap":
            self.lap_start_var.set(f"{xmin:.3f}")
            self.lap_end_var.set(f"{xmax:.3f}")
            self.status_var.set(f"Lap range {xmin:.2f}–{xmax:.2f} s; add or update the lap")
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
        dev_s = analysis.smooth(self.filtered_acc / analysis.G, win, self.rate)
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
            for lap in self.laps:
                ax.axvspan(lap.start_us / 1e6, lap.end_us / 1e6,
                           color="tab:blue" if lap.include else "gray",
                           alpha=0.08, lw=0)
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


class GyroCalibrationDialog(tk.Toplevel):
    def __init__(self, app, store):
        super().__init__(app)
        self.app = app
        self.store = store
        self.session = app.session
        self.title("Gyro / temperature calibration")
        self.geometry("850x570")
        self.transient(app)
        self.grab_set()
        self.serial = app.unit_serial if app.unit_serial != DEMO_UNIT else None
        ttk.Label(self, text=f"Recording unit: {self.serial or 'unknown (cannot save/apply per-unit model)'}",
                  padding=8).pack(fill=tk.X)
        ttk.Label(self, wraplength=800, justify=tk.LEFT, padding=8, text=(
            "Mark at least 10 seconds when you KNOW the device was motionless; times are "
            "seconds after the FIRST SAMPLE, not the session time-series timestamp. "
            "Quiet IMU data cannot prove zero speed. Allow warm-up and temperature settling. "
            "Measurements use native gyro axes (rad/s). Marked bias updates affect only "
            "subsequent samples; they do not reset velocity, position or yaw. "
            "Use for startup fit assumes rest and zero velocity from sample 0 through End."
        )).pack(fill=tk.X)
        controls = ttk.Frame(self, padding=8)
        controls.pack(fill=tk.X)
        self.start = tk.StringVar(value="1.0")
        self.end = tk.StringVar(value="16.0")
        for label, variable in (("Start (s)", self.start), ("End (s)", self.end)):
            ttk.Label(controls, text=label).pack(side=tk.LEFT)
            ttk.Entry(controls, textvariable=variable, width=9).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="Measure interval", command=self.measure).pack(side=tk.LEFT)
        ttk.Button(controls, text="Use for startup fit", command=self.use_startup).pack(side=tk.LEFT, padx=6)
        self.result = tk.StringVar(value="No measurement yet.")
        ttk.Label(self, textvariable=self.result, justify=tk.LEFT, padding=8).pack(fill=tk.X)
        actions = ttk.Frame(self, padding=8)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Save per-unit measurement", command=self.save_measurement).pack(side=tk.LEFT)
        ttk.Button(actions, text="Use as bias-update interval", command=self.use_interval).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Clear session bias updates", command=self.clear_intervals).pack(side=tk.LEFT)
        self.intervals = tk.StringVar()
        ttk.Label(self, textvariable=self.intervals, padding=8).pack(fill=tk.X)
        self.points = ttk.Treeview(self, columns=("temperature", "bias", "noise"), show="headings", height=6)
        for name in ("temperature", "bias", "noise"):
            self.points.heading(name, text=name)
            self.points.column(name, width=260)
        self.points.pack(fill=tk.BOTH, expand=True, padx=8)
        footer = ttk.Frame(self, padding=8)
        footer.pack(fill=tk.X)
        ttk.Button(footer, text="Fit / save temperature model", command=self.fit).pack(side=tk.LEFT)
        ttk.Button(footer, text="Delete selected measurement", command=self.delete).pack(side=tk.LEFT, padx=6)
        ttk.Button(footer, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self.refresh()

    def refresh(self):
        self.points.delete(*self.points.get_children())
        for index, item in enumerate(self.store.measurements(self.serial)):
            temperature = "unavailable (legacy)" if item.temperature_c is None else f"{item.temperature_c:.3f} °C"
            self.points.insert("", tk.END, iid=str(index), values=(
                temperature, ", ".join(f"{v:+.5f}" for v in item.bias),
                ", ".join(f"{v:.5f}" for v in item.noise)))
        self.intervals.set(f"Session bias-update intervals: {self.app.gyro_intervals or 'none'}")

    def measurement(self):
        serial = self.app.unit_serial if self.app.unit_serial != DEMO_UNIT else None
        if self.app.session is not self.session or serial != self.serial:
            raise ValueError("The recording/unit changed; close and reopen this calibration dialog")
        return gyrocal.measure(self.app.session, self.app.acc,
                               float(self.start.get()), float(self.end.get()))

    def measure(self):
        try:
            item = self.measurement()
        except ValueError as exc:
            messagebox.showerror("Stationary measurement", str(exc), parent=self)
            return
        self.result.set(
            f"{item.samples} samples; bias XYZ: {item.bias}\nNoise std XYZ: {item.noise}\n"
            f"BMI270 temperature: {item.temperature_c if item.temperature_c is not None else 'unavailable'} °C")

    def save_measurement(self):
        try:
            self.store.add(self.serial, self.measurement())
        except (ValueError, OSError) as exc:
            messagebox.showerror("Save gyro measurement", str(exc), parent=self)
            return
        self.refresh()
        self.result.set("Saved measurement. Temperature model is unchanged until Fit / save is clicked.")

    def use_startup(self):
        try:
            item = self.measurement()
            if item.end_s >= self.app.session.duration_s:
                raise ValueError("Startup fitting needs subsequent samples")
            if any(start < item.end_s for start, _end in self.app.gyro_intervals):
                raise ValueError("Startup window would overlap a marked bias update; clear those intervals first")
        except ValueError as exc:
            messagebox.showerror("Startup fit", str(exc), parent=self)
            return
        self.app.path_settle_var.set(f"{item.start_s:g}")
        self.app.path_rest_var.set(f"{item.end_s:g}")
        self.app._invalidate_path()
        self.result.set("Startup window selected. Ensure the kart was at rest from sample 0 through End.")

    def use_interval(self):
        try:
            item = self.measurement()
            intervals = self.app.gyro_intervals
            if any(item.start_s < end and start < item.end_s for start, end in intervals):
                raise ValueError("Bias-update intervals cannot overlap")
            if item.start_s < float(self.app.path_rest_var.get()):
                raise ValueError("Bias-update interval must start after initial stationary initialization")
            if item.end_s >= self.app.session.duration_s:
                raise ValueError("Bias-update interval needs subsequent samples")
        except ValueError as exc:
            messagebox.showerror("Bias-update interval", str(exc), parent=self)
            return
        intervals.append((item.start_s, item.end_s))
        intervals.sort()
        self.app._invalidate_path()
        self.refresh()

    def clear_intervals(self):
        self.app.gyro_intervals.clear()
        self.app._invalidate_path()
        self.refresh()

    def fit(self):
        try:
            model = self.store.fit(self.serial)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Temperature model", str(exc), parent=self)
            return
        self.app._invalidate_path()
        self.result.set(f"Saved model over {model.temperatures[0]:.2f}–{model.temperatures[-1]:.2f} °C. "
                        "Choose Saved per-unit model in the path tab to apply it.")

    def delete(self):
        selection = self.points.selection()
        if not selection:
            return
        try:
            unit = self.store.units[self.serial]
            del unit["measurements"][int(selection[0])]
            unit.pop("model", None)
            self.store.save()
        except OSError as exc:
            messagebox.showerror("Delete measurement", str(exc), parent=self)
            return
        self.app._invalidate_path()
        self.refresh()
        self.result.set("Measurement deleted; temperature model invalidated. Refit remaining points.")


class CalibrationChecklist(tk.Toplevel):
    """User-confirmed reminders, not device control or automatic verification."""

    def __init__(self, app):
        super().__init__(app)
        self.title("Calibration checklist")
        self.transient(app)
        self.geometry("720x680")
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = ttk.Frame(canvas, padding=12)
        canvas.create_window((0, 0), window=body, anchor=tk.NW)
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        ttk.Label(body, text="Six-face accelerometer calibration", font=("TkDefaultFont", 13, "bold")
                  ).pack(anchor=tk.W)
        ttk.Label(body, wraplength=650, justify=tk.LEFT, text=(
            "Check each item after doing it. This checklist does not control or verify "
            "the device, and it does not calibrate the gyroscope. Use the top USER button, "
            "not the separate reset/bootloader button."
        )).pack(anchor=tk.W, pady=(6, 8))
        ttk.Label(body, wraplength=650, justify=tk.LEFT, foreground="firebrick", text=(
            "Erasing is NOT required if there is enough free space: new sessions append to "
            "existing recordings. If full (-ENOSPC), download and back up anything important "
            "first. To erase ALL stored recordings, hold the USER button for 5 seconds, "
            "then release. Keep power connected while blue blinks (up to a minute); wait "
            "for three quick blue flashes. The device remains stopped. Erase cannot be undone."
        )).pack(anchor=tk.W, pady=(0, 8))

        steps = [
            "Prepare: power on, confirm stopped/blue and no red error, and use a stable, "
            "flat surface. Check Status for free space before recording.",
            "Start a NEW calibration session: briefly press and release the USER button "
            "(do not hold it). Confirm the LED turns green.",
        ]
        steps.extend(
            f"Face {number} of 6: {face}. Let the device settle, then leave it completely "
            "still for at least 5 seconds before turning it."
            for number, face in enumerate((
                "top up", "bottom up", "first side up", "opposite side up",
                "third side up", "remaining opposite side up",
            ), start=1)
        )
        steps.extend([
            "Stop and flush: briefly press and release the USER button again. Confirm "
            "blue/stopped; keep power on. If red appears, inspect Status and retry after "
            "resolving the error.",
            "Download: connect a USB DATA cable while stopped, close other serial terminals, "
            "then use Download. Select the new calibration session in the Session list "
            "(the GUI initially selects the longest session). Download does not erase data.",
            "Fit and save: open 'Calibrate / check from this session', confirm all six "
            "distinct faces were detected, press Fit, enter the unit name and serial, "
            "then Save to calibration file.",
            "Independent check: record another NEW session, resting still for at least "
            "5 seconds in each of 2 or 3 orientations. Stop, download, select that session "
            "and the saved unit calibration, then open Calibrate / check. Each corrected "
            "|g| should be near 1.000 g. Do not validate using the same six-face fit data.",
        ])
        self.checked = []
        self.progress_var = tk.StringVar()
        for number, text in enumerate(steps, start=1):
            checked = tk.BooleanVar(value=False)
            self.checked.append(checked)
            row = ttk.Frame(body)
            row.pack(fill=tk.X, pady=4)
            ttk.Checkbutton(row, text=f"{number}.", variable=checked,
                            command=self._update_progress).pack(side=tk.LEFT, anchor=tk.N)
            ttk.Label(row, text=text, wraplength=590, justify=tk.LEFT).pack(
                side=tk.LEFT, anchor=tk.N)
        footer = ttk.Frame(self, padding=12)
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.progress_var).pack(side=tk.LEFT)
        ttk.Button(footer, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self._update_progress()

    def _update_progress(self):
        completed = sum(item.get() for item in self.checked)
        self.progress_var.set(
            f"{completed} of {len(self.checked)} reminders checked (not device-verified).")


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
