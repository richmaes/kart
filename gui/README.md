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
- Compare manually selected laps by elapsed time or normalized lap progress.
- Estimate an experimental apparent path from acceleration and gyroscope samples.
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
6. **Compare laps**: open **Lap comparison**, click **Select on time series**, then
   switch to **Time series** and drag across a lap. Add the selected range, or
   enter start/end times directly. Select a lap to edit its boundaries; double-click
   its Overlay cell to include or exclude it from the comparison. Included laps
   appear as individual traces, with a median typical-lap trace and a 25–75%
   variability band. Choose **Elapsed time** to preserve real timing differences,
   or **Lap progress** to align each lap from 0–100%.

Lap boundaries are manual and apply to the selected recording session. The table
flags laps touching a recording edge, containing sample gaps over 15 ms, or
lasting more than 20% away from the median when at least three laps are defined.
These are review hints, not automatic judgments that a lap is invalid; flagged
laps remain available for inspection and comparison. Lap boundaries are not saved
with the CSV. Automatic lap detection and accurate physical course reconstruction
are not implemented. Lap comparison currently uses the calibrated and smoothed
acceleration traces; gyroscope data is not used.

The GUI accepts logger page format versions 1..4 and five-, eight-, and
nine-column CSVs. Downloads and CSV saves preserve available gyro and BMI270 die
temperature values; missing legacy values stay blank in mixed recordings.
Acceleration plots and lap comparison still use acceleration only.

## Experimental apparent path

1. Record with the sensor **already rigidly mounted on the kart**. Keep the kart
   stationary, with zero velocity and no rotation, for the first **3 seconds**
   after starting recording (or the interval entered in the path tab). Then drive.
   A six-face calibration recording is not suitable for a path estimate.
2. Download a gyro-enabled v2/v3/v4 recording, select its session and the appropriate
   saved accelerometer calibration, and set the device **Forward axis** and
   **Yaw trim**. These determine the initial relative heading, not compass north.
3. Open **Apparent path (experimental)**. Configure the signed gyro axis supplying
   each accelerometer axis component. The selected mapping is assumed correct
   without a confirmation checkbox. Defaults are **Accel X = gyro -Y,
   Accel Y = gyro +X, Accel Z = gyro +Z**, inferred by comparing integrated gyro
   orientation with measured gravity across local recordings. This is not yet
   confirmed against the board's physical axis documentation; keep the controls
   editable for other hardware. Matching X/Y/Z between these separate chips
   can turn actual yaw into apparent tilt and produce a severely distorted path.
   The mapping must use every axis once and preserve right-handedness.
4. Click **Estimate apparent path**. Stationary, zero-velocity initialization and
   rigid mounting are assumed. Changes to the mapping or calibration/orientation
   settings clear the estimate; regenerate it.

**Startup settling seconds** defaults to **1 second**. This excludes the button
press/settling transient from the gravity, tilt and gyro-bias fit: with the default
3-second stationary interval, the fit uses **1–3 seconds**. Keep the kart stationary
for the entire interval. Settling must be shorter than the stationary interval;
set it to zero to use all initial samples. Error messages show the measured
variation and the fitting window. This setting does not filter noise during driving.

The estimator uses the post-settling stationary samples to estimate gravity, tilt and
gyro bias, holds velocity and position at zero during the entire initial interval, then
integrates three-dimensional orientation from gyro samples, removes gravity from
acceleration, and integrates velocity and position with actual timestamp intervals.
It uses sensor-calibrated acceleration, not the Display smoothing setting.
The plot shows horizontal displacement relative to the initial heading, colored
by time. No lap closure, final zero-speed constraint, or GPS correction is applied.

**Orientation estimator** defaults to **Cautious tilt fusion**; choose **Gyro only**
to compare with the original integration. Fusion gently corrects roll/pitch toward
measured gravity with a 5-second time constant, after 0.5 seconds of continuously
acceptable observations. It uses a trailing 0.5-second acceleration window and
rejects corrections if any of these hold:

- Acceleration magnitude differs from the initial gravity magnitude by more than 3%.
- Acceleration vector noise exceeds 0.02 g.
- Bias-corrected angular rate exceeds 0.1 rad/s.
- Measured and predicted gravity directions disagree by more than about 4.6 degrees.

The current sample is checked too, so sudden braking, cornering or impacts suspend
correction immediately. The status shows how many correction updates were applied.
These are conservative heuristics, not proof of rest: smooth sustained acceleration
can still resemble tilt. Large tilt drift will not be recovered automatically.
Gravity supplies no heading reference, so **yaw drift remains uncorrected**.
Fusion does not update gyro bias or reset accumulated velocity/position.

