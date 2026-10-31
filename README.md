# Thingy:53 accelerometer and gyroscope logger

Firmware for the **Nordic Thingy:53**, targeting **nRF Connect SDK v3.4.1**.
Uses the onboard ADXL362 accelerometer at **100 Hz, +/-4 g** and the BMI270
gyroscope (**+/-2000 deg/s**, read with every accelerometer sample), external QSPI
flash, and USB CDC ACM for laptop downloads. Bluetooth is not required or enabled.

## Operation

- Power on: stopped; existing recordings remain in flash.
- Short press and release the top user button (`sw0` / P1.14): start recording.
- Short press again: stop and flush the current recording.
- Repeat to append additional sessions without deleting earlier sessions.
- Hold that button for **5 seconds**: erase all logger data and **remain stopped**.
  Release does not start another recording. Erase can take up to a minute;
  keep power connected. This action cannot be undone.
- When stopped, connect a USB **data** cable and download using the tool below.
  Download is read-only; it does not clear logs. Short presses are ignored during
  a transfer. A long hold cancels the transfer and erases the logs.

Do not confuse the user button with the separate reset/bootloader button.

LEDs: green = recording; blue = stopped/ready or downloading; red = an error or
invalid stored page. Blue blinks slowly while erasing (this can take
up to a minute), then flashes three times quickly when the erase completes
successfully. Use `status` to inspect errors
(negative Zephyr errno values). A full store stops collection with `-ENOSPC`;
it never overwrites old recordings. A sensor timing error (`-EOVERFLOW`) or a storage write
that falls behind (`-EBUSY`) also stops
collection explicitly. Erase frees space; reboot is required after a fatal
initialization or erase error.

## Build and flash

Open an **nRF Connect SDK v3.4.1 terminal** (including its matching toolchain),
then run from this directory:

```sh
west build --no-sysbuild -b thingy53/nrf5340/cpuapp -d build-app .
west flash -d build-app
```

This is a standalone, secure **application-core** image. It deliberately disables
MCUboot and does not use the network core. The board's normal sysbuild defaults
enable network-core secure boot, which is unnecessary for this USB-only app.
Do not omit `--no-sysbuild`.

**Initial flashing requires an external SWD debug probe**, such as a compatible
Nordic DK connected to the Thingy:53 debug connector. USB here is for logged-data
downloads, not factory bootloader/DFU updates. Programming this image replaces
the factory application/bootloader layout. Back up anything important first;
debug-probe recovery/chip-erase operations can destroy existing data.
No hardware has been flashed automatically by this project.

## Download CSV on macOS, Linux, or Windows

Python 3.10+:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r tools/requirements.txt
python tools/logger.py ports
python tools/logger.py --port /dev/cu.usbmodemXXXX status
python tools/logger.py --port /dev/cu.usbmodemXXXX download motion.csv
```

On Windows activate with `.venv\Scripts\activate` and select e.g. `--port COM5`;
on Linux select e.g. `--port /dev/ttyACM0` (serial-device permissions may be needed).
If exactly one logger is detected, `--port` can be omitted. Close other serial
terminals before downloading. The tool refuses to overwrite an existing file,
checks every page's CRC and sample ordering, and publishes the CSV only after a
complete transfer. There is no USB mass-storage drive to eject.

Output:

```csv
session,time_us,ax_m_s2,ay_m_s2,az_m_s2,gx_rad_s,gy_rad_s,gz_rad_s
1,10000,0.000000,0.000000,9.806650,0.001500,-0.000200,0.000000
```

Acceleration is in **m/s^2**, including gravity, and angular rate is in **rad/s**,
both to six decimal places (this formatting does not imply that sensor precision).
Rows recorded by earlier (accelerometer-only) firmware have empty gyroscope fields. `time_us` is the software
capture time relative to that session's start, not UTC. Session numbers increase
across starts and reboots until erase. An empty session with no saved samples
need not survive a reboot. No real-time clock synchronization is implemented.

Status reports state, occupied/capacity pages, latest session number, invalid
page count, and the last runtime error. For example:

```text
STATUS stopped pages=20 capacity=1024 session=2 invalid=0 error=0
```

After an interrupted flash write, use
`download recovered.csv --skip-invalid` to explicitly salvage valid pages.
Every skipped page is reported; missing data is never silently repaired.

## Storage and sampling guarantees

- Reserves **4 MiB at external flash offsets 0x400000..0x7fffff**. The lower
  4 MiB is untouched by the logger. This layout is for this standalone image,
  not for combining with another application's partitions or OTA slots.
- Append-only, 4096-byte pages, with a versioned header, session ID, physical
  page index, 203 samples, and CRC32. Each 20-byte sample is a little-endian 64-bit
  microsecond timestamp plus three signed 16-bit accelerometer counts and three
  signed 16-bit gyroscope counts, stored raw; `tools/logger.py` scales them to
  m/s^2 and rad/s. Pages from earlier firmware (versions 1 and 2) remain readable. The full
  byte layout and rules for reading a store are in
  [docs/log-format.md](docs/log-format.md), with a machine-readable Kaitai Struct
  description in [docs/thingy53_log.ksy](docs/thingy53_log.ksy).
- Approximately **34.6 minutes** total at 100 Hz (**69 minutes** at 50 Hz) when
  pages are full. Each stop
  closes a page, so many short sessions reduce this duration.
- Data-ready interrupts drive acquisition, not an approximate software polling
  loop. Two 4 KiB RAM pages ping-pong: the accelerometer
  thread fills one while a separate lower-priority writer thread programs the
  other into flash, so flash latency never blocks sampling. Every sample carries
  its own microsecond timestamp, so any pause is visible in the data. A
  software-observed inter-sample gap over 15 ms (`-EOVERFLOW`), or a page that
  fills before the previous one finished writing (`-EBUSY`, no samples lost),
  stops recording with an error.
  This is **not a lossless FIFO capture implementation**: timestamps represent
  driver callback times and interrupt coalescing cannot always be detected.
  Validate actual timing under the intended hardware/power conditions.
- Pages are committed every 203 samples (about **2.03 seconds** at 100 Hz) and on stop.
  Sudden power loss can lose the RAM pages not yet written. Torn
  writes are CRC-rejected and their occupied slots are not reused. Always stop
  before removing power if all samples must be saved.
- Power loss during erase can leave an incompletely erased store. After reboot,
  repeat the full long-press erase before collecting new data.
- USB transfer is available only while stopped; a stalled transfer is canceled
  after 10 seconds without progress. Data is unencrypted and accessible to
  anyone with physical USB access.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests require Python and a host C compiler (`cc`, e.g. Xcode Command Line Tools
on macOS). They compile the production storage and button logic against a
simulated NOR flash and check debounce/hold thresholds, repeated sessions,
capacity, write/erase/read failures, and reboot recovery. The resulting binary
page is also decoded by the Python tool to verify format compatibility.
Host tests cover CRC rejection, session separation, partial serial reads,
interrupted downloads, explicit salvage, and output-file safety.
If this Mac's default macOS 27 SDK reports a linker/TAPI architecture mismatch,
use its installed SDK matching Xcode instead (without changing system settings):

```sh
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
MACOSX_DEPLOYMENT_TARGET=26.0 python3 -m unittest discover -s tests -v
```

Hardware verification is still required: repeated button toggles, five-second
hold without a release toggle, reboot persistence, unplug during recording and
download, full-store behavior, and measured 100 Hz timing.
