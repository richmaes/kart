"""Recording container plus CSV load/save in the logger.py CSV format."""

import csv
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

import numpy as np

CSV_HEADER = ("session", "time_us", "ax_m_s2", "ay_m_s2", "az_m_s2")
CSV_HEADER_WITH_GYRO = CSV_HEADER + ("gx_rad_s", "gy_rad_s", "gz_rad_s")
NOMINAL_DT_US = 10_000
GAP_LIMIT_US = 15_000


@dataclass
class Session:
    number: int
    time_us: np.ndarray  # int64, microseconds since session start
    acc: np.ndarray  # (N, 3) float64, m/s^2 in the device frame, including gravity

    @property
    def t(self):
        return self.time_us / 1e6

    @property
    def duration_s(self):
        return float(self.time_us[-1] - self.time_us[0]) / 1e6 if len(self.time_us) else 0.0

    def gaps(self, limit_us=GAP_LIMIT_US):
        """Return [(time_s, step_ms), ...] for steps larger than `limit_us`."""
        dt = np.diff(self.time_us)
        idx = np.nonzero(dt > limit_us)[0]
        return [(self.time_us[i] / 1e6, dt[i] / 1e3) for i in idx]


def sessions_from_arrays(session, time_us, acc):
    """Split flat arrays into Session objects ordered by session number."""
    result = []
    for number in np.unique(session):
        mask = session == number
        result.append(Session(int(number), time_us[mask].astype(np.int64), acc[mask].astype(float)))
    return result


def sessions_from_records(records):
    """Convert decoded logger rows to sessions; gyro columns are not analyzed."""
    if not records:
        return []
    arr = np.asarray([record[:5] for record in records], dtype=np.int64)
    return sessions_from_arrays(arr[:, 0], arr[:, 1], arr[:, 2:5] / 1e6)


def load_csv(path):
    with open(path, newline="", encoding="ascii") as handle:
        header = next(csv.reader(handle))
    if tuple(h.strip() for h in header) not in (CSV_HEADER, CSV_HEADER_WITH_GYRO):
        raise ValueError(f"{path}: unexpected CSV header {header}")
    data = np.genfromtxt(
        path, delimiter=",", skip_header=1, usecols=range(5), ndmin=2
    )
    if data.size == 0:
        return []
    return sessions_from_arrays(
        data[:, 0].astype(np.int64), np.rint(data[:, 1]).astype(np.int64), data[:, 2:5]
    )


def _micro(value):
    value = int(round(value * 1e6))
    return f"{'-' if value < 0 else ''}{abs(value) // 1000000}.{abs(value) % 1000000:06d}"


def save_csv(path, sessions):
    """Write atomically; refuses to overwrite an existing file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite {path}")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".kartlog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="ascii") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_HEADER)
            for s in sessions:
                for t, (x, y, z) in zip(s.time_us, s.acc):
                    writer.writerow((s.number, int(t), _micro(x), _micro(y), _micro(z)))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, path)
    finally:
        os.unlink(temp)