**This is not a measured track, racing line, speed or distance measurement.**
Even 0.01 g of constant acceleration error causes about 4.9 m of position error
in 10 seconds; gyro drift and tilt errors make it worse. Use short runs beginning
at rest. Quiet constant motion cannot be distinguished from rest by an IMU, so
the initial zero-velocity assumption must actually be true. The estimator rejects
missing/non-finite gyro data, non-increasing timestamps, gaps over 15 ms, incomplete
initial intervals, and initial noise above 0.05 g or gyro variation above
0.03 rad/s. It also rejects initial gravity magnitudes more than 10% from 1 g.
Those checks do not prove the mounting or stationary assumptions.

Older GUI downloads discarded gyro data. They cannot be used for this feature
unless re-downloaded from the device (if still stored), or replaced with an
gyro-enabled CSV from `../tools/logger.py`. Accelerometer-only CSVs still work with
the other GUI views.

### TODO: motion-estimation improvements

1. [x] Add cautious roll/pitch fusion, with a gyro-only comparison mode.
   Tests cover small roll/pitch drift reduction, preservation of yaw, rejection
   during acceleration/braking/cornering/impacts/vibration, and ideal circular motion.
   Real kart validation remains required.
2. [x] Longer explicit stationary gyro measurements, per-axis bias/noise reporting,
   selectable startup windows, and **user-marked** subsequent bias updates.
   Tests cover bias estimation, motion rejection, time-varying bias, and no
   retrospective correction or velocity reset. Quiet data alone never adds updates.
3. [x] Investigate offset compensation, CRT, driver behavior and temperature support.
   Application-local read-only BMI270 temperature acquisition and v4 recording
   are implemented and build-tested; host SPI tests cover register/dummy bytes,
   delay, sentinel and errors. Hardware temperature/timing verification remains
   required. Offset/CRT/NVM commands remain disabled (see below).
4. [ ] **Capture and validate real multi-temperature measurements on hardware.**
   Collection, per-unit persistence, bounded piecewise-linear compensation and
   tests are implemented. Tests cover insufficiency, interpolation, missing/out-of-range
   temperatures, axis mapping, and avoiding double bias subtraction. There is no
   factory/default curve and no claim of measured repeatability yet.

### Gyro / temperature calibration

1. Keep the mounted unit motionless on a rigid surface. Let button transients settle;
   record **15–20 seconds or longer** at rest. Use the top USER button to start/stop,
   download, and open **Gyro / temperature calibration…**. Erasing is not required
   unless storage is full; back up recordings before any erase.
2. Enter a known stationary interval, e.g. **1–16 s**. Times are relative to the
   **first sample**, not the recording's absolute `time_us`. **Measure interval**
   reports native gyro XYZ bias and per-axis standard deviation in rad/s, sample
   count and mean BMI270 die temperature. No noise filter is added.
3. **Use for startup fit** selects that interval in the path settings. The entire
   recording from first sample through End must have been at rest, zero velocity,
   and in the final mounting orientation; the selected window fits bias/gravity.
   Otherwise keep the default startup window and use only a later known-rest interval.
4. **Use as bias-update interval** adds a known-rest interval after initialization.
   Each interval must be at least 10 s, non-overlapping, and have subsequent samples.
   The measured bias replaces the previous bias **after that interval ends**, never
   retrospectively; orientation, yaw, velocity and position are not reset.
   Intervals are in memory per session, survive switching sessions, and reset on
   loading another recording. **Clear session bias updates** removes them.
5. **Save per-unit measurement** stores bias/noise/time/count/temperature keyed by
   the recording's USB serial, separately from accelerometer calibration, in
   `gyro-calibration.json` (ignored by Git). Legacy gyro data can produce a bias
   measurement but cannot produce a temperature calibration point. A recording
   without unit metadata cannot save/apply a per-unit gyro model; re-download or
   restore its correct metadata. Saved measurements are observations, not an
   automatically applied bias; startup fitting and marked intervals determine bias.

Intervals reject insufficient duration, invalid timing or >15 ms gaps, missing
sensor samples, acceleration vector noise >0.02 g, gravity error >10%, gyro
vector noise >0.03 rad/s, or first-half/second-half gyro mean changes >0.01 rad/s.
Temperature-bearing intervals require all readings and <=1°C total temperature
change. These gates cannot distinguish smooth motion/constant rotation from
physical rest: **the user's stationary assertion is essential**.

For temperature compensation, repeat stationary measurements at **at least three
distinct, stabilized die temperatures**, spanning **at least 5°C** and separated
by **at least 0.5°C**. Natural warm-up/ordinary ambient changes are preferable;
do not heat or cool the battery outside the device's rated operating conditions.
Record repeated points on different runs to assess repeatability before trusting
a model. Delete an unsuitable/repeated-temperature point from the dialog before fitting.

