"""Recording container plus CSV load/save in the logger.py CSV format."""

import csv
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

import numpy as np

CSV_HEADER = ("session", "time_us", "ax_m_s2", "ay_m_s2", "az_m_s2")
CSV_HEADER_WITH_GYRO = CSV_HEADER + ("gx_rad_s", "gy_rad_s", "gz_rad_s")
CSV_HEADER_WITH_TEMPERATURE = CSV_HEADER_WITH_GYRO + ("temperature_c",)
NOMINAL_DT_US = 10_000
GAP_LIMIT_US = 15_000


@dataclass
class Session:
    number: int
    time_us: np.ndarray  # int64, microseconds since session start
    acc: np.ndarray  # (N, 3) float64, m/s^2 in the device frame, including gravity
    gyro: np.ndarray | None = None  # (N, 3) rad/s; NaN rows denote legacy/missing gyro
    temperature: np.ndarray | None = None  # (N,) BMI270 die Celsius; NaN for missing

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


def sessions_from_arrays(session, time_us, acc, gyro=None, temperature=None):
    """Split flat arrays into Session objects ordered by session number."""
    result = []
    for number in np.unique(session):
        mask = session == number
        result.append(Session(int(number), time_us[mask].astype(np.int64), acc[mask].astype(float),
                              None if gyro is None else gyro[mask].astype(float),
                              None if temperature is None else temperature[mask].astype(float)))
    return result


def sessions_from_records(records):
    """Convert decoded logger rows to sessions, preserving optional gyro samples."""
    if not records:
        return []
    arr = np.asarray([record[:5] for record in records], dtype=np.int64)
    gyro = np.asarray([
        [np.nan if value is None else value / 1e6 for value in record[5:8]]
        if len(record) >= 8 else [np.nan] * 3 for record in records
    ])
    temperature = np.asarray([record[8] / 1e6 if len(record) == 9 and record[8] is not None else np.nan
                              for record in records])
    return sessions_from_arrays(arr[:, 0], arr[:, 1], arr[:, 2:5] / 1e6,
                                gyro if np.isfinite(gyro).any() else None,
                                temperature if np.isfinite(temperature).any() else None)


def load_csv(path):
    with open(path, newline="", encoding="ascii") as handle:
        header = next(csv.reader(handle))
    if tuple(h.strip() for h in header) not in (
        CSV_HEADER, CSV_HEADER_WITH_GYRO, CSV_HEADER_WITH_TEMPERATURE
    ):
        raise ValueError(f"{path}: unexpected CSV header {header}")
    columns = len(header)
    data = np.genfromtxt(path, delimiter=",", skip_header=1, usecols=range(columns), ndmin=2)
    if data.size == 0:
        return []
    return sessions_from_arrays(
        data[:, 0].astype(np.int64), np.rint(data[:, 1]).astype(np.int64), data[:, 2:5],
        data[:, 5:8] if columns >= 8 else None,
        data[:, 8] if columns == 9 else None,
    )


def _micro(value):
    value = int(round(value * 1e6))
    return f"{'-' if value < 0 else ''}{abs(value) // 1000000}.{abs(value) % 1000000:06d}"


def save_csv(path, sessions):
    """Write atomically; refuses to overwrite an existing file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite {path}")
    for session in sessions:
        if session.temperature is not None:
            if session.temperature.shape != (len(session.time_us),) or np.isinf(session.temperature).any():
                raise ValueError("Temperature must match timestamps and cannot contain infinities")
        if session.gyro is not None:
            if session.gyro.shape != (len(session.time_us), 3):
                raise ValueError("Gyroscope samples must match timestamps and contain three axes")
            if np.isinf(session.gyro).any():
                raise ValueError("Cannot save infinite gyroscope values")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".kartlog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="ascii") as handle:
            writer = csv.writer(handle)
            with_temperature = any(s.temperature is not None for s in sessions)
            with_gyro = with_temperature or any(s.gyro is not None for s in sessions)
            writer.writerow(CSV_HEADER_WITH_TEMPERATURE if with_temperature else
                            CSV_HEADER_WITH_GYRO if with_gyro else CSV_HEADER)
            for s in sessions:
                for index, (t, (x, y, z)) in enumerate(zip(s.time_us, s.acc)):
                    row = [s.number, int(t), _micro(x), _micro(y), _micro(z)]
                    if with_gyro:
                        row.extend([""] * 3 if s.gyro is None else [
                            _micro(value) if np.isfinite(value) else "" for value in s.gyro[index]
                        ])
                    if with_temperature:
                        temperature = np.nan if s.temperature is None else s.temperature[index]
                        row.append(_micro(temperature) if np.isfinite(temperature) else "")
                    writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, path)
    finally:
        os.unlink(temp)
