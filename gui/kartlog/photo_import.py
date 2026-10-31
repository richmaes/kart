"""Tk photo crop, local OCR, column selection and explicit lap-time review."""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector

from . import laptimes, photo_ocr


class PhotoImportDialog(tk.Toplevel):
    def __init__(self, app, filename):
        image = photo_ocr.open_image(filename)
        super().__init__(app)
        self.app = app
        self.session = app.session
        self.image = image
        self.crop_box = None
        self.proposals = []
        self.results = queue.Queue()
        self.busy = False
        self.title("Import lap times from photo")
        self.geometry("1200x850")
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        ttk.Label(self, text=(
            "1. Rotate upright, then drag around ONE lap table including kart headers. "
            "2. Read selected table locally. 3. Choose kart/column and check every row.\n"
            "For a page break, import the first table, then open/crop the continuation. "
            "Start lap controls numbering; no sensor synchronization is performed."
        ), wraplength=1150, justify=tk.LEFT, padding=8).pack(fill=tk.X)
        controls = ttk.Frame(self, padding=8)
        controls.pack(fill=tk.X)
        self.left_button = ttk.Button(controls, text="Rotate left", command=lambda: self.rotate(90))
        self.left_button.pack(side=tk.LEFT)
        self.right_button = ttk.Button(controls, text="Rotate right", command=lambda: self.rotate(-90))
        self.right_button.pack(side=tk.LEFT, padx=4)
        self.reset_button = ttk.Button(controls, text="Reset crop", command=self.reset_crop)
        self.reset_button.pack(side=tk.LEFT)
        self.read_button = ttk.Button(controls, text="Read selected table", command=self.read)
        self.read_button.pack(side=tk.LEFT, padx=8)
        self.status = tk.StringVar(value="Select a table with its headers, not summary/best-times columns.")
        ttk.Label(self, textvariable=self.status, padding=8, wraplength=1150).pack(fill=tk.X)
        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8)
        picture = ttk.Frame(body)
        body.add(picture, weight=3)
        self.figure = Figure(layout="constrained")
        self.axes = self.figure.add_subplot()
        self.canvas = FigureCanvasTkAgg(self.figure, master=picture)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.selector = RectangleSelector(self.axes, self.select_crop, useblit=False,
                                           button=[1], minspanx=10, minspany=10, spancoords="data")
        review = ttk.Frame(body, padding=8)
        body.add(review, weight=1)
        ttk.Label(review, text="Detected column / proposed kart header").pack(anchor=tk.W)
        self.column_box = ttk.Combobox(review, state="readonly", width=35)
        self.column_box.pack(fill=tk.X, pady=4)
        self.column_box.bind("<<ComboboxSelected>>",
                             lambda _event: self.select_column(suggest_start=True))
        start = ttk.Frame(review)
        start.pack(fill=tk.X)
        ttk.Label(start, text="Start lap").pack(side=tk.LEFT)
        first_empty = next((index + 1 for index, variable in enumerate(app.external_time_vars)
                            if not variable.get().strip()), 1)
        self.start_var = tk.StringVar(value=str(first_empty))
        ttk.Entry(start, textvariable=self.start_var, width=5).pack(side=tk.LEFT, padx=6)
        ttk.Button(start, text="Renumber preview", command=self.select_column).pack(side=tk.LEFT)
        ttk.Label(review, text=(
            "Editable preview: lap number followed by time.\n"
            "Check row count and order against the photo.\n"
            "OCR errors must be corrected before import."
        ), justify=tk.LEFT).pack(anchor=tk.W, pady=8)
        self.preview = tk.Text(review, width=32, height=26, wrap=tk.NONE)
        scrollbar = ttk.Scrollbar(review, orient=tk.VERTICAL, command=self.preview.yview)
        self.preview.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.preview.pack(fill=tk.BOTH, expand=True)
        actions = ttk.Frame(self, padding=8)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Import reviewed times into session entries",
                   command=self.apply).pack(side=tk.LEFT)
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        self.draw()

    def draw(self):
        self.axes.clear()
        self.axes.imshow(self.image)
        self.axes.set_axis_off()
        self.selector.set_visible(False)
        self.canvas.draw_idle()

    def rotate(self, angle):
        if self.busy:
            return
        self.image = self.image.rotate(angle, expand=True)
        self.reset_crop()
        self.draw()

    def reset_crop(self):
        if self.busy:
            return
        self.crop_box = None
        self.proposals = []
        self.column_box["values"] = []
        self.column_box.set("")
        self.preview.delete("1.0", tk.END)
        self.selector.set_visible(False)
        self.canvas.draw_idle()
        self.status.set("Drag around one table including its kart headers.")

    def select_crop(self, first, last):
        if self.busy or first.xdata is None or last.xdata is None:
            return
        x1, x2 = sorted((first.xdata, last.xdata))
        y1, y2 = sorted((first.ydata, last.ydata))
        self.crop_box = (max(0, int(x1)), max(0, int(y1)),
                         min(self.image.width, int(x2)), min(self.image.height, int(y2)))
        self.proposals = []
        self.column_box["values"] = []
        self.column_box.set("")
        self.preview.delete("1.0", tk.END)
        self.status.set("Crop selected. Click Read selected table; include all rows and kart headers.")

    def read(self):
        if self.busy:
            return
        if self.crop_box is None:
            messagebox.showerror("Photo OCR", "Drag around one lap table first.", parent=self)
            return
        image = self.image.crop(self.crop_box)
        self.busy = True
        for button in (self.read_button, self.left_button, self.right_button, self.reset_button):
            button.state(["disabled"])
        self.proposals = []
        self.preview.delete("1.0", tk.END)
        self.column_box.set("")
        self.column_box["values"] = []
        self.status.set("Reading locally; photo stays on this computer. This may take a minute.")

        def work():
            try:
                self.results.put(("ok", photo_ocr.columns(photo_ocr.recognize(image))))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.results.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.poll)

    def poll(self):
        try:
            kind, result = self.results.get_nowait()
        except queue.Empty:
            self.after(100, self.poll)
            return
        self.busy = False
        for button in (self.read_button, self.left_button, self.right_button, self.reset_button):
            button.state(["!disabled"])
        if kind == "error":
            self.status.set(f"Cannot read table: {result}")
            messagebox.showerror("Photo OCR", result, parent=self)
            return
        self.proposals = result
        self.column_box["values"] = [column.label for column in self.proposals]
        self.status.set("Choose a kart/column; header detection is a suggestion, not verification.")
        if len(result) == 1:
            self.column_box.current(0)
            self.select_column(suggest_start=True)

    def select_column(self, suggest_start=False):
        index = self.column_box.current()
        if index < 0 or not self.proposals:
            return
        column = self.proposals[index]
        if suggest_start:
            starts = [number - index for index, number in enumerate(column.lap_numbers)
                      if number is not None]
            if starts and len(set(starts)) == 1 and starts[0] >= 1:
                self.start_var.set(str(starts[0]))
        try:
            start = int(self.start_var.get())
            if not 1 <= start <= laptimes.MAX_LAPS or start + len(column.words) - 1 > laptimes.MAX_LAPS:
                raise ValueError("Selected rows must fit within laps 1–40")
        except ValueError as exc:
            messagebox.showerror("Start lap", str(exc), parent=self)
            return
        self.preview.delete("1.0", tk.END)
        self.preview.insert("1.0", "\n".join(
            f"{start + index} {word.text}" for index, word in enumerate(column.words)))
        uncertain = [start + index for index, word in enumerate(column.words)
                     if word.confidence < 0.8 or not photo_ocr.is_time(word.text)]
        self.status.set(
            f"{column.label}: numbered from lap {start}. Flagged OCR laps: {uncertain or 'none'}. "
            "Verify row count, lap numbers and EVERY value; missing OCR rows cannot be assumed absent laps.")

    def apply(self):
        if self.busy:
            return
        if self.app.session is not self.session:
            messagebox.showerror("Photo import", "Session changed; reopen the photo for the correct session.", parent=self)
            return
        try:
            rows = photo_ocr.reviewed_rows(self.preview.get("1.0", tk.END))
            current = [variable.get() for variable in self.app.external_time_vars]
            merged = photo_ocr.merge_entries(current, rows)
        except ValueError as exc:
            messagebox.showerror("Review lap times", str(exc), parent=self)
            return
        overwritten = [number for number, value in rows
                       if current[number - 1].strip()
                       and current[number - 1].strip() != laptimes.format_time(value)]
        note = f"Replace existing entries for laps {overwritten}?\n" if overwritten else ""
        if not messagebox.askyesno(
            "Confirm reviewed photo times",
            note + f"Import {len(rows)} reviewed times into session {self.session.number}, "
            f"laps {rows[0][0]}–{rows[-1][0]}?\n"
            "Check that no printed rows were missed. Entries are not saved until you click Save session lap times.",
            parent=self,
        ):
            return
        self.app._loading_external_times = True
        try:
            for variable, value in zip(self.app.external_time_vars, merged):
                variable.set(value)
        finally:
            self.app._loading_external_times = False
        self.app._external_times_edited()
        self.destroy()
