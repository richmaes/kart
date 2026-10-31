"""Manual lap boundaries and lap-to-lap signal comparisons."""

from dataclasses import dataclass

import numpy as np

from .data import GAP_LIMIT_US, Session


@dataclass
class Lap:
    start_us: int
    end_us: int
    include: bool = True

    @property
    def duration_s(self):
        return (self.end_us - self.start_us) / 1e6


def validate(session, lap, others=()):
    if len(session.time_us) < 2:
        raise ValueError("A session needs at least two samples to define a lap")
    if np.any(np.diff(session.time_us) <= 0):
        raise ValueError("Session timestamps must be strictly increasing")
    if lap.start_us < session.time_us[0] or lap.end_us > session.time_us[-1]:
        raise ValueError("Lap boundaries must be within the session")
    if lap.end_us <= lap.start_us:
        raise ValueError("Lap end must be after its start")
    start = np.searchsorted(session.time_us, lap.start_us, side="left")
    end_side = "right" if lap.end_us == session.time_us[-1] else "left"
    end = np.searchsorted(session.time_us, lap.end_us, side=end_side)
    if end - start < 2:
        raise ValueError("Lap boundaries must contain at least two samples")
    for other in others:
        if lap.start_us < other.end_us and other.start_us < lap.end_us:
            raise ValueError("Lap boundaries cannot overlap another lap")


def samples(session, lap, values):
    """Return elapsed seconds and values in [start, end), including the final sample."""
    start = int(np.searchsorted(session.time_us, lap.start_us, side="left"))
    end = int(np.searchsorted(session.time_us, lap.end_us, side="left"))
    if lap.end_us == session.time_us[-1]:
        end += 1
    return (session.time_us[start:end] - lap.start_us) / 1e6, values[start:end]


def flags(session, lap, all_laps):
    result = []
    start_t = session.time_us[0]
    end_t = session.time_us[-1]
    typical_step = float(np.median(np.diff(session.time_us)))
    edge_tolerance = max(typical_step * 2, 1)
    if lap.start_us - start_t <= edge_tolerance or end_t - lap.end_us <= edge_tolerance:
        result.append("recording edge")

    first = int(np.searchsorted(session.time_us, lap.start_us, side="left"))
    last = int(np.searchsorted(session.time_us, lap.end_us, side="left"))
    if lap.end_us == end_t:
        last += 1
    gaps = np.diff(session.time_us[first:last])
    gap_count = int(np.count_nonzero(gaps > GAP_LIMIT_US))
    if gap_count:
        result.append(f"{gap_count} gap{'s' if gap_count != 1 else ''} >15 ms")

    if len(all_laps) >= 3:
        median = float(np.median([item.duration_s for item in all_laps]))
        if median > 0 and abs(lap.duration_s - median) / median > 0.2:
            result.append("duration >20% from median")
    return result


def comparison(session, values, selected, mode="elapsed", points=201):
    """Build individual curves and a median/IQR template for included laps."""
    if mode not in ("elapsed", "progress"):
        raise ValueError(f"Unknown lap comparison mode: {mode}")
    curves = []
    for number, lap in selected:
        x, y = samples(session, lap, values)
        if len(x) < 2:
            continue
        axis = x if mode == "elapsed" else x / lap.duration_s
        curves.append((number, axis, y))
    if not curves:
        raise ValueError("Select at least one lap with two or more samples")

    if mode == "progress":
        template_x = np.linspace(0.0, 1.0, points)
        aligned = [
            np.column_stack([
                np.interp(template_x, x, y[:, column])
                for column in range(y.shape[1])
            ])
            for _, x, y in curves
        ]
    else:
        common_start = max(float(x[0]) for _, x, _ in curves)
        common_end = min(float(x[-1]) for _, x, _ in curves)
        if common_end <= common_start:
            raise ValueError("Included laps have no overlapping elapsed-time range")
        template_x = np.linspace(common_start, common_end, points)
        aligned = [
            np.column_stack([
                np.interp(template_x, x, y[:, column])
                for column in range(y.shape[1])
            ])
            for _, x, y in curves
        ]

    stack = np.stack(aligned)
    return {
        "curves": curves,
        "x": template_x,
        "median": np.median(stack, axis=0),
        "low": np.percentile(stack, 25, axis=0),
        "high": np.percentile(stack, 75, axis=0),
    }
