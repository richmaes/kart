"""Local OCR and spatial lap-time column proposals; all results require review."""

import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageOps

from . import laptimes


@dataclass(frozen=True)
class Word:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float

    @property
    def center(self):
        return self.x + self.width / 2


@dataclass
class Column:
    label: str
    words: list[Word]
    lap_numbers: list[int | None]


def open_image(filename):
    with Image.open(filename) as source:
        if source.format not in ("PNG", "JPEG", "MPO"):
            raise ValueError("Select a PNG or JPG/JPEG image")
        return ImageOps.exif_transpose(source).convert("RGB")


def recognize(image):
    """Return normalized top-left word boxes, using no network service."""
    with tempfile.TemporaryDirectory(prefix="kartlog-ocr-") as directory:
        filename = Path(directory) / "image.png"
        image.save(filename)
        if sys.platform == "darwin":
            executable = shutil.which("swift")
            if executable is None:
                raise ValueError("Apple OCR needs Swift: install Xcode Command Line Tools (xcode-select --install)")
            command = [executable, str(Path(__file__).with_name("vision_ocr.swift")), str(filename)]
        else:
            executable = shutil.which("tesseract")
            if executable is None:
                raise ValueError("Local OCR needs Tesseract installed on this system and available on PATH")
            command = [executable, str(filename), "stdout", "--psm", "6", "tsv"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Local OCR timed out after 120 seconds; try a smaller table crop") from exc
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"Local OCR failed: {exc.stderr.strip() or exc}") from exc
        if sys.platform == "darwin":
            words = [Word(**value) for value in json.loads(result.stdout)]
        else:
            words = tesseract_words(result.stdout, image.width, image.height)
        if not words:
            raise ValueError("No text recognized; rotate/crop the table or use a sharper photo")
        return words


def tesseract_words(tsv, width, height):
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        if row["level"] != "5" or not row["text"].strip():
            continue
        words.append(Word(
            row["text"].strip(), int(row["left"]) / width, int(row["top"]) / height,
            int(row["width"]) / width, int(row["height"]) / height,
            max(0, float(row["conf"])) / 100))
    return words


def is_time(text):
    if not re.fullmatch(r"(?:\d+:)?\d+\.\d{1,3}", text):
        return False
    try:
        laptimes.parse(text)
    except ValueError:
        return False
    return True


def columns(words):
    """Group time tokens by horizontal column; propose headers and printed lap numbers."""
    groups = []
    for word in sorted((w for w in words if is_time(w.text)), key=lambda w: w.center):
        group = next((g for g in groups
                      if abs(word.center - sum(w.center for w in g) / len(g))
                      < max(word.width * 0.6, 0.015)), None)
        if group is None:
            groups.append([word])
        else:
            group.append(word)
    results = []
    for group in groups:
        if len(group) < 2:
            continue
        center = sum(w.center for w in group) / len(group)
        first, last = min(w.y for w in group), max(w.y for w in group)
        height = max(w.height for w in group)
        # Include suspicious tokens in the column instead of silently dropping an OCR error.
        candidates = [w for w in words if abs(w.center - center) < max(w.width * 0.6, 0.015)
                      and first - height * 0.5 <= w.y <= last + height * 0.5]
        candidates.sort(key=lambda w: w.y)
        headers = [w for w in words if w.text.isdecimal() and w.y < first
                   and abs(w.center - center) < max(w.width, 0.04)]
        header = max(headers, key=lambda w: w.y, default=None)
        numbers = []
        for word in candidates:
            nearby = [w for w in words if w.text.isdecimal() and 1 <= int(w.text) <= 40
                      and w.center < center - word.width
                      and abs((w.y + w.height / 2) - (word.y + word.height / 2))
                      < max(w.height, word.height) * 1.5]
            lap = min(nearby, key=lambda w: abs(
                (w.y + w.height / 2) - (word.y + word.height / 2)), default=None)
            numbers.append(int(lap.text) if lap else None)
        label = f"Column {len(results) + 1}"
        if header:
            label += f" (kart {header.text}?)"
        label += f" - {len(candidates)} rows"
        results.append(Column(label, candidates, numbers))
    if not results:
        raise ValueError("No lap-time columns found; crop one lap table including its kart headers")
    return results


def reviewed_rows(text):
    """Explicit 'lap duration' lines prevent skipped/duplicated OCR rows shifting later laps."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdecimal():
            raise ValueError("Each preview line must contain a lap number and duration, e.g. 19 25.313")
        number = int(parts[0])
        if not 1 <= number <= laptimes.MAX_LAPS:
            raise ValueError("Imported lap numbers must be between 1 and 40")
        rows.append((number, laptimes.parse(parts[1])))
    if not rows:
        raise ValueError("Review at least one lap time before importing")
    numbers = [number for number, _value in rows]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        raise ValueError("Preview lap numbers must be consecutive; fix missing, duplicate or out-of-order rows")
    return rows


def merge_entries(entries, rows):
    result = list(entries)
    if len(result) != laptimes.MAX_LAPS:
        raise ValueError("Expected 40 destination lap entries")
    for number, value in rows:
        if not 1 <= number <= laptimes.MAX_LAPS:
            raise ValueError("Imported lap numbers must be between 1 and 40")
        result[number - 1] = laptimes.format_time(value)
    laptimes.parse_entries(result)
    return result
