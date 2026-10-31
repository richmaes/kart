"""Orientation calibration and kart-frame analysis.

The accelerometer measures specific force: at rest it reads +1 g pointing *up*.
A reference window where the kart is stationary defines "up". The forward
direction is a chosen device axis projected onto the horizontal plane, optionally
rotated by a yaw trim. The kart frame is right-handed (ISO 8855 style):

    longitudinal  +forward  (positive = accelerating, negative = braking)
    lateral       +left     (positive = turning left, negative = turning right)
    vertical      +up       (reads 1 g on level ground at rest)
"""

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt


def vibration_filter(t, acc, cutoff_hz=0.0):
    """Offline fourth-order Butterworth, forward/backward; never bridge >15 ms gaps."""
    t = np.asarray(t, dtype=float)
    acc = np.asarray(acc, dtype=float)
    if not np.isfinite(cutoff_hz) or cutoff_hz < 0:
        raise ValueError("Vibration cutoff must be finite and non-negative (0 means Off)")
    if cutoff_hz == 0:
        return acc.copy()
    if (t.ndim != 1 or acc.shape != (len(t), 3) or len(t) < 2
            or not np.isfinite(t).all() or not np.isfinite(acc).all()
            or np.any(np.diff(t) <= 0)):
        raise ValueError("Vibration filter needs finite XYZ samples and increasing timestamps")
    result = np.empty_like(acc)
    boundaries = np.r_[0, np.flatnonzero(np.diff(t) > 0.0150001) + 1, len(t)]
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        times = t[start:end]
        if len(times) < 16:
            raise ValueError("Vibration filter needs at least 16 samples in each gap-free segment")
        rate = sample_rate(times)
        if cutoff_hz >= rate / 2:
            raise ValueError(f"Vibration cutoff must be below Nyquist ({rate / 2:.2f} Hz)")
        # Resample timestamp jitter for the fixed-rate filter, retaining original output times.
        grid = np.linspace(times[0], times[-1], len(times))
        rate = (len(grid) - 1) / (grid[-1] - grid[0])
        if cutoff_hz >= rate / 2:
            raise ValueError(f"Vibration cutoff must be below Nyquist ({rate / 2:.2f} Hz)")
        uniform = np.column_stack([np.interp(grid, times, axis) for axis in acc[start:end].T])
        filtered = sosfiltfilt(butter(4, cutoff_hz, fs=rate, output="sos"), uniform, axis=0)
        result[start:end] = np.column_stack([np.interp(times, grid, axis) for axis in filtered.T])
    return result

G = 9.80665
SAMPLE_RATE_HZ = 100

DEVICE_AXES = {
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}


@dataclass
class Calibration:
    up: np.ndarray
    forward: np.ndarray
    left: np.ndarray
    ref_mean: np.ndarray  # m/s^2, device frame
    ref_std: np.ndarray  # m/s^2, per device axis
    samples: int
    scale: float = 1.0  # multiplier applied to readings (1/|ref| when normalizing)

    @property
    def rotation(self):
        """Rows are kart axes in device coordinates: kart = rotation @ device."""
        return np.vstack([self.forward, self.left, self.up])

    @property
    def magnitude_g(self):
        return float(np.linalg.norm(self.ref_mean) / G)

    @property
    def noise_g(self):
        return float(np.linalg.norm(self.ref_std) / G)

    @property
    def tilt_deg(self):
        """Angle between the device +Z axis and true up."""
        return float(np.degrees(np.arccos(np.clip(self.up[2], -1.0, 1.0))))


def window_mask(t, start, duration):
    return (t >= start) & (t <= start + duration)


def sample_rate(t):
    """Measured sample rate (Hz) from timestamps in seconds."""
    if len(t) < 2:
        return SAMPLE_RATE_HZ
    return float(1.0 / np.median(np.diff(t)))


def find_still_segments(t, acc, max_std_g=0.02, min_s=0.3, window_s=0.25):
    """Stationary stretches: rolling vector std (all axes) below `max_std_g`.

    Uses every axis, not |a|, so that tilting without translation also counts as motion.
    Returns dicts with start, end (s), mean, std (m/s^2, per axis) and n samples.
    """
    rate = sample_rate(t)
    g = acc / G
    n = max(2, int(window_s * rate))
    if len(g) < n:
        return []
    mean = smooth(g, window_s, rate)
    var = smooth(g ** 2, window_s, rate) - mean ** 2
    std = np.sqrt(np.maximum(var.sum(axis=1), 0.0))
    quiet = std < max_std_g
    # Trim half a window from each motion edge so it is not averaged in.
    half = n // 2
    found = []
    run_start = None
    for i, q in enumerate(np.append(quiet, False)):
        if q and run_start is None:
            run_start = i
        elif not q and run_start is not None:
            a = run_start + (half if run_start > 0 else 0)
            b = i - 1 - (half if i < len(quiet) else 0)
            if b > a and t[b] - t[a] >= min_s:
                seg = acc[a:b + 1]
                found.append({"start": float(t[a]), "end": float(t[b]), "mean": seg.mean(axis=0),
                              "std": seg.std(axis=0), "n": b - a + 1})
            run_start = None
    return found


