"""Explicit stationary gyro measurements and bounded temperature compensation."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

import numpy as np

from .analysis import G
from .data import GAP_LIMIT_US

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "gyro-calibration.json"


@dataclass
class Measurement:
    start_s: float
    end_s: float
    bias: list[float]
    noise: list[float]
    samples: int
    temperature_c: float | None


def measure(session, acc, start_s, end_s):
    """Times are relative to the first sample; the user must assert physical rest."""
    if not np.isfinite([start_s, end_s]).all() or end_s - start_s < 10:
        raise ValueError("Mark at least 10 seconds of known physical rest")
    if len(session.time_us) < 3:
        raise ValueError("Need at least three samples")
    if np.asarray(acc).shape != (len(session.time_us), 3):
        raise ValueError("Acceleration samples must match timestamps")
    elapsed = (session.time_us - session.time_us[0]) / 1e6
    if start_s < 0 or end_s > elapsed[-1]:
        raise ValueError("Stationary interval must be within the recording")
    if session.gyro is None:
        raise ValueError("This recording has no gyro samples")
    if session.gyro.shape != (len(session.time_us), 3):
        raise ValueError("Gyro samples must match timestamps")
    mask = (elapsed >= start_s) & (elapsed <= end_s)
    time = session.time_us[mask]
    if len(time) < 3 or np.any(np.diff(time) <= 0) or np.any(np.diff(time) > GAP_LIMIT_US):
        raise ValueError("Stationary interval has too few samples or invalid timing/gaps")
    if time[-1] - time[0] < 10_000_000:
        raise ValueError("Stationary interval must contain at least 10 seconds of actual samples")
    gyro = session.gyro[mask]
    acceleration = acc[mask]
    if not np.isfinite(gyro).all() or not np.isfinite(acceleration).all():
        raise ValueError("Stationary interval contains missing/non-finite sensor values")
    if np.linalg.norm(acceleration.std(axis=0)) / G > 0.02:
        raise ValueError("Acceleration noise exceeds 0.02 g; choose a still interval")
    if abs(np.linalg.norm(acceleration.mean(axis=0)) / G - 1) > 0.1:
        raise ValueError("Stationary gravity differs by >10% from 1 g")
    noise = gyro.std(axis=0)
    if np.linalg.norm(noise) > 0.03:
        raise ValueError("Gyro variation exceeds 0.03 rad/s; choose a still interval")
    # A changing offset/rotation must not be disguised by averaging a long interval.
    half = len(gyro) // 2
    if np.linalg.norm(gyro[:half].mean(axis=0) - gyro[half:].mean(axis=0)) > 0.01:
        raise ValueError("Gyro mean changes by >0.01 rad/s across the interval")
    temperature = None
    if session.temperature is not None:
        if session.temperature.shape != (len(session.time_us),):
            raise ValueError("Temperature samples must match timestamps")
        values = session.temperature[mask]
        if not np.isfinite(values).all():
            raise ValueError("Stationary interval has missing temperature values")
        if np.ptp(values) > 1:
            raise ValueError("Temperature changes by >1°C; allow the unit to stabilize")
        temperature = float(values.mean())
        if not -41 < temperature < 87:
            raise ValueError("Temperature is outside the BMI270 representable range")
    return Measurement(start_s, end_s, gyro.mean(axis=0).tolist(), noise.tolist(),
                       len(gyro), temperature)


@dataclass
class TemperatureModel:
    temperatures: list[float]
    biases: list[list[float]]

    def correction(self, temperatures):
        x = np.asarray(self.temperatures, dtype=float)
        y = np.asarray(self.biases, dtype=float)
        values = np.asarray(temperatures, dtype=float)
        if (len(x) < 3 or y.shape != (len(x), 3) or not np.isfinite(x).all()
                or not np.isfinite(y).all() or np.any(np.diff(x) <= 0) or np.ptp(x) < 5
                or np.any(x <= -41) or np.any(x >= 87)):
            raise ValueError("Invalid temperature model: need three ordered points spanning at least 5°C")
        if not np.isfinite(values).all() or np.any(values < x[0]) or np.any(values > x[-1]):
            raise ValueError(f"Temperature outside calibrated {x[0]:.2f}–{x[-1]:.2f}°C; no extrapolation")
        return np.stack([np.interp(values, x, y[:, axis]) for axis in range(3)], axis=-1)


def fit(measurements):
    if len(measurements) < 3 or any(m.temperature_c is None for m in measurements):
        raise ValueError("Collect at least three stationary measurements with BMI270 temperatures")
    temperatures = np.asarray([m.temperature_c for m in measurements])
    biases = np.asarray([m.bias for m in measurements])
    order = np.argsort(temperatures)
    temperatures, biases = temperatures[order], biases[order]
    if np.any(np.diff(temperatures) < 0.5):
        raise ValueError("Calibration points must differ by at least 0.5°C")
    model = TemperatureModel(temperatures.tolist(), biases.tolist())
    model.correction(temperatures)
    return model


class Store:
    def __init__(self, filename=DEFAULT_CONFIG):
        self.filename = Path(filename)
        self.units = {}
        if self.filename.exists():
            payload = json.loads(self.filename.read_text())
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("Unsupported gyro calibration file version")
            if not isinstance(payload.get("units"), dict):
                raise ValueError("Gyro calibration file must contain a units object")
            self.units = payload["units"]

    def measurements(self, serial):
        return [Measurement(**m) for m in self.units.get(serial, {}).get("measurements", [])]

    def model(self, serial):
        saved = self.units.get(serial, {}).get("model")
        return TemperatureModel(**saved) if saved is not None else None

    def add(self, serial, measurement):
        if not serial:
            raise ValueError("Select the recording's unit serial before saving gyro calibration")
        unit = self.units.setdefault(serial, {"measurements": []})
        unit["measurements"].append(asdict(measurement))
        self.save()

    def fit(self, serial):
        model = fit(self.measurements(serial))
        self.units[serial]["model"] = asdict(model)
        self.save()
        return model

    def save(self):
        payload = json.dumps({"version": 1, "units": self.units}, indent=2, allow_nan=False)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", dir=self.filename.parent,
                prefix=".gyrocal-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.filename)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
