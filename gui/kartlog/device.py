"""USB CDC ACM protocol for the Thingy:53 accelerometer logger.

Mirrors the reference decoder in ../../tools/logger.py and the format
in ../../docs/log-format.md (versions 1, 2, and 3). Keep them
consistent.
"""

import math
import struct
import zlib

PAGE_SIZE = 4096
MAX_PAGES = 1024
MAGIC = 0x33474C41
HEADER = struct.Struct("<IHHIIII")
EXTRA = struct.Struct("<IHH")
SAMPLES = {
    1: struct.Struct("<Qiii"),
    2: struct.Struct("<Qiiiiii"),
    3: struct.Struct("<Qhhhhhh"),
    4: struct.Struct("<Qhhhhhhh"),
}
MAX_SAMPLES = {1: 203, 2: 126, 3: 203, 4: 184}
ACCEL_COUNTS_PER_G = 500
GYRO_RANGE_DPS = 2000
G_MICRO = 9_806_650
PRODUCT_NAME = "Thingy53 Accelerometer Logger"


class ProtocolError(ValueError):
    pass


def _accel_micro(counts):
    return round(counts * G_MICRO / ACCEL_COUNTS_PER_G)


def _gyro_micro(counts):
    return round(counts * GYRO_RANGE_DPS * math.pi * 1_000_000 / (180 * 32768))


def decode_page(page, index):
    """Return rows (session, time_us, accel xyz, gyro xyz) in micro-units.

    Gyroscope values are None for version 1 pages.
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
        magic != MAGIC
        or version not in MAX_SAMPLES
        or not 1 <= count <= MAX_SAMPLES[version]
        or not session
        or sequence != index
        or rate != 100
        or accel_range != 4
        or (version >= 2 and gyro_range != GYRO_RANGE_DPS)
        or (version >= 3 and counts_per_g != ACCEL_COUNTS_PER_G)
    ):
        raise ProtocolError(f"Page {index}: invalid or unsupported header")
    sample = SAMPLES[version]
    records = []
    previous = None
    for offset in range(count):
        timestamp, *values = sample.unpack_from(page, 32 + offset * sample.size)
        if previous is not None and timestamp <= previous:
            raise ProtocolError(f"Page {index}: non-increasing timestamps")
        previous = timestamp
        if version >= 3:
            temperature = values[6] if version == 4 else None
            values = [_accel_micro(value) for value in values[:3]] + [
                _gyro_micro(value) for value in values[3:6]
            ]
            if version == 4:
                if temperature == -32768:
                    raise ProtocolError(f"Page {index}: invalid BMI270 temperature")
                values.append(round((23 + temperature / 512) * 1_000_000))
        elif version == 1:
            values += [None] * 3
        records.append((session, timestamp, *values))
    return records


def read_exact(port, size):
    result = bytearray()
    while len(result) < size:
        data = port.read(size - len(result))
        if not data:
            raise ProtocolError("Transfer timed out or was interrupted; stop recording and retry")
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


def list_ports():
    """Return [(device, description, is_logger), ...]."""
    from serial.tools import list_ports as lp
    return [
        (p.device, p.description, PRODUCT_NAME in (p.product or ""))
        for p in lp.comports()
    ]


def serial_number(port_name):
    """USB serial number of the port (the unit's hardware ID), or None."""
    from serial.tools import list_ports as lp
    for p in lp.comports():
        if p.device == port_name:
            return p.serial_number
    return None


def _open(port_name):
    import serial
    port = serial.Serial(port_name, 115200, timeout=5, write_timeout=5)
    port.dtr = True
    port.reset_input_buffer()
    return port


def query_status(port):
    # Terminate any partial command left by an earlier serial client.
    port.write(b"\nSTATUS\n")
    response = read_line(port)
    if response == "ERR unknown-command":
        response = read_line(port)
    if not response.startswith("STATUS "):
        raise ProtocolError(f"Unexpected device response: {response}")
    return response


def status(port_name):
    with _open(port_name) as port:
        return query_status(port)


def download(port_name, skip_invalid=False, progress=None):
    """Read every page into memory.

    Returns (status_line, records, warnings). `progress(done, total)` is called
    after each page.
    """
    with _open(port_name) as port:
        status_line = query_status(port)
        if not status_line.startswith("STATUS stopped "):
            raise ProtocolError("Stop the logger before downloading")
        return (status_line, *download_from(port, skip_invalid, progress))


def download_from(port, skip_invalid=False, progress=None):
    port.write(b"DUMP\n")
    response = read_line(port)
    parts = response.split()
    if len(parts) != 2 or parts[0] != "BEGIN" or not parts[1].isdigit():
        raise ProtocolError(f"Device refused download: {response}")
    pages = int(parts[1])
    if not 0 <= pages <= MAX_PAGES:
        raise ProtocolError(f"Invalid page count: {pages}")

    records = []
    warnings = []
    previous = None
    for index in range(pages):
        page = read_exact(port, PAGE_SIZE)
        try:
            page_records = decode_page(page, index)
            page_previous = previous
            for record in page_records:
                key = record[:2]
                if page_previous is not None and key <= page_previous:
                    raise ProtocolError(f"Page {index}: session/time order mismatch")
                page_previous = key
        except ProtocolError as exc:
            if not skip_invalid:
                raise
            warnings.append(f"Skipped invalid page: {exc}")
            continue
        previous = page_previous
        records.extend(page_records)
        if progress:
            progress(index + 1, pages)
    if read_line(port) != "END":
        raise ProtocolError("Missing END marker; incomplete download")
    return records, warnings
