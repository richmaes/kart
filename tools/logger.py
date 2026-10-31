#!/usr/bin/env python3
"""Download Thingy:53 logger pages over USB CDC ACM and validate them."""

import argparse
import csv
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import zlib

PAGE_SIZE = 4096
MAX_PAGES = 1024
HEADER = struct.Struct("<IHHIIII")
EXTRA = struct.Struct("<IHH")  # gyro_range_dps, accel_counts_per_g, reserved (versions 2 and 3)
SAMPLE_V1 = struct.Struct("<Qiii")
SAMPLE_V2 = struct.Struct("<Qiiiiii")
SAMPLE_V3 = struct.Struct("<Qhhhhhh")
SAMPLE_V4 = struct.Struct("<Qhhhhhhh")
MAX_SAMPLES = {1: 203, 2: 126, 3: 203, 4: 184}
ACCEL_COUNTS_PER_G = 500
GYRO_FULL_SCALE_DPS = 2000
G_MICRO = 9806650
CSV_HEADER = (
    "session", "time_us", "ax_m_s2", "ay_m_s2", "az_m_s2",
    "gx_rad_s", "gy_rad_s", "gz_rad_s",
    "temperature_c",
)


class ProtocolError(ValueError):
    pass


def accel_micro(counts):
    """Raw ADXL362 counts (+/-4 g, 500 counts per g) to micro-m/s^2."""
    return round(counts * G_MICRO / ACCEL_COUNTS_PER_G)


def gyro_micro(counts):
    """Raw BMI270 counts (+/-2000 deg/s over 2^15 counts) to micro-rad/s."""
    return round(counts * GYRO_FULL_SCALE_DPS * math.pi * 1000000 / (180 * 32768))


def decode_page(page, index):
    """Return (session, time_us, ax, ay, az, gx, gy, gz[, temperature]) rows.

    Accelerations are micro-m/s^2 and angular rates micro-rad/s. The gyroscope
    values are None for version 1 pages. Version 4 adds micro-degrees Celsius;
    legacy row tuples retain eight fields. CSV output always has nine columns.
    """
    if len(page) != PAGE_SIZE:
        raise ProtocolError(f"Page {index}: wrong size")
    if page == b"\xff" * PAGE_SIZE:
        return []
    magic, version, count, session, sequence, rate, accel_range = HEADER.unpack_from(page)
    gyro_range, counts_per_g, _ = EXTRA.unpack_from(page, HEADER.size)
    crc = struct.unpack_from("<I", page, PAGE_SIZE - 4)[0]
    if crc != zlib.crc32(page[:-4]):
        raise ProtocolError(f"Page {index}: CRC mismatch (torn write or damaged data)")
    if (
        magic != 0x33474C41
        or version not in MAX_SAMPLES
        or not 1 <= count <= MAX_SAMPLES[version]
        or not session
        or sequence != index
        or rate != 100
        or accel_range != 4
        or (version >= 2 and gyro_range != GYRO_FULL_SCALE_DPS)
        or (version >= 3 and counts_per_g != ACCEL_COUNTS_PER_G)
    ):
        raise ProtocolError(f"Page {index}: invalid or unsupported header")
    sample = {1: SAMPLE_V1, 2: SAMPLE_V2, 3: SAMPLE_V3, 4: SAMPLE_V4}[version]
    records = []
    previous = None
    for offset in range(count):
        timestamp, *values = sample.unpack_from(page, 32 + offset * sample.size)
        if previous is not None and timestamp <= previous:
            raise ProtocolError(f"Page {index}: non-increasing timestamps")
        previous = timestamp
        if version >= 3:
            temperature = values[6] if version == 4 else None
            values = [accel_micro(v) for v in values[:3]] + [gyro_micro(v) for v in values[3:6]]
            if version == 4:
                if temperature == -32768:
                    raise ProtocolError(f"Page {index}: invalid BMI270 temperature")
                values.append(round((23 + temperature / 512) * 1000000))
        elif version == 1:
            values += [None] * 3
        records.append((session, timestamp, *values))
    return records


def decimal_micro(value):
    return f"{'-' if value < 0 else ''}{abs(value) // 1000000}.{abs(value) % 1000000:06d}"


