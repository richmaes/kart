"""Idle-to-launch inspection without assuming the current forward-axis sign."""

import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

from . import analysis


class LaunchDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.session = app.session
        self.acc = app.acc.copy()
        self.result = None
        self.title("Inspect idle to first major acceleration")
        self.geometry("1150x850")
        self.transient(app)
        self.grab_set()
        ttk.Label(self, padding=8, wraplength=1100, justify=tk.LEFT, text=(
            "Candidate launch, not proof of motion from rest. Confirm the reference was physically stationary "
            "and the first push was a straight FORWARD launch—not braking, handling, a turn or tilt. "
            "Idle vibration does not prove zero speed. All times use the session plot coordinates.\n"
            "Detection uses calibrated raw acceleration with a separate 0.3 s moving average, "
            "not the display low-pass. The initial gravity reference is fixed, not continuous tilt correction."
        )).pack(fill=tk.X)
        controls = ttk.Frame(self, padding=8)
        controls.pack(fill=tk.X)
        self.variables = {}
        for key, label, default in (
            ("ref_start", "Reference start (s)", app.ref_start.get()),
            ("ref_duration", "Duration (s)", app.ref_dur.get()),
            ("threshold_g", "Threshold (g)", 0.12),
            ("hold_s", "Hold (s)", 0.3),
            ("window_s", "Direction window (s)", 1.0),
        ):
            variable = tk.StringVar(value=str(default))
            self.variables[key] = variable
            ttk.Label(controls, text=label).pack(side=tk.LEFT)
            ttk.Entry(controls, textvariable=variable, width=7).pack(side=tk.LEFT, padx=4)
        ttk.Button(controls, text="Find / refresh", command=self.find).pack(side=tk.LEFT, padx=6)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, padding=8, wraplength=1100,
                  justify=tk.LEFT).pack(fill=tk.X)
        self.figure = Figure(layout="constrained")
        self.axes = self.figure.subplots(3, 1, sharex=True)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        NavigationToolbar2Tk(self.canvas, self).update()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        actions = ttk.Frame(self, padding=8)
        actions.pack(fill=tk.X)
        self.apply_button = ttk.Button(actions, text="Apply suggested forward axis / yaw and reference",
                                       command=self.apply)
        self.apply_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        for variable in self.variables.values():
            variable.trace_add("write", self.invalidate)
        self.find()

    def invalidate(self, *_args):
        self.result = None
        self.apply_button.state(["disabled"])
        self.status.set("Settings changed; click Find / refresh to update the preview.")

    def find(self):
        self.result = None
        self.apply_button.state(["disabled"])
        for ax in self.axes:
            ax.clear()
        try:
            settings = {key: float(variable.get()) for key, variable in self.variables.items()}
            result = analysis.inspect_launch(self.session.t, self.acc, **settings)
            current = analysis.calibrate(self.session.t, self.acc, settings["ref_start"],
                                         settings["ref_duration"], self.app.forward_var.get(),
                                         self.app.yaw_var.get())
        except (ValueError, tk.TclError) as exc:
            self.status.set(f"Cannot inspect launch: {exc}")
            self.canvas.draw_idle()
            return
        self.result, self.settings = result, settings
        t = self.session.t
        start, end = result["time_s"], result["window_end_s"]
        xyz = analysis.smooth((self.acc - result["reference"]) / analysis.G,
                              0.3, analysis.sample_rate(t))
        kart = analysis.smooth(analysis.to_kart_g(self.acc, current), 0.3, analysis.sample_rate(t))
        for index, name in enumerate("XYZ"):
            self.axes[0].plot(t, xyz[:, index], label=f"Device {name} minus idle", lw=1)
        self.axes[1].plot(t, kart[:, 0], label="Current longitudinal", lw=1.2)
        self.axes[1].plot(t, kart[:, 1], label="Current lateral", lw=1.2)
        self.axes[1].plot(t, result["horizontal_g"], label="Horizontal magnitude (detection)", color="black")
        self.axes[1].axhline(settings["threshold_g"], ls="--", color="gray", label="Threshold")
        self.axes[2].plot(t, kart[:, 2], label="Along initial up", lw=1)
        self.axes[2].plot(t, analysis.smooth(np.linalg.norm(self.acc, axis=1) / analysis.G,
                                           0.3, analysis.sample_rate(t)), label="Total |acc|", lw=1)
        self.axes[2].axhline(1, color="gray", ls="--")
        for ax in self.axes:
            ax.axvspan(settings["ref_start"], settings["ref_start"] + settings["ref_duration"],
                       color="gold", alpha=0.25, label="Idle reference")
            ax.axvspan(start, end, color="red", alpha=0.12, label="Direction averaging")
            ax.axvline(start, color="red", lw=1.5, label=f"Candidate onset {start:.2f} s")
            ax.set_ylabel("Acceleration (g)")
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize=7, ncols=3, loc="upper right")
        self.axes[-1].set_xlim(max(t[0], start - 5), min(t[-1], end + 5))
        self.axes[-1].set_xlabel("Session time (s); toolbar zoom/pan can show the full idle period")
        vector = result["vector_g"]
        mask = (t >= start) & (t <= end)
        longitudinal = kart[mask, 0].mean()
        warning = (" WARNING: noisy idle reference." if result["reference_noise_g"] > 0.05 else "")
        self.status.set(
            f"Candidate launch: {start:.3f} s. Mean horizontal device ΔXYZ = "
            f"[{vector[0]:+.3f}, {vector[1]:+.3f}, {vector[2]:+.3f}] g.\n"
            f"Suggested Forward {result['axis']}, yaw {result['yaw_deg']:+.1f}°. "
            f"Current Forward {self.app.forward_var.get()}: mean longitudinal {longitudinal:+.3f} g. "
            f"Reference variation {result['reference_noise_g']:.3f} g.{warning}\n"
            "Suggestion assumes this is a straight forward launch. Red onset is smoothed and approximate; "
            "vertical changes may be tilt, bumps or sensor issues, not just acceleration.")
        self.apply_button.state(["!disabled"])
        self.canvas.draw_idle()

    def apply(self):
        if self.result is None:
            return
        if self.app.session is not self.session or not np.array_equal(self.app.acc, self.acc):
            messagebox.showerror("Launch inspection", "Recording or calibration changed; reopen the tool.", parent=self)
            return
        if not messagebox.askyesno(
            "Confirm forward launch",
            "Was the reference physically stationary and this event a straight forward launch?\n"
            f"Apply Forward {self.result['axis']} and yaw {self.result['yaw_deg']:+.1f}° "
            "plus the inspected reference? No raw data will be changed.", parent=self):
            return
        self.app.forward_var.set(self.result["axis"])
        self.app.yaw_var.set(round(self.result["yaw_deg"], 1))
        self.app.ref_start.set(self.settings["ref_start"])
        self.app.ref_dur.set(self.settings["ref_duration"])
        self.app.update_all()
        self.app.status_var.set(f"Forward direction set from confirmed launch at {self.result['time_s']:.3f} s.")
        self.destroy()
