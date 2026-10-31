meta:
  id: thingy53_log_page
  title: Thingy:53 accelerometer logger flash page
  endian: le
doc: |
  One 4096-byte page of the accelerometer log as stored in the external QSPI
  flash partition (and as sent by the DUMP command). See log-format.md for the
  rules that apply across pages. An all-0xff page is an unwritten slot and does
  not parse as this type; test for it before parsing. Parse each page with
  `size: 4096`, and verify `crc32` against the CRC-32/IEEE of the first 4092
  bytes before trusting the content.
  Version 3 pages (current) carry accelerometer and gyroscope samples as raw
  16-bit counts; scale them as described in log-format.md. Version 2 (converted
  32-bit values) and version 1 (accelerometer only) pages were written by older
  firmware and may share the same store; the `version` field selects the layout.
seq:
  - id: header
    type: header
  - id: samples
    type:
      switch-on: header.version
      cases:
        1: sample_v1
        2: sample_v2
        3: sample_v3
    repeat: expr
    repeat-expr: header.count
  - id: unused
    size: unused_size
    doc: Unused sample slots and padding, left erased (0xff).
  - id: crc32
    type: u4
    doc: CRC-32/IEEE (zlib.crc32) of the preceding 4092 bytes of the page.
instances:
  unused_size:
    value: 'header.version == 2 ? 28 + (126 - header.count) * 32 : (203 - header.count) * 20'
types:
  header:
    seq:
      - id: magic
        type: u4
        valid: 0x33474c41
        doc: Bytes 41 4c 47 33 ("ALG3").
      - id: version
        type: u2
        valid:
          any-of: [1, 2, 3]
      - id: count
        type: u2
        valid:
          min: 1
          max: 203
        doc: Number of valid samples in this page (at most 203 for versions 1 and 3, 126 for version 2).
      - id: session
        type: u4
        valid:
          min: 1
        doc: Recording session. Increases by one each time recording starts.
      - id: page_index
        type: u4
        doc: Physical slot number; equals the page's position in the partition.
      - id: sample_rate_hz
        type: u4
        valid: 100
      - id: range_g
        type: u4
        valid: 4
        doc: Accelerometer full-scale range, +/- g.
      - id: gyro_range_dps
        type: u4
        doc: |
          Gyroscope full-scale range, +/- deg/s (2000). Versions 2 and 3; in a
          version 1 page these four bytes are erased (0xffffffff).
      - id: accel_counts_per_g
        type: u2
        doc: Accelerometer counts per g (500). Version 3; erased (0xffff) before.
      - id: reserved
        size: 2
        doc: Left erased (0xff).
  sample_v1:
    seq:
      - id: time_us
        type: u8
        doc: |
          Microseconds since this session started, taken when the sensor
          callback ran. Strictly increasing within a session.
      - id: ax_micro
        type: s4
        doc: X acceleration in micro-m/s^2 (divide by 1e6 for m/s^2).
      - id: ay_micro
        type: s4
      - id: az_micro
        type: s4
  sample_v2:
    seq:
      - id: time_us
        type: u8
        doc: |
          Microseconds since this session started, taken when the sensor
          callback ran. Strictly increasing within a session.
      - id: ax_micro
        type: s4
        doc: X acceleration in micro-m/s^2 (divide by 1e6 for m/s^2).
      - id: ay_micro
        type: s4
      - id: az_micro
        type: s4
      - id: gx_micro
        type: s4
        doc: X angular rate in micro-rad/s (divide by 1e6 for rad/s).
      - id: gy_micro
        type: s4
      - id: gz_micro
        type: s4
  sample_v3:
    seq:
      - id: time_us
        type: u8
        doc: |
          Microseconds since this session started, taken when the sensor
          callback ran. Strictly increasing within a session.
      - id: ax_raw
        type: s2
        doc: X acceleration in raw counts; divide by accel_counts_per_g for g.
      - id: ay_raw
        type: s2
      - id: az_raw
        type: s2
      - id: gx_raw
        type: s2
        doc: X angular rate in raw counts; multiply by gyro_range_dps / 32768 for deg/s.
      - id: gy_raw
        type: s2
      - id: gz_raw
        type: s2
