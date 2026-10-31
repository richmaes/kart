"""Timing-system lap durations, stored as exact milliseconds beside a recording."""

import json
import os
from pathlib import Path
import re
import tempfile

MAX_LAPS = 40
TIME_PATTERN = re.compile(r"(?:(\d+):([0-5]\d)|(\d+))(?:\.(\d{1,3}))?")


def parse(text):
    match = TIME_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValueError("Use seconds (42.310) or minutes:seconds (1:02.310), with at most three decimals")
    minutes, seconds, total_seconds, fraction = match.groups()
    value = ((int(minutes) * 60 + int(seconds)) if minutes is not None
             else int(total_seconds)) * 1000
    value += int((fraction or "").ljust(3, "0"))
    if value <= 0:
        raise ValueError("Lap duration must be greater than zero")
    return value


def format_time(milliseconds):
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def parse_entries(entries):
    if len(entries) > MAX_LAPS:
        raise ValueError(f"A session supports at most {MAX_LAPS} lap times")
    values = []
    blank = False
    for index, text in enumerate(entries, 1):
        if not text.strip():
            blank = True
            continue
        if blank:
            raise ValueError(f"Lap {index}: fill earlier laps first; only trailing entries may be blank")
        try:
            values.append(parse(text))
        except ValueError as exc:
            raise ValueError(f"Lap {index}: {exc}") from exc
    return values


def sidecar(csv_path):
    return Path(csv_path).with_suffix(".laptimes.json")


def validate(payload):
    if not isinstance(payload, dict) or payload.get("version") not in (1, 2):
        raise ValueError("Unsupported lap-times file version")
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        raise ValueError("Lap-times file must contain a sessions object")
    for number, values in sessions.items():
        if not isinstance(number, str) or not number.isdecimal() or int(number) < 1:
            raise ValueError("Lap-times session numbers must be positive integers")
        if (not isinstance(values, list) or len(values) > MAX_LAPS
                or any(type(value) is not int or value <= 0 for value in values)):
            raise ValueError(f"Session {number}: expected up to {MAX_LAPS} positive integer durations")
    factor = 10 if payload["version"] == 1 else 1
    return {number: [value * factor for value in values] for number, values in sessions.items()}


def load(csv_path):
    filename = sidecar(csv_path)
    if not filename.exists():
        return {}
    return validate(json.loads(filename.read_text(encoding="ascii")))


def save(csv_path, sessions):
    payload = {"version": 2, "sessions": sessions}
    validate(payload)
    filename = sidecar(csv_path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=filename.parent,
            prefix=".laptimes-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(filename)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
