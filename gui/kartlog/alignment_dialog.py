"""Reviewable, background lap-time alignment for Tk."""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

from . import alignment, analysis, laptimes


class AlignmentDialog(tk.Toplevel):
    def __init__(self, app, values):
        super().__init__(app)
        self.app = app
        self.session = app.session
        self.values = list(values)
        self.acc = (app.filtered_acc if app.filtered_acc is not None else app.acc).copy()
        self.results = queue.Queue()
        self.cancel_event = threading.Event()
        self.candidates = []
        self.busy = False
        self.poll_id = None
        self.title("Auto-align track lap times")
        self.geometry("1150x900")
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        ttk.Label(self, padding=8, wraplength=1100, justify=tk.LEFT, text=(
            "Search for repeating gyro/acceleration patterns using the entered lap durations. "
            "No position integration or clock stretching. Scores are not accuracy probabilities.\n"
            "Anchor time is the START crossing of the specified timing lap, in the same seconds "
            "shown on the session time-series plot. Leave blank to search all possible overlaps. "
            "Identical laps cannot uniquely locate the timing line."
        )).pack(fill=tk.X)
        controls = ttk.Frame(self, padding=8)
        controls.pack(fill=tk.X)
        self.anchor = tk.StringVar()
        self.anchor_lap = tk.StringVar(value="1")
        self.radius = tk.StringVar(value="2")
        for label, variable in (("Approx. crossing (s)", self.anchor),
                                ("Timing lap", self.anchor_lap), ("Search ± (s)", self.radius)):
            ttk.Label(controls, text=label).pack(side=tk.LEFT)
            ttk.Entry(controls, textvariable=variable, width=9).pack(side=tk.LEFT, padx=5)
        self.search_button = ttk.Button(controls, text="Find candidates", command=self.find)
        self.search_button.pack(side=tk.LEFT, padx=8)
        self.status = tk.StringVar(value="Enter an optional anchor, then Find candidates.")
        ttk.Label(self, textvariable=self.status, padding=8, wraplength=1100,
                  justify=tk.LEFT).pack(fill=tk.X)
        self.tree = ttk.Treeview(self, columns=("offset", "score", "similarity", "laps"),
                                 show="headings", height=5)
        for column, label in (("offset", "Timing lap 1 start (s)"), ("score", "Match score"),
                              ("similarity", "Pattern similarity"), ("laps", "Usable complete laps")):
            self.tree.heading(column, text=label)
            self.tree.column(column, anchor=tk.CENTER, width=200)
        self.tree.pack(fill=tk.X, padx=8)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        adjust = ttk.Frame(self, padding=8)
        adjust.pack(fill=tk.X)
        self.offset = tk.StringVar()
        ttk.Label(adjust, text="Adjust timing lap 1 start (s; may be negative)").pack(side=tk.LEFT)
        ttk.Spinbox(adjust, textvariable=self.offset, from_=-1e6, to=1e6,
                    increment=0.01, width=13).pack(side=tk.LEFT, padx=6)
        ttk.Button(adjust, text="Preview offset", command=self.preview).pack(side=tk.LEFT)
        self.apply_button = ttk.Button(adjust, text="Apply confirmed boundaries", command=self.apply)
        self.apply_button.pack(side=tk.RIGHT)
        ttk.Button(adjust, text="Cancel", command=self.close).pack(side=tk.RIGHT, padx=8)
        self.apply_button.state(["disabled"])
        self.preview_status = tk.StringVar()
        ttk.Label(self, textvariable=self.preview_status, padding=8, wraplength=1100).pack(fill=tk.X)
        self.figure = Figure(layout="constrained")
        self.axes = self.figure.subplots(3, 1)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        NavigationToolbar2Tk(self.canvas, self).update()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.offset.trace_add("write", lambda *_args: self.apply_button.state(["disabled"]))
        self.previewed_offset = None

    def close(self):
        self.cancel_event.set()
        if self.poll_id is not None:
            self.after_cancel(self.poll_id)
        self.destroy()

    def find(self):
        if self.busy:
            return
        try:
            anchor = float(self.anchor.get()) if self.anchor.get().strip() else None
            lap = int(self.anchor_lap.get())
            radius = float(self.radius.get())
        except ValueError:
            messagebox.showerror("Auto alignment", "Enter numeric anchor settings.", parent=self)
            return
        self.busy = True
        self.search_button.state(["disabled"])
        self.apply_button.state(["disabled"])
        self.tree.delete(*self.tree.get_children())
        self.candidates = []
        self.status.set("Searching possible timing offsets...")

        def work():
            try:
                result = alignment.search(self.session, self.values, self.acc, anchor, lap, radius,
                                          self.cancel_event)
            except (ValueError, ArithmeticError) as exc:
                self.results.put(exc)
            else:
                self.results.put(result)

        threading.Thread(target=work, daemon=True).start()
        self.poll_id = self.after(50, self.poll)

    def poll(self):
        self.poll_id = None
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.poll_id = self.after(50, self.poll)
            return
        self.busy = False
        self.search_button.state(["!disabled"])
        if isinstance(result, Exception):
            self.status.set(f"Alignment failed: {result}")
            messagebox.showerror("Auto alignment", str(result), parent=self)
            return
        self.candidates = result.candidates
        for index, candidate in enumerate(self.candidates):
            self.tree.insert("", tk.END, iid=str(index), values=(
                f"{candidate.offset_s:.3f}", f"{candidate.score:.3f}",
                f"{candidate.similarity:.3f}", candidate.matched_laps))
        self.status.set(f"Local refinement step about {result.resolution_s:.3f} s "
                        "(not timing accuracy).\n" + "\n".join(result.warnings))
        self.tree.selection_set("0")
        self.select()

    def select(self, _event=None):
        selected = self.tree.selection()
        if selected:
            self.offset.set(f"{self.candidates[int(selected[0])].offset_s:.6f}")
            self.preview()

    def preview(self):
        self.apply_button.state(["disabled"])
        if self.busy:
            return
        try:
            offset = float(self.offset.get())
            complete, partial = alignment.boundaries(self.session, self.values, offset)
        except ValueError as exc:
            self.preview_status.set(f"Invalid preview: {exc}")
            return
        self.previewed_offset = offset
        excluded = [lap.external_number for lap in complete if not lap.include]
        self.preview_status.set(
            f"Complete timing laps: {', '.join(str(lap.external_number) for lap in complete)}. "
            f"Partial laps omitted: {partial or 'none'}. "
            f"Gap/missing-gyro laps excluded from overlays: {excluded or 'none'}. "
            "Applying replaces current comparison boundaries only after confirmation.")
        for ax in self.axes:
            ax.clear()
        gyro = self.session.gyro
        finite = np.isfinite(gyro).all(axis=1)
        mean = gyro[finite].mean(axis=0)
        for index, axis in enumerate("XYZ"):
            self.axes[0].plot(self.session.t, gyro[:, index] - mean[index],
                              lw=0.6, label=f"Native gyro {axis}")
        for lap in complete:
            start, end = lap.start_us / 1e6, lap.end_us / 1e6
            self.axes[0].axvspan(start, end, color="tab:blue", alpha=0.06)
            self.axes[0].axvline(start, color="black", lw=0.7)
            self.axes[0].text(start, 1, str(lap.external_number),
                              transform=self.axes[0].get_xaxis_transform(), fontsize=8)
        self.axes[0].set_xlabel("Session plotted time (s)")
        self.axes[0].set_ylabel("Gyro (rad/s)\nmean removed")
        self.axes[0].legend(fontsize=8, ncols=3)
        for ax, signal, unit, name in (
            (self.axes[1], gyro, "rad/s", "gyro"),
            (self.axes[2], self.acc / analysis.G, "g", "acceleration"),
        ):
            component = int(np.nanargmax(np.nanstd(signal, axis=0)))
            for lap in complete:
                if not lap.include:
                    continue
                mask = ((self.session.time_us >= lap.start_us)
                        & (self.session.time_us < lap.end_us))
                x = (self.session.time_us[mask] - lap.start_us) / (lap.end_us - lap.start_us)
                y = signal[mask, component]
                ax.plot(x * 100, y - np.nanmean(y), lw=0.8, alpha=0.7,
                        label=f"Lap {lap.external_number}")
            ax.set_xlabel("Normalized lap progress (%)")
            ax.set_ylabel(f"Native {name} {'XYZ'[component]}\nmean removed ({unit})")
            ax.grid(True, alpha=0.3)
        self.axes[1].legend(fontsize=7, ncols=6)
        self.canvas.draw_idle()
        self.apply_button.state(["!disabled"])

    def apply(self):
        if self.busy:
            return
        try:
            values = laptimes.parse_entries([v.get() for v in self.app.external_time_vars])
            offset = float(self.offset.get())
            if (self.app.session is not self.session or values != self.values
                    or offset != self.previewed_offset):
                raise ValueError("Session, durations or offset changed; preview again before applying")
            complete, _partial = alignment.boundaries(self.session, values, offset)
            if not messagebox.askyesno(
                "Confirm lap alignment",
                f"Replace the current lap comparison boundaries with {len(complete)} complete timing laps?\n"
                "Save the entered durations and this confirmed offset beside the recording?\n"
                "The match does not prove the physical timing-line location.", parent=self):
                return
            self.app.apply_alignment(values, offset)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Apply alignment", str(exc), parent=self)
            return
        self.close()
