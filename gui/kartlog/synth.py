"""Synthetic go-kart recording for demos and tests."""

import numpy as np

from .analysis import G, SAMPLE_RATE_HZ
from .data import Session


def _rot(axis, deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


# Columns are the kart forward/left/up axes in device coordinates for a unit
# mounted with device -Y pointing forward and +Z roughly up, a little tilted.
MOUNT = _rot("x", 6.0) @ _rot("y", -4.0) @ np.column_stack([(0, -1, 0), (1, 0, 0), (0, 0, 1)])


def kart_profile(rest_s=4.0, laps=3, rng=None):
    """Return (t, kart_g) with columns long, lat, vert in g."""
    rng = rng or np.random.default_rng(1)
    segments = [(rest_s, 0.0, 0.0), (3.0, 0.45, 0.0)]  # (duration, long, lat)
    corner = [
        (1.0, -0.9, 0.0), (2.0, 0.0, -1.3), (2.5, 0.4, 0.0),  # brake, right hander, accel
        (0.8, -0.7, 0.0), (1.6, 0.0, 1.1), (3.0, 0.35, 0.0),  # brake, left hander, accel
        (1.2, -0.5, 0.0), (3.0, 0.1, -0.9), (2.0, 0.3, 0.0),  # long sweeper
    ]
    segments += corner * laps
    segments += [(3.0, -0.4, 0.0), (rest_s, 0.0, 0.0)]  # stop and rest
    lon, lat = [], []
    for duration, a, b in segments:
        n = int(duration * SAMPLE_RATE_HZ)
        lon.append(np.full(n, a))
        lat.append(np.full(n, b))
    lon = np.concatenate(lon)
    lat = np.concatenate(lat)
    # Smooth transitions like a real driver.
    k = np.ones(25) / 25
    lon = np.convolve(lon, k, mode="same")
    lat = np.convolve(lat, k, mode="same")
    t = np.arange(len(lon)) / SAMPLE_RATE_HZ
    moving = (t > rest_s) & (t < t[-1] - rest_s)
    vert = 1.0 + moving * 0.05 * rng.standard_normal(len(t))
    return t, np.column_stack([lon, lat, vert])


def synthetic_session(number=1, laps=3, noise_g=0.25, seed=1):
    rng = np.random.default_rng(seed)
    t, kart = kart_profile(laps=laps, rng=rng)
    moving = (t > 4.0) & (t < t[-1] - 4.0)
    vib = rng.standard_normal(kart.shape) * noise_g * moving[:, None]
    vib += rng.standard_normal(kart.shape) * 0.005  # sensor noise at rest
    device = (kart + vib) @ MOUNT.T * G
    time_us = (t * 1e6).astype(np.int64) + 10_000 + rng.integers(-300, 300, len(t))
    return Session(number, time_us, device)
