"""Relative timing-sequence alignment by repeated, ordered IMU signatures."""

from dataclasses import dataclass
import hashlib
from threading import Event

import numpy as np
from scipy.ndimage import uniform_filter1d

from . import analysis, laps, laptimes
from .data import GAP_LIMIT_US, Session


@dataclass(frozen=True)
class Candidate:
    offset_s: float  # Start of timing lap 1, in the session's plotted time coordinates.
    score: float
    similarity: float
    matched_laps: int


@dataclass
class SearchResult:
    candidates: list[Candidate]
    warnings: list[str]
    resolution_s: float


def durations(values):
    if (not isinstance(values, list) or not 2 <= len(values) <= laptimes.MAX_LAPS
            or any(type(v) is not int or v <= 0 for v in values)):
        raise ValueError("Auto alignment needs 2–40 positive integer millisecond lap durations")
    result = np.asarray(values, dtype=float) / 1000
    if (not np.isfinite(result).all() or not np.isfinite(result.sum())
            or sum(values) > np.iinfo(np.int64).max // 1000):
        raise ValueError("Lap durations are too large to align")
    return result


def boundaries(session, values, offset_s):
    durations(values)
    if (not np.isfinite(offset_s)
            or abs(offset_s) + sum(values) / 1000 >= np.iinfo(np.int64).max / 1e6):
        raise ValueError("Alignment offset must be finite")
    if len(session.time_us) < 2:
        raise ValueError("Alignment needs at least two recording samples")
    edges = round(offset_s * 1e6) + np.r_[0, np.cumsum(values, dtype=np.int64) * 1000]
    complete = []
    partial = []
    for number, (start, end) in enumerate(zip(edges[:-1], edges[1:]), 1):
        if start >= session.time_us[0] and end <= session.time_us[-1]:
            lap = laps.Lap(int(start), int(end), external_number=number)
            laps.validate(session, lap, complete)
            first = max(0, np.searchsorted(session.time_us, start, side="right") - 1)
            last = np.searchsorted(session.time_us, end, side="left")
            lap.include = bool(np.all(np.diff(session.time_us[first:last + 1]) <= GAP_LIMIT_US))
            if session.gyro is not None:
                lap.include &= bool(np.isfinite(session.gyro[first:last + 1]).all())
            complete.append(lap)
        elif end > session.time_us[0] and start < session.time_us[-1]:
            partial.append(number)
    if not complete:
        raise ValueError("This offset places no complete timing laps inside the recording")
    return complete, partial


def fingerprint(session):
    return hashlib.sha256(np.asarray(session.time_us, dtype="<i8").tobytes()).hexdigest()


def record(session, values, offset_s):
    boundaries(session, values, offset_s)
    return {"offset_us": round(offset_s * 1e6), "durations_ms": list(values),
            "timestamps_sha256": fingerprint(session)}


def restore(session, values, saved):
    if saved["durations_ms"] != values or saved["timestamps_sha256"] != fingerprint(session):
        raise ValueError("Saved alignment does not match the lap durations or recording timestamps; re-align")
    return boundaries(session, values, saved["offset_us"] / 1e6)[0]


def search(session: Session, values, acc=None, anchor_time_s=None, anchor_lap=1,
           anchor_radius_s=2.0, cancel_event: Event | None = None):
    """Rank offsets using mean pairwise correlation of normalized lap signatures.

    Each lap's native-axis gyro mean is removed, rejecting constant bias without
    requiring a stationary startup. No position integration or clock stretching.
    """
    lengths = durations(values)
    t = session.t
    if (len(t) < 16 or not np.isfinite(t).all() or np.any(np.diff(t) <= 0)
            or session.gyro is None or session.gyro.shape != (len(t), 3)):
        raise ValueError("Auto alignment needs increasing timestamps and XYZ gyro samples")
    acc = session.acc if acc is None else np.asarray(acc)
    if acc.shape != (len(t), 3) or not np.isfinite(acc).all():
        raise ValueError("Auto alignment needs finite XYZ acceleration samples")
    finite = np.isfinite(session.gyro).all(axis=1)
    if np.count_nonzero(finite) < 16:
        raise ValueError("Auto alignment needs usable gyro data; legacy/missing samples cannot be matched")
    cumulative = np.r_[0, np.cumsum(lengths)]
    low, high = t[0] - cumulative[-1] + min(lengths), t[-1] - min(lengths)
    if anchor_time_s is not None:
        if (type(anchor_lap) is not int or not 1 <= anchor_lap <= len(lengths)
                or not np.isfinite(anchor_time_s) or not t[0] <= anchor_time_s <= t[-1]
                or not np.isfinite(anchor_radius_s) or anchor_radius_s <= 0):
            raise ValueError("Anchor needs an in-recording start-crossing time, valid lap number and positive radius")
        center = anchor_time_s - cumulative[anchor_lap - 1]
        low, high = max(low, center - anchor_radius_s), min(high, center + anchor_radius_s)
    if high < low:
        raise ValueError("The timing sequence/anchor cannot overlap enough of this recording")

    # Smooth only within contiguous finite runs; never mix data across recording gaps.
    gyro = np.zeros_like(session.gyro)
    cuts = np.r_[0, np.flatnonzero((np.diff(session.time_us) > GAP_LIMIT_US)
                                   | (finite[1:] != finite[:-1])) + 1, len(t)]
    for start, end in zip(cuts[:-1], cuts[1:]):
        if finite[start]:
            width = max(1, round(0.15 * analysis.sample_rate(t[start:end])))
            gyro[start:end] = uniform_filter1d(session.gyro[start:end], width,
                                               axis=0, mode="nearest")
    bad_intervals = ((np.diff(session.time_us) > GAP_LIMIT_US)
                     | ~finite[:-1] | ~finite[1:])
    bad_prefix = np.r_[0, np.cumsum(bad_intervals)]
    progress = np.linspace(0, 1, 129)
    max_possible = min(len(lengths), int((t[-1] - t[0]) / min(lengths)))
    if max_possible < 2:
        raise ValueError("Recording must contain at least two complete timing laps")

    def evaluate(offsets):
        results = []
        for begin in range(0, len(offsets), 64):
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError("Alignment search cancelled")
            batch = np.asarray(offsets[begin:begin + 64])
            starts = batch[:, None] + cumulative[:-1]
            ends = batch[:, None] + cumulative[1:]
            first = np.clip(np.searchsorted(t, starts, side="right") - 1, 0, len(t) - 1)
            last = np.clip(np.searchsorted(t, ends, side="left"), 0, len(t) - 1)
            valid = ((starts >= t[0]) & (ends <= t[-1]) & (last - first >= 2)
                     & (bad_prefix[last] == bad_prefix[first]))
            queries = starts[:, :, None] + lengths[None, :, None] * progress

            def signatures(signal):
                sampled = np.stack([np.interp(queries.ravel(), t, axis).reshape(queries.shape)
                                    for axis in signal.T], axis=-1)
                centered = sampled - sampled.mean(axis=2, keepdims=True)
                norm = np.sqrt(np.sum(centered ** 2, axis=(2, 3)))
                return centered / np.maximum(norm[:, :, None, None], 1e-12), norm / np.sqrt(len(progress))

            g, variation = signatures(gyro)
            valid &= variation > 0.02  # Quiet/constant-rate windows are not a corner signature.
            count = valid.sum(axis=1)
            a, accel_variation = signatures(acc)

            def correlation(signal, mask):
                n = mask.sum(axis=1)
                summed = np.sum(signal * mask[:, :, None, None], axis=1)
                # ||sum(unit vectors)||² - n is the sum of all off-diagonal dot products.
                return ((np.sum(summed ** 2, axis=(1, 2)) - n)
                        / np.maximum(n * (n - 1), 1))

            similarity = correlation(g, valid)
            acc_mask = valid & (accel_variation > 0.03 * analysis.G)
            use_acc = acc_mask.sum(axis=1) >= 2
            similarity = np.where(use_acc, 0.8 * similarity + 0.2 * correlation(a, acc_mask), similarity)
            scores = 0.9 * np.maximum(similarity, 0) + 0.1 * count / max_possible
            for index in np.flatnonzero(count >= 2):
                results.append(Candidate(float(batch[index]), float(scores[index]),
                                         float(similarity[index]), int(count[index])))
        return results

    steps = max(1, min(12000, int(np.ceil((high - low) / 0.1))))
    grid = np.linspace(low, high, steps + 1)
    coarse_step = (high - low) / steps
    scored = evaluate(grid)
    if not scored:
        raise ValueError("No offset contains two complete, gap-free laps with varying gyro turn patterns")
    separation = max(0.5, float(min(lengths)) * 0.05)

    def distinct(items, limit):
        selected = []
        for item in sorted(items, key=lambda c: c.score, reverse=True):
            if all(abs(item.offset_s - old.offset_s) >= separation for old in selected):
                selected.append(item)
                if len(selected) == limit:
                    break
        return selected

    seeds = distinct(scored, 10)
    fine_step = max(0.01, coarse_step / 20)
    refined = evaluate(np.unique(np.concatenate([
        np.linspace(max(low, seed.offset_s - coarse_step),
                    min(high, seed.offset_s + coarse_step), 41) for seed in seeds])))
    candidates = distinct(scored + refined, 5)
    if candidates[0].similarity < 0.35:
        raise ValueError("No consistent repeating lap signature found; check times, kart selection and overlap")
    warnings = []
    if anchor_time_s is None:
        warnings.append("No timing-line anchor: repeating patterns cannot establish the physical timing line.")
    if len(candidates) > 1 and candidates[0].score - candidates[1].score < 0.03:
        warnings.append("Ambiguous: multiple offsets have similar scores. Inspect candidates or supply an anchor.")
    if candidates[0].matched_laps == 2:
        warnings.append("Only two usable laps support the best match; evidence is weak.")
    if bad_intervals.any():
        warnings.append("Laps spanning gaps or missing gyro were excluded from scoring.")
    warnings.append("Scores measure pattern similarity/coverage, not an accuracy probability. Confirm visually.")
    return SearchResult(candidates, warnings, fine_step)