def find_rest(t, acc, max_std_g=0.02, min_s=0.3, max_s=3.0):
    """First stationary stretch as (start_s, duration_s), capped at `max_s`."""
    segs = find_still_segments(t, acc, max_std_g, min_s)
    if not segs:
        raise ValueError("No stationary stretch found")
    return segs[0]["start"], min(segs[0]["end"] - segs[0]["start"], max_s)


def calibrate(t, acc, start, duration, forward_axis="+X", yaw_deg=0.0, normalize=False):
    mask = window_mask(t, start, duration)
    n = int(mask.sum())
    if n < 2:
        raise ValueError("Reference window contains fewer than 2 samples")
    ref = acc[mask]
    mean = ref.mean(axis=0)
    norm = np.linalg.norm(mean)
    if norm < 0.2 * G:
        raise ValueError("Reference window has no clear gravity vector")
    up = mean / norm

    d = np.asarray(DEVICE_AXES[forward_axis])
    f = d - np.dot(d, up) * up
    if np.linalg.norm(f) < 0.3:
        raise ValueError(f"Forward axis {forward_axis} is nearly vertical; choose another")
    f /= np.linalg.norm(f)
    yaw = np.radians(yaw_deg)
    f = f * np.cos(yaw) + np.cross(up, f) * np.sin(yaw)
    left = np.cross(up, f)
    scale = G / norm if normalize else 1.0
    return Calibration(up, f, left, mean, ref.std(axis=0), n, scale)


def to_kart_g(acc, cal):
    """(N, 3) device m/s^2 -> (N, 3) kart frame in g: long, lat, vert."""
    return acc @ cal.rotation.T * (cal.scale / G)


def smooth(x, window_s, rate=SAMPLE_RATE_HZ):
    """Centered moving average along axis 0; edges use the available samples."""
    n = int(round(window_s * rate))
    if n <= 1 or len(x) == 0:
        return x
    n = min(n, len(x))
    kernel = np.ones(n)
    ones = np.convolve(np.ones(len(x)), kernel, mode="same")
    if x.ndim == 1:
        return np.convolve(x, kernel, mode="same") / ones
    return np.column_stack([np.convolve(c, kernel, mode="same") / ones for c in x.T])


def estimate_launch_yaw(t, kart_g, after_s, threshold_g=0.12, hold_s=0.3, window_s=1.0, rate=None):
    """Estimate yaw correction (degrees) from the first sustained acceleration.

    Assumes the first strong horizontal push after `after_s` is the kart pulling
    away in a straight line, so its direction is "forward".
    Returns (yaw_deg, launch_time_s) or raises ValueError.
    """
    rate = rate or sample_rate(t)
    horiz = smooth(kart_g[:, :2], 0.3, rate)
    mag = np.hypot(horiz[:, 0], horiz[:, 1])
    above = (mag > threshold_g) & (t > after_s)
    need = max(1, int(hold_s * rate))
    run = 0
    for i, flag in enumerate(above):
        run = run + 1 if flag else 0
        if run >= need:
            start = i - need + 1
            seg = (t >= t[start]) & (t <= t[start] + window_s)
            v = horiz[seg].mean(axis=0)
            return float(np.degrees(np.arctan2(v[1], v[0]))), float(t[start])
    raise ValueError("No sustained acceleration found after the reference window")


def summarize(t, kart_g):
    """Peak values (g) over the given samples. kart_g should already be smoothed."""
    if len(t) == 0:
        return {}
    lon, lat, vert = kart_g.T
    combined = np.hypot(lon, lat)

    def at(arr, fn):
        i = int(fn(arr))
        return float(arr[i]), float(t[i])

    return {
        "max_accel": at(lon, np.argmax),
        "max_brake": at(lon, np.argmin),
        "max_left": at(lat, np.argmax),
        "max_right": at(lat, np.argmin),
        "max_combined": at(combined, np.argmax),
        "vert_min": at(vert, np.argmin),
        "vert_max": at(vert, np.argmax),
    }
