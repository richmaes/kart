"""Per-unit accelerometer offset/gain calibration from static face orientations.

Model, per device axis i:  measured_i = gain_i * true_i + offset_i  (m/s^2).
Each still segment of a calibration recording has the unit resting on a face, so
the true specific force is +1 g along one device axis and 0 along the other two.
Faces are identified from the data.

With 6+ well-spread orientations (ideally all six faces) offset and gain are a
"sphere fit": every corrected reading must have magnitude 1 g. That uses only the
magnitude, so table tilt or an imperfectly square case affects it only to second
order (~0.3 mg at 1.5 degrees). With fewer orientations it falls back to a linear
"face fit" that assumes the faces are exactly level. Three faces (+X, +Y, +Z up)
is the minimum that solves every axis, but surface tilt then leaks in: about
12 mg of offset and 1.3 % of gain per degree.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path

import numpy as np

from .analysis import G, find_still_segments

DEFAULT_CONFIG = Path(os.environ.get(
    "KARTLOG_CONFIG", Path(__file__).resolve().parent.parent / "calibration.json"))
FACE_TOLERANCE_DEG = 20.0
AXES = "XYZ"


@dataclass
class SensorCal:
    offset: list  # m/s^2 per device axis
    gain: list  # unitless per device axis
    solved: list = field(default_factory=lambda: [True] * 3)
    faces: list = field(default_factory=list)  # e.g. ["+Z", "-Z", "+Y"]
    rms_error_g: float = 0.0
    created: str = ""
    name: str = ""
    method: str = "faces"  # "sphere" (tilt-insensitive) or "faces" (assumes level faces)

    def apply(self, acc):
        return (acc - np.asarray(self.offset)) / np.asarray(self.gain)

    def describe(self):
        parts = []
        for i, a in enumerate(AXES):
            flag = "" if self.solved[i] else "?"
            parts.append(f"{a}: off {self.offset[i] / G * 1000:+.0f} mg, gain {self.gain[i]:.3f}{flag}")
        parts.append(f"method: {self.method}" + ("" if self.method == "sphere"
                                                 else " (level-surface assumed)"))
        return "\n".join(parts)


def face_of(mean, tolerance_deg=FACE_TOLERANCE_DEG):
    """Return e.g. '+Z' for the device axis pointing up, or None if ambiguous."""
    unit = mean / np.linalg.norm(mean)
    i = int(np.argmax(np.abs(unit)))
    if np.degrees(np.arccos(min(1.0, abs(unit[i])))) > tolerance_deg:
        return None
    return ("+" if unit[i] > 0 else "-") + AXES[i]


def face_vector(face):
    v = np.zeros(3)
    v[AXES.index(face[1])] = 1.0 if face[0] == "+" else -1.0
    return v


def segments(t, acc, min_s=1.0):
    """Still segments with their detected face: dicts of start, end, mean, std, n, face."""
    out = []
    for seg in find_still_segments(t, acc, min_s=min_s):
        seg["face"] = face_of(seg["mean"])
        out.append(seg)
    return out


def fit(segs):
    """Fit offset/gain from segments that have a face. Returns (SensorCal, residuals_g).

    residuals_g[k] is |corrected mean| - 1 g for each segment, the calibration check.
    """
    segs = [s for s in segs if s.get("face")]
    if not segs:
        raise ValueError("No segments with a recognised face")
    means = np.array([s["mean"] for s in segs])
    truth = np.array([face_vector(s["face"]) for s in segs]) * G
    offset, gain, solved = np.zeros(3), np.ones(3), [False] * 3
    for i in range(3):
        u, m = truth[:, i], means[:, i]
        if len(np.unique(np.round(u))) >= 2:
            A = np.column_stack([u, np.ones_like(u)])
            (gain[i], offset[i]), *_ = np.linalg.lstsq(A, m, rcond=None)
            solved[i] = True
        elif np.all(u == 0):
            offset[i] = m.mean()  # gain unknown without this axis pointing up or down
        else:
            gain[i] = (m.mean()) / u[0]  # offset unknown; attribute all error to gain
    method = "faces"
    sphere = _sphere_fit(means, gain, offset)
    if sphere is not None:
        gain, offset = sphere
        solved, method = [True] * 3, "sphere"
    cal = SensorCal(offset.tolist(), gain.tolist(), solved,
                    sorted({s["face"] for s in segs}),
                    created=datetime.now().isoformat(timespec="seconds"), method=method)
    residuals = check(cal, segs)
    cal.rms_error_g = float(np.sqrt(np.mean(np.square(residuals))))
    return cal, residuals


def _sphere_fit(means, gain, offset, iterations=25, max_cond=1e3):
    """Gauss-Newton fit of |(m - offset) / gain| = G. Returns (gain, offset) or None.

    Returns None when the orientations do not constrain all six parameters.
    """
    if len(means) < 6:
        return None
    p = np.concatenate([gain, offset])
    for _ in range(iterations):
        g, o = p[:3], p[3:]
        c = (means - o) / g
        norm = np.linalg.norm(c, axis=1)
        r = norm - G
        unit = c / norm[:, None]
        J = np.hstack([-unit * c / g, -unit / g])
        if np.linalg.cond(J) > max_cond:
            return None
        step, *_ = np.linalg.lstsq(J, -r, rcond=None)
        p = p + step
        if np.abs(step).max() < 1e-9:
            break
    if np.any(p[:3] < 0.5) or np.any(p[:3] > 1.5):
        return None
    return p[:3], p[3:]


def check(cal, segs):
    """|corrected mean| - 1 g for each segment."""
    return [float(np.linalg.norm(cal.apply(s["mean"])) / G - 1.0) for s in segs]


class Store:
    """JSON file holding one SensorCal per unit, keyed by USB serial number."""

    def __init__(self, path=DEFAULT_CONFIG):
        self.path = Path(path)
        self.units = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            if raw.get("version") != 1:
                raise ValueError(f"{self.path}: unsupported calibration file version")
            self.units = {k: SensorCal(**v) for k, v in raw.get("units", {}).items()}

    def get(self, serial):
        return self.units.get(serial)

    def put(self, serial, cal):
        self.units[serial] = cal
        self.save()

    def save(self):
        payload = {"version": 1, "units": {k: asdict(v) for k, v in self.units.items()}}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n")
        temp.replace(self.path)


def meta_path(csv_path):
    return Path(csv_path).with_suffix(".meta.json")


def read_meta(csv_path):
    path = meta_path(csv_path)
    return json.loads(path.read_text()) if path.exists() else {}


def write_meta(csv_path, **meta):
    meta_path(csv_path).write_text(json.dumps(meta, indent=2) + "\n")
