"""Experimental inertial dead reckoning, initialized at rest without forced closure."""

from dataclasses import dataclass

import numpy as np

from . import analysis, gyrocal
from .data import GAP_LIMIT_US, Session


@dataclass
class ApparentPath:
    position: np.ndarray
    velocity: np.ndarray
    gyro_bias: np.ndarray
    rest_end: int
    final_orientation: np.ndarray
    tilt_corrections: int
    bias_updates: int


def gyro_mapping(axes):
    """Each accel-frame axis names the signed gyro axis supplying its component."""
    if len(axes) != 3 or any(axis not in analysis.DEVICE_AXES for axis in axes):
        raise ValueError("Choose three signed gyro axes")
    matrix = np.asarray([analysis.DEVICE_AXES[axis] for axis in axes])
    if not np.allclose(matrix @ matrix.T, np.eye(3)) or np.linalg.det(matrix) < 0:
        raise ValueError("Gyro mapping must use each axis once and preserve right-handedness")
    return matrix


def _rotation(vector):
    angle = float(np.linalg.norm(vector))
    if angle < 1e-12:
        return np.eye(3)
    x, y, z = vector / angle
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + np.sin(angle) * cross + (1 - np.cos(angle)) * (cross @ cross)


def _gravity_correction(orientation, measured, gravity_norm, noise_g, angular_rate):
    """Reject unreliable gravity observations; return a body-frame tilt correction."""
    magnitude = float(np.linalg.norm(measured))
    if (abs(magnitude / gravity_norm - 1) > 0.03 or noise_g > 0.02
            or angular_rate > 0.1):
        return None
    measured_up = measured / magnitude
    predicted_up = orientation[2]
    cross = np.cross(measured_up, predicted_up)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.dot(measured_up, predicted_up))
    # Large disagreements may be linear acceleration or an incorrect axis mapping.
    if cosine <= 0 or sine > 0.08:
        return None
    if sine < 1e-12:
        return np.zeros(3)
    return cross * (np.arctan2(sine, cosine) / sine)