def read_exact(port, size):
    result = bytearray()
    while len(result) < size:
        data = port.read(size - len(result))
        if not data:
            raise ProtocolError(
                "Transfer timed out or was interrupted; stop recording and retry"
            )
        result.extend(data)
    return bytes(result)


def read_line(port):
    line = bytearray()
    while len(line) < 256:
        char = read_exact(port, 1)
        line.extend(char)
        if char == b"\n":
            try:
                return line.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise ProtocolError("Non-ASCII protocol response") from exc
    raise ProtocolError("Protocol response too long")


def download(port, output, skip_invalid=False):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    port.write(b"DUMP\n")
    response = read_line(port)
    parts = response.split()
    if len(parts) != 2 or parts[0] != "BEGIN" or not parts[1].isdigit():
        raise ProtocolError(f"Device refused download: {response}")
    pages = int(parts[1])
    if not 0 <= pages <= MAX_PAGES:
        raise ProtocolError(f"Invalid page count: {pages}")

    temp_path = None
    rows = 0
    skipped = 0
    previous = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="ascii", dir=output.parent,
            prefix=".logger-", suffix=".tmp", delete=False
        ) as temp:
            temp_path = Path(temp.name)
            writer = csv.writer(temp)
            writer.writerow(CSV_HEADER)
            for index in range(pages):
                page = read_exact(port, PAGE_SIZE)
                try:
                    records = decode_page(page, index)
                    page_previous = previous
                    for record in records:
                        key = record[:2]
                        if page_previous is not None and key <= page_previous:
                            raise ProtocolError(f"Page {index}: session/time order mismatch")
                        page_previous = key
                except ProtocolError as exc:
                    if not skip_invalid:
                        raise
                    skipped += 1
                    print(f"WARNING: skipping invalid page: {exc}", file=sys.stderr)
                    continue
                previous = page_previous
                for session, timestamp, *values in records:
                    if len(values) == 6:
                        values.append(None)
                    writer.writerow((
                        session, timestamp,
                        *("" if v is None else decimal_micro(v) for v in values),
                    ))
                    rows += 1
            if read_line(port) != "END":
                raise ProtocolError("Missing END marker; incomplete download")
            temp.flush()
            os.fsync(temp.fileno())
        # Same-directory hard link publishes a complete file without overwriting.
        os.link(temp_path, output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return rows, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="USB serial port; auto-detected if only one matches")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="List available serial ports")
    commands.add_parser("status", help="Read recording state and storage/error counters")
    get = commands.add_parser("download", help="Download all sessions as a CSV file")
    get.add_argument("output", type=Path)
    get.add_argument(
        "--skip-invalid", action="store_true",
        help="Explicitly salvage valid pages, warning about every skipped page"
    )
    args = parser.parse_args()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        parser.exit(1, "Install dependencies: python -m pip install -r tools/requirements.txt\n")

    try:
        ports = list(list_ports.comports())
        if args.command == "ports":
            for port in ports:
                print(f"{port.device}\t{port.description}")
            return 0
        selected = args.port
        if not selected:
            matches = [
                p.device for p in ports
                if "Thingy53 Accelerometer Logger" in (p.product or "")
            ]
            if len(matches) != 1:
                raise ProtocolError("Specify --port; use the 'ports' command to list devices")
            selected = matches[0]
        with serial.Serial(selected, 115200, timeout=5, write_timeout=5) as port:
            port.dtr = True
            port.reset_input_buffer()
            # Terminate any partial command left by an earlier serial client.
            port.write(b"\nSTATUS\n")
            response = read_line(port)
            if response == "ERR unknown-command":
                response = read_line(port)
            if not response.startswith("STATUS "):
                raise ProtocolError(f"Unexpected device response: {response}")
            print(response)
            if args.command == "download":
                if not response.startswith("STATUS stopped "):
                    raise ProtocolError("Stop the logger before downloading")
                rows, skipped = download(port, args.output, args.skip_invalid)
                print(f"Saved {rows} samples to {args.output}; invalid pages skipped: {skipped}")
    except (OSError, ValueError, serial.SerialException) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
