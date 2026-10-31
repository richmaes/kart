# Go-kart accelerometer analyzer

Desktop GUI for the Thingy:53 logger firmware in the parent directory. It
downloads recorded sessions over USB, or opens a CSV saved by
`../tools/logger.py`. It then rotates the data into the kart's frame and plots it.

## Running

From this directory (`gui/`):

```sh
python3 -m pip install -r requirements.txt
python3 -m kartlog                            # start the GUI
python3 -m unittest discover -s tests -t .    # run the GUI tests
```

Dependencies are NumPy, Matplotlib, and pyserial; Tk ships with Python. Close
any other serial terminal before downloading.

## Features

- Download sessions directly from the logger over USB, with CRC validation.
- Open and save logger-compatible CSV files.
- Calibrate each sensor by USB serial number and apply per-recording installation
  references.
- Plot kart-frame acceleration, timing, and G-G data.
- Sampling rates can differ from the nominal rate; analysis uses the recorded
  timestamps.

Downloads and calibration are stored locally in `recordings/` and
`calibration.json`. These device-specific files are intentionally excluded from
version control.

## Workflow

1. **Download**: stop the logger, plug in USB, pick the port (it is auto-selected
   when it reports as *Thingy53 Accelerometer Logger*) and press **Download**.
   The transfer is read-only and validates CRCs like `logger.py`. Every download
   is saved to `recordings/kart-YYYYMMDD-HHMMSS.csv` in the logger's CSV format.
   Use **Open CSV…** to reload a saved file.
2. **Session**: pick one from the drop-down. The longest session is selected by default.
3. **Reference window**: a stretch where the kart is stationary and level. When
   a session loads, the first still stretch is found automatically (up to 3 s).
   You can also type the start and duration, press **Auto-find rest**, or press
   **Pick window on plot** and drag across the top plot. The mean acceleration
   there defines "up" (1 g). The panel shows the vector, |g|, noise and the
   device tilt, and warns when the window looks like the kart was moving. With
   **Scale so reference reads 1 g** on (the default), every reading is scaled by
   1/|g_ref| to cancel the sensor's gain error.
4. **Mounting**: choose which device axis points roughly forward. It is projected
   onto the level plane, so a tilted mount is fine. **Auto yaw from launch** finds
   the first sustained acceleration after the reference window. It assumes that
   is a straight-line pull-away and sets the yaw trim so that direction becomes
   +longitudinal. You can also adjust the trim by hand.
5. **Analyze**: zooming or panning the time-series updates the G-G
   (friction-circle) plot and the peak summary to the visible time range. Use
   this to look at a single lap or corner.

The GUI accepts logger page format versions 1, 2, and 3, and can open both the
GUI's accelerometer-only CSV files and the logger tool's CSV files with
gyroscope columns. It analyzes acceleration; gyroscope values are currently
ignored.

## Sensor calibration (once per unit)

The firmware logs raw readings; all correction happens here. There are two layers:

1. **Sensor calibration**: a per-axis offset and gain for each unit, stored in
   `calibration.json` and keyed by USB serial number. You can point to another
   file with `KARTLOG_CONFIG`. Every download writes `<csv>.meta.json` with the
   unit serial, so the right calibration is applied when the CSV is opened. A CSV
   without metadata uses the only stored unit, if there is just one.
2. **Installation reference** (per recording): after sensor correction, the
   reference window defines "up", so a mount that is not quite square is fine.

To calibrate:

1. Record one session on a flat surface. Rest the unit **still on each of its
   6 faces for about 5 s**, handling it between faces.
2. Download it, then open **Calibrate / check from this session…**. Still
   segments are found and their faces identified automatically.
3. Press **Fit**, enter a name and **Save**.

With 6 or more orientations the fit is a sphere fit: every corrected reading must
read 1 g. That makes it insensitive to surface tilt. Three faces (+X, +Y, +Z up)
also work but assume level faces, which costs about 12 mg of offset and 1.3 % of
gain per degree of tilt.

To check a calibration, record a separate short session with 2 or 3
orientations, for example as installed and upside down. Open the same dialog
with the unit's calibration selected. Each orientation's **|g| corrected** should
read 1.000; a few mg is good. A 6-face fit always matches its own six faces, so
it cannot check itself.

## Conventions

Kart frame (right-handed): **longitudinal** + = accelerating, − = braking;
**lateral** + = turning left, − = turning right; **vertical** reads +1 g at rest.
The accelerometer reports specific force, so cornering shows up as the
centripetal direction (a right-hander gives negative lateral). Smoothing is a
centered moving average. Its window length uses the sample rate measured from the
timestamps. Kart engines vibrate hard and the logger may sample below 100 Hz, so
the unsmoothed trace can be dominated by aliased vibration.

With no gyroscope, tilt relative to the reference cannot be told apart from
acceleration. For example, 5° of chassis pitch shows up as about 0.09 g of
longitudinal acceleration. A longitudinal or lateral offset that lasts for many
seconds is tilt, not motion.

## Layout

- `kartlog/device.py` – serial protocol and page decoder (mirrors `../tools/logger.py`)
- `kartlog/data.py` – sessions, CSV load/save, gap detection
- `kartlog/analysis.py` – still-segment finder, installation reference, kart-frame rotation, launch-yaw estimate, peaks
- `kartlog/sensorcal.py` – per-unit offset/gain fit (sphere or face), check, `calibration.json` store, recording metadata
- `kartlog/synth.py` – synthetic kart session (**Demo data** button, tests)
- `kartlog/gui.py` – Tkinter + matplotlib UI