def estimate(session: Session, acc, rest_s=3.0, forward_axis="+X", yaw_deg=0.0,
             axes=("+X", "+Y", "+Z"), settle_s=1.0, fuse_tilt=True,
             stationary_intervals=(), temperature_model=None,
             startup_accel_variation_limit_g=0.05, vibration_cutoff_hz=0.0,
             startup_gyro_variation_limit_rad_s=0.03):
    acc = np.asarray(acc, dtype=float)
    if forward_axis not in analysis.DEVICE_AXES or not np.isfinite(yaw_deg):
        raise ValueError("Choose a valid Forward axis and finite Yaw trim")
    if session.gyro is None:
        raise ValueError("This session has no gyroscope data; record v2/v3/v4 data or open a gyro-enabled CSV")
    if acc.shape != (len(session.time_us), 3) or session.gyro.shape != acc.shape:
        raise ValueError("Acceleration, gyro and timestamps must have matching sample counts")
    if not np.isfinite(acc).all() or not np.isfinite(session.gyro).all():
        raise ValueError("Missing/non-finite sensor samples: select a complete gyro-enabled session")
    if len(acc) < 3 or not np.isfinite(rest_s) or rest_s <= 0:
        raise ValueError("Choose a positive stationary duration with at least three samples")
    if (not np.isfinite(startup_accel_variation_limit_g)
            or startup_accel_variation_limit_g <= 0):
        raise ValueError("Startup acceleration-variation limit must be a positive finite number")
    if (not np.isfinite(startup_gyro_variation_limit_rad_s)
            or startup_gyro_variation_limit_rad_s <= 0):
        raise ValueError("Startup gyro-variation limit must be a positive finite number")
    dt_us = np.diff(session.time_us)
    if np.any(dt_us <= 0):
        raise ValueError("Timestamps must be strictly increasing")
    if np.any(dt_us > GAP_LIMIT_US):
        raise ValueError("Sample gaps exceed 15 ms; path integration is unsafe for this session")
    elapsed = (session.time_us - session.time_us[0]) / 1e6
    if not np.isfinite(settle_s) or not 0 <= settle_s < rest_s:
        raise ValueError("Settling seconds must be finite, non-negative and shorter than stationary seconds")
    start = int(np.searchsorted(elapsed, settle_s, side="left"))
    end = int(np.searchsorted(elapsed, rest_s, side="right"))
    if end - start < 3 or end >= len(acc):
        raise ValueError("Need at least three samples after settling and subsequent motion")
    mapping = gyro_mapping(axes)
    raw_gyro = session.gyro.copy()
    if temperature_model is not None:
        if session.temperature is None:
            raise ValueError("Temperature compensation requires v4 temperature samples")
        if session.temperature.shape != (len(acc),):
            raise ValueError("Temperature samples must match acceleration timestamps")
        raw_gyro -= temperature_model.correction(session.temperature)
    gyro = raw_gyro @ mapping.T
    stationary_acc = acc[start:end]
    stationary_gyro = gyro[start:end]
    acc_std_g = float(np.linalg.norm(stationary_acc.std(axis=0)) / analysis.G)
    gyro_std = float(np.linalg.norm(stationary_gyro.std(axis=0)))
    window = f"{settle_s:g}–{rest_s:g} s"
    if acc_std_g > startup_accel_variation_limit_g:
        raise ValueError(f"Stationary window {window}: acceleration variation {acc_std_g:.4f} g "
                         f"exceeds {startup_accel_variation_limit_g:.4f} g; keep the mounted kart still")
    if gyro_std > startup_gyro_variation_limit_rad_s:
        raise ValueError(f"Stationary window {window}: gyro variation {gyro_std:.5f} rad/s "
                         f"exceeds {startup_gyro_variation_limit_rad_s:g} rad/s; keep the kart still")
    if abs(np.linalg.norm(stationary_acc.mean(axis=0)) / analysis.G - 1.0) > 0.1:
        raise ValueError("Initial gravity magnitude differs from 1 g by >10%; check sensor calibration")
    raw_acc = acc
    acc = analysis.vibration_filter(elapsed, acc, vibration_cutoff_hz)
    stationary_acc = acc[start:end]
    cal = analysis.calibrate(elapsed, acc, settle_s, rest_s - settle_s, forward_axis, yaw_deg)
    bias = stationary_gyro.mean(axis=0)
    gyro = gyro - bias
    updates = {}
    last_end = rest_s
    for interval_start, interval_end in sorted(stationary_intervals):
        if interval_start < last_end:
            raise ValueError("Marked stationary intervals must not overlap initialization or each other")
        gyrocal.measure(session, raw_acc, interval_start, interval_end)
        first = int(np.searchsorted(elapsed, interval_start))
        last = int(np.searchsorted(elapsed, interval_end, side="right"))
        if last >= len(acc):
            raise ValueError("A bias-update interval needs subsequent samples")
        updates[last] = (raw_gyro[first:last] @ mapping.T).mean(axis=0) - bias
        last_end = interval_end
    orientation = cal.rotation.copy()
    # Use initial gravity magnitude to avoid turning residual static scale error into motion.
    gravity = np.array([0.0, 0.0, np.linalg.norm(stationary_acc.mean(axis=0))])
    velocity = np.zeros_like(acc)
    position = np.zeros_like(acc)
    previous_acc = np.zeros(3)
    corrections = 0
    quiet_s = 0.0
    bias_adjustment = np.zeros(3)
    if fuse_tilt:
        window_starts = np.searchsorted(elapsed, elapsed - 0.5, side="left")
        sums = np.vstack([np.zeros(3), np.cumsum(acc, axis=0)])
        square_sums = np.vstack([np.zeros(3), np.cumsum(acc ** 2, axis=0)])
    for index in range(end, len(acc)):
        if index in updates:
            bias_adjustment = updates[index]
        dt = dt_us[index - 1] / 1e6
        angular_velocity = (gyro[index - 1] + gyro[index]) * 0.5 - bias_adjustment
        orientation = orientation @ _rotation(angular_velocity * dt)
        if fuse_tilt:
            first = int(window_starts[index])
            count = index - first + 1
            mean = (sums[index + 1] - sums[first]) / count
            variance = (square_sums[index + 1] - square_sums[first]) / count - mean ** 2
            noise_g = np.sqrt(np.maximum(variance, 0).sum()) / analysis.G
            correction = _gravity_correction(
                orientation, mean, gravity[2], noise_g, np.linalg.norm(gyro[index] - bias_adjustment))
            # Also gate the current sample, so an abrupt maneuver stops correction immediately.
            current = _gravity_correction(
                orientation, acc[index], gravity[2], noise_g, np.linalg.norm(gyro[index] - bias_adjustment))
            if correction is None or current is None:
                quiet_s = 0.0
            else:
                quiet_s += dt
                if quiet_s >= 0.5:
                    orientation = orientation @ _rotation(correction * (-np.expm1(-dt / 5.0)))
                    corrections += 1
        linear_acc = orientation @ acc[index] - gravity
        velocity[index] = velocity[index - 1] + (previous_acc + linear_acc) * (0.5 * dt)
        position[index] = position[index - 1] + (velocity[index - 1] + velocity[index]) * (0.5 * dt)
        previous_acc = linear_acc
    return ApparentPath(position, velocity, bias, end, orientation, corrections, len(updates))