**Fit / save temperature model** stores a piecewise-linear interpolation of native
gyro bias versus measured die temperature; it does not invent slopes or extrapolate.
Choose **Temperature compensation: Saved per-unit model** in the path tab to apply
it (default **Off**). Every sample must have a temperature inside the calibrated
range; legacy/missing/out-of-range data produces an explicit error. Correction
occurs in native gyro axes before mapping. Startup and later marked intervals then
estimate the **remaining residual bias**, avoiding subtraction of the same offset
twice. Adding observations leaves the last fitted model intact until refitting;
deleting an observation invalidates it. This cannot remove earlier accumulated yaw
or position error, nor guarantee an accurate track.

### BMI270 hardware calibration investigation

The Thingy:53 board DTS contains a BME688 environmental sensor and a BMI270 IMU.
The BMI270 internal temperature is relevant to its gyro bias. In NCS v3.4.1,
Zephyr's BMI270 driver fetches acceleration/gyro but returns `-ENOTSUP` for
temperature; it disables advanced power saving and enables the configured gyro.
The logger's local SPI helper reads `0x22..0x23` using the driver's SPI framing
and 2 us post-access delay, without editing the SDK or changing sensor settings.
With the active gyro, temperature updates about every 10 ms (+/-12%).
`Celsius = 23 + raw / 512`; `0x8000` is invalid and explicitly stops acquisition.
Firmware v4 records this raw temperature alongside accel/gyro (184 samples/page);
v1..v3 remain readable without erase. Live temperature and timing are not yet
validated on this unit.

The [Bosch datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi270-ds000.pdf)
(sections 4.6.14 and 4.13) describes writable gyro offset registers, fast offset
compensation, and Component ReTrimming (CRT). **CRT corrects sensitivity/scale,
not the zero-rate offset or accumulated heading drift.** It requires a specific
configuration-upload/command/status sequence and physical rest. The installed
driver exposes neither offset/CRT attributes nor a temperature channel; simply
setting an undocumented application attribute is not a solution. The
[official Bosch API](https://github.com/boschsensortec/BMI270_SensorAPI) provides
procedures but is not part of this application's driver.
This implementation deliberately keeps offset/CRT/NVM writes disabled and
uses auditable host-side measured corrections. Enabling hardware calibration
requires a separately verified driver/procedure and on-device scale/offset tests.

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

Open **Calibration checklist…** in the Sensor calibration panel for checkable
reminders. It is available before loading data; checks are reminders only, not
device verification, and reset when the checklist is reopened. It calibrates
acceleration, not the gyroscope.

1. Power on and confirm stopped/blue, with no red error. Use the top **USER**
   button, not the separate reset/bootloader button.
2. **Erasing is not normally required:** calibration appends a new session.
   Check Status for free space. If storage is full (`-ENOSPC`), download/back up
   important recordings first, then hold the USER button for **5 seconds** and
   release to erase **all** stored recordings. Keep power connected while blue
   blinks (up to a minute), and wait for three quick blue flashes. The device
   remains stopped; erase cannot be undone.
3. Briefly press and release the USER button to start a **new** session; confirm
   green. On a stable, flat surface, rest the unit on **all six faces**, including
   each pair of opposite faces. Let it settle after every turn, then keep it
   completely still for **at least 5 seconds per face**.
4. Briefly press and release again to stop and flush; confirm blue/stopped.
   If red appears, inspect Status and resolve the error before retrying.
5. Connect a USB data cable, download, and select the **new calibration session**
   (the GUI initially selects the longest session). Download does not erase data.
   Open **Calibrate / check from this session…**. Still
   segments are found and their faces identified automatically.
6. Confirm all six distinct faces are detected. Press **Fit**, enter the unit
   serial and name, and **Save to calibration file**.

With 6 or more orientations the fit is a sphere fit: every corrected reading must
read 1 g. That makes it insensitive to surface tilt. Three faces (+X, +Y, +Z up)
also work but assume level faces, which costs about 12 mg of offset and 1.3 % of
gain per degree of tilt.

To check a calibration, record a separate short session with 2 or 3
orientations, for example as installed and upside down, settling and holding
still for at least 5 seconds in each. Stop, download, select that new session,
and open the same dialog
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
- `kartlog/path.py` – experimental gyro orientation and acceleration dead reckoning
- `kartlog/gyrocal.py` – explicit stationary bias/noise measurements, bounded temperature model and per-unit store
- `kartlog/sensorcal.py` – per-unit offset/gain fit (sphere or face), check, `calibration.json` store, recording metadata
- `kartlog/synth.py` – synthetic kart session (**Demo data** button, tests)
- `kartlog/gui.py` – Tkinter + matplotlib UI
