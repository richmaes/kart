# Accelerometer, gyroscope and temperature log format (version 4)

This describes the data the Thingy:53 logger stores in external flash and sends
over USB with the `DUMP` command. It is the reference for tools and agents that
read the data. Related files that must stay consistent with it:

- [src/storage.h](../src/storage.h): `struct log_page_header`,
  `struct log_sample_record` and `struct log_page_image` (the firmware's layout,
  with compile-time size checks)
- [thingy53_log.ksy](thingy53_log.ksy): the same page layout as a machine-readable
  [Kaitai Struct](https://kaitai.io/) description; it compiles to parsers for most
  languages
- [tools/logger.py](../tools/logger.py): the reference decoder (`decode_page`)

Stores can contain pages of **several versions** (written by earlier firmware); each
page states its own `version`. The current layout (version 4) is below, with the
older ones after it. Versions 3 and 4 store the sensors' **raw 16-bit counts**, so a
reader must scale them (see "Converting raw counts").

## Storage area

- Partition `accelerometer-log`: 4 MiB at external QSPI flash offset
  `0x400000..0x7fffff`, so **1024 pages** of 4096 bytes.
- Page *n* lives at partition offset `n * 4096`. Pages are written in order and
  only appended; nothing is overwritten until a full erase.
- An erased page reads as all `0xff`. All multi-byte values are **little-endian**.

## Page layout (4096 bytes, version 4)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 4 | `magic` | `0x33474C41` (bytes `41 4C 47 33`) |
| 4 | 2 | `version` | `4` |
| 6 | 2 | `count` | valid samples in this page, 1..184 |
| 8 | 4 | `session` | recording session, starts at 1 |
| 12 | 4 | `page_index` | slot number; must equal the page's position |
| 16 | 4 | `sample_rate_hz` | `100` |
| 20 | 4 | `range_g` | `4` (accelerometer full scale, +/- g) |
| 24 | 4 | `gyro_range_dps` | `2000` (gyroscope full scale, +/- deg/s) |
| 28 | 2 | `accel_counts_per_g` | `500` (accelerometer counts per g) |
| 30 | 2 | `reserved` | left `0xff` |
| 32 | 22 x `count` | samples | see below |
| 32 + 22 x `count` | 22 x (184 - `count`) | unused | left `0xff` |
| 4080 | 12 | padding | left `0xff` |
| 4092 | 4 | `crc32` | CRC-32/IEEE (as `zlib.crc32`) of bytes 0..4091 |

Sample record (22 bytes, all values signed except the time):

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 8 | `time_us` | unsigned microseconds since the session started |
| 8 | 2 | `ax` | accelerometer X, raw counts |
| 10 | 2 | `ay` | accelerometer Y, raw counts |
| 12 | 2 | `az` | accelerometer Z, raw counts |
| 14 | 2 | `gx` | gyroscope X, raw counts |
| 16 | 2 | `gy` | gyroscope Y, raw counts |
| 18 | 2 | `gz` | gyroscope Z, raw counts |
| 20 | 2 | `temperature_raw` | BMI270 internal die temperature, raw counts |

### Converting raw counts

- **Acceleration:** `m/s^2 = ax / accel_counts_per_g * 9.80665` (500 counts per g at
  +/-4 g, the ADXL362's nominal sensitivity for that range).
- **Angular rate:** `deg/s = gx * gyro_range_dps / 32768`, then `rad/s = deg/s * pi / 180`
  (+/-2000 deg/s spans the full 16-bit range, about 16.4 counts per deg/s).
- **BMI270 temperature:** `Celsius = 23 + temperature_raw / 512`.
  `-32768` (`0x8000`) means invalid; acquisition stops with `-ENODATA` rather
  than recording a fabricated temperature. Host readers reject that sentinel.
- `tools/logger.py` applies exactly these formulas when it writes the CSV
  (`accel_micro` and `gyro_micro`).

Acceleration comes from the ADXL362 and angular rate from the BMI270 gyroscope,
both read in the same data-ready callback, so one `time_us` covers all six values.
The gyroscope runs at 200 Hz internally, so each gyroscope value is at most 5 ms
older than the accelerometer reading it is stored with. Axes are those of each
chip as mounted on the board; the two chips' axis orientations are not
guaranteed to match, so confirm the relative orientation before fusing them.

The application reads BMI270 registers `0x22..0x23` through the same SPI bus
configuration as the driver, sequentially after the gyro fetch under the capture
mutex. SPI reads discard the address response and dummy byte, then wait 2 us
after access (matching the installed driver). The discarded bytes use a real
two-byte receive buffer: a null placeholder can produce a zero-length nrfx DMA
chunk once the command TX buffer ends, stalling acquisition and button/USB handling.
With the gyro enabled and advanced
power saving disabled, temperature updates every 10 ms (+/-12%); reads are not
latched with the gyro/acceleration samples, so repeated temperatures are expected.
This is **die temperature**, not the BME688's ambient-temperature channel.
Source: [Bosch BMI270 datasheet, section 4.6.14](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi270-ds000.pdf).

`time_us` is taken when the sensor callback runs. It is relative to the
start of the session, not wall-clock time; the device has no real-time clock.

### Older versions

- **Version 3:** same header as v4, `version` = `3`, `count` is 1..203.
  Samples are 20 bytes: `time_us` and six raw 16-bit acceleration/gyro counts.
  There is no temperature field or extra padding.
- **Version 2:** same header up to offset 24, `version` = `2`; bytes 28..31 are `0xff`.
  `count` is 1..126 and samples are 32 bytes: `time_us`, then `ax ay az` as signed
  32-bit micro-m/s^2 and `gx gy gz` as signed 32-bit micro-rad/s (already converted).
  There are 28 + 32 x (126 - `count`) unused bytes before the CRC.
- **Version 1:** accelerometer only. `version` = `1`, bytes 24..31 are `0xff`, `count` is
  1..203 and samples are 20 bytes: `time_us`, then `ax ay az` as signed 32-bit
  micro-m/s^2 (converted). There are no gyroscope values.

## Reading a store

1. Read every page 0..1023 (or as many as the device reports).
2. A page that is entirely `0xff` is empty; skip it.
3. Otherwise the page is valid only if **all** of these hold: CRC matches,
   `magic`, `sample_rate_hz` and `range_g` are as above, `version` is 1, 2, 3 or 4
   (versions 2..4 also need `gyro_range_dps` = 2000, and versions 3 and 4 need
   `accel_counts_per_g` = 500), `count` is within the version's limit (203 for
   versions 1 and 3, 126 for version 2, 184 for version 4), `session >= 1`, and
   `page_index` equals the page's slot.
4. A non-empty page that fails validation is damaged, usually a write interrupted
   by power loss. The firmware never reuses that slot, so skip it, report it and
   continue. Do not silently treat it as missing data.
5. Pages are grouped by `session`. A new session starts each time recording
   starts, and `time_us` restarts near zero. Session numbers increase with page
   order. A session may span many pages. The last page of a session is usually
   partial (`count` below the page maximum) because stopping closes the open page.
6. Within a session, `time_us` is strictly increasing across all its samples,
   including across page boundaries.

## Detecting gaps

At 100 Hz consecutive samples are about 10,000 us apart. A larger step between
neighboring `time_us` values means samples are missing or were delayed, for
example because the storage writer fell behind. The firmware stops recording with
an error if it sees a step over 15,000 us or if a page fills before the previous
one is written. In both cases the samples stored up to that point are intact and
ordered. Any other unexpected step is worth flagging in analysis. Gaps do not
occur between pages unless a page is invalid (step 4).

## Compatibility

`version` is `4`; readers must also accept `1`, `2` and `3`. A change to any field, size or unit must increase it, with the
firmware, [thingy53_log.ksy](thingy53_log.ksy), `tools/logger.py` and this document
updated together. Readers should reject versions they do not know.

The CLI emits nine CSV columns: `session,time_us,ax_m_s2,ay_m_s2,az_m_s2,`
`gx_rad_s,gy_rad_s,gz_rad_s,temperature_c`. Temperature is blank for v1..v3;
gyro is also blank for v1. The GUI reads legacy five/eight-column CSVs and
nine-column CSVs, preserving missing values instead of inventing temperatures.
The decoders return legacy eight-field tuples, with a ninth micro-Celsius field
only for v4, to retain existing programmatic callers' legacy behavior.
