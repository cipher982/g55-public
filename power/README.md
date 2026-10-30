# BLE Power Telemetry & Control (Victron Orion-Tr Smart + JBD/Eco-Worthy LiFePO4 BMS)

Reusable protocol notes and reference Python codecs for reading and configuring 12 V auxiliary battery systems over Bluetooth Low Energy (BLE) without vendor GUI apps.

## Evidence Grades

- **Measured** — directly verified against hardware captures and live readback on an Orion-Tr Smart 12/12-30A Non-Isolated DC-DC charger (`0xA3C8`, firmware `v1.19`) and a 12 V 314 Ah LiFePO4 BMS.
- **Borrowed** — taken from published manufacturer documentation or open-source community references (cited below).
- **Provisional** — observed on one hardware/firmware revision; behavior on other models or firmware versions should be verified before use.

---

## 1. Victron Instant Readout BLE Advertisement (`0x02e1`)

**Grade:** Borrowed (Victron *Extra Manufacturer Data* specification; community reference [`keshavdv/victron-ble`](https://github.com/keshavdv/victron-ble)) and Measured (`0xA3C8` DC-DC charger).

Victron Smart devices broadcast encrypted telemetry in BLE Manufacturer Specific Data under company ID `0x02e1` (`737`):

| Byte Offset | Field | Description |
|---|---|---|
| `0` | Prefix | `0x10` (Instant Readout advertisement) |
| `1` | Frag / Reserved | `0x02` |
| `2..3` | Model ID (`uint16 LE`) | `0xA3C8` = Orion-Tr Smart 12/12-30A Non-Isolated |
| `4` | Readout Type (`uint8`) | `0x04` = DC-DC Converter / Charger |
| `5..6` | IV / Nonce (`uint16 LE`) | 16-bit counter placed in the first 2 bytes of a 16-byte AES-128-CTR little-endian counter block (remaining 14 bytes zero) |
| `7` | Key Check (`uint8`) | Must equal `key[0]` (first byte of the 16-byte device encryption key) |
| `8..N` | Ciphertext | AES-128-CTR encrypted payload |

### Decrypted Payload Layout for Readout Type `0x04` (DC-DC Charger)

| Byte / Bit Range | Field | Encoding |
|---|---|---|
| `byte 0` | Charger State (`uint8`) | `0`=Off, `1`=Low Power, `2`=Fault, `3`=Bulk, `4`=Absorption, `5`=Float, `6`=Storage, `7`=Equalize, `8`=Standby, `11`=Power Supply |
| `byte 1` | Charger Error (`uint8`) | `0` = No error |
| `bytes 2..3` (`uint16 LE`) | Input Voltage | `0.01 V/count` (`0xFFFF` = unavailable) |
| `bytes 4..5` (`int16 LE`) | Output Voltage | `0.01 V/count` (`0x7FFF` or `0xFFFF` = unavailable / off) |
| `bytes 6..9` (`uint32 LE`) | Off-Reason Bitmask | `0x01`=No input power, `0x02`=Switched off (remote bridge), `0x04`=Switched off (register/BLE), `0x08`=Remote hibernate, `0x10`=Protection active, `0x20`=Paygo, `0x40`=BMS shutdown, `0x80`=Engine shutdown detected, `0x100`=Analyzing input voltage |

---

## 2. Victron Orion-Tr Smart BLE GATT Register Protocol (`306b0001-…`)

**Grade:** Measured (captured from an authenticated GATT session with an Orion-Tr Smart 12/12-30A `0xA3C8` on firmware `v1.19` and verified by write + readback).

While Victron documents passive BLE advertisements, changing engine-shutdown thresholds or toggling remote standby over BLE uses an undocumented GATT tunnel service:

- **Service UUID:** `306b0001-b081-4037-83dc-e59fcc3cdfd0`
- **Session Control / Keepalive Characteristic:** `306b0002-b081-4037-83dc-e59fcc3cdfd0`
- **Register Read / Write / Notify Characteristic:** `306b0003-b081-4037-83dc-e59fcc3cdfd0`
- **Bulk Stream Characteristic:** `306b0004-b081-4037-83dc-e59fcc3cdfd0`

> **Prerequisite (Measured):** The BLE host must hold an OS-level bond (paired with the device's 6-digit PIN) so the encrypted GATT characteristics permit reads/writes.

### Session Handshake & Keepalive

1. Enable notifications on `306b0002-…`, `306b0003-…`, and `306b0004-…`.
2. Write `05 00 81 19` to `306b0002-…` (keepalive; repeat every ~3 s during an active session).
3. Write `04 00 01` to `306b0002-…` to start the register notification stream on `306b0003-…`.
4. Send a periodic `02 41` ping on `306b0003-…` if the session remains open longer than 10 s; without keepalives the charger closes the GATT link after ~45 s.

### Register Frame Formats on `306b0003-…`

- **Read Request (Host -> Charger):**
  ```text
  06 00 00 19 <reg_msb> <reg_lsb>
  ```
- **Register Report / Write Request (Bidirectional):**
  ```text
  08 00 19 <reg_msb> <reg_lsb> <type_code> <value_le_bytes...>
  ```
  where `<type_code>` is `0x41` for a 1-byte (`uint8`) register and `0x42` for a 2-byte (`uint16 LE`) register. When the host writes an `08 00 19 ...` frame to `306b0003-…`, the charger applies the setting (clamping to its internal hardware bounds if out of range) and echoes the updated `08 00 19 ...` frame on `306b0003-…`.

### Measured Register Map (Orion-Tr Smart 12/12-30A `0xA3C8`, fw `v1.19`)

| Register (`BE16`) | Name | Type | Scale | Measured Hardware Bounds | Notes |
|---|---|---|---|---|---|
| `0x0200` | `device_mode` | `uint8` (`0x41`) | enum | `1` = Charger On, `4` = Software Standby / Off | Toggles charging in software without pulling the physical `L-H` bridge |
| `0x0201` | `charger_state` | `uint8` (`0x41`) | enum | Read-only | Same state enum as Instant Readout byte 0 |
| `0x0207` | `off_reason_mask` | `uint32` | bitmask | Read-only | Bit `0x80` = Engine shutdown; Bit `0x04` = Software standby (`0x0200=4`) |
| `0xED8D` | `output_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | Read-only | Auxiliary battery side voltage |
| `0xED8E` | `input_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | Read-only | Starter battery / alternator side voltage |
| `0x10EC` | `input_lockout_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | `11.50 V .. 13.00 V` (`1150..1300`) | Below this input voltage, charging stops immediately. Values `< 11.50 V` are clamped to `11.50 V` by the charger |
| `0x10ED` | `input_restart_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | `12.00 V .. 13.50 V` (`1200..1350`) | Input voltage required to clear input lockout |
| `0x10EF` | `engine_shutdown_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | `12.00 V .. 14.00 V` (`1200..1400`) | Below this input voltage, alternator is considered off |
| `0x10F0` | `engine_start_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | `12.20 V .. 14.50 V` (`1220..1450`) | Immediate engine-running detection voltage |
| `0x10F1` | `engine_delayed_start_voltage` | `uint16 LE` (`0x42`) | `0.01 V` | `12.20 V .. 14.40 V` (`1220..1440`) | Delayed engine-running threshold (must remain `>= engine_shutdown_voltage`) |
| `0x10F2` | `engine_delayed_start_delay` | `uint16 LE` (`0x42`) | `1 s` | `0 s .. 3600 s` | Delay before starting charge above `0x10F1` |

---

## 3. JBD / Eco-Worthy 12 V LiFePO4 BMS BLE Protocol (`0xfff0`)

**Grade:** Measured (verified on a 12 V 314 Ah 4S LiFePO4 smart BMS).

- **Service UUID:** `0000fff0-0000-1000-8000-00805f9b34fb`
- **Notify Characteristic:** `0000fff1-0000-1000-8000-00805f9b34fb`
- **Write Characteristic:** `0000fff2-0000-1000-8000-00805f9b34fb`
- **Poll Command (optional):** `dd 02 00 01 00 60 64`

When connected (or polled), `0xfff1` emits two frames:
- **`0xA1` (29 bytes):** Pack metadata and rated capacity (`bytes 10..11` `uint16 BE`, Ah).
- **`0xA2` (36 bytes):** Real-time pack and cell telemetry protected by a trailing **CRC-16/MODBUS** (`poly=0xA001`, `init=0xFFFF`, little-endian in `bytes 34..35`):
  - `bytes 14..15` (`uint16 BE`): State of Charge (`0.1 %/count`)
  - `bytes 16..17` (`uint16 BE`): Pack Voltage (`0.01 V/count`)
  - `bytes 18..19` (`int16 BE`): Pack Current (`0.1 A/count`, positive = charging, negative = discharging)
  - `bytes 20..21` (`uint16 BE`): Problem / protection code (`0` = normal)
  - `bytes 22..23` (`uint16 BE`): State of Health (`0.1 %/count`)
  - `bytes 2..9` (`4 x uint16 BE`): Cell 1..4 voltages (`1 mV/count`)
  - `bytes 26..33` (`4 x int16 BE`): Temperature sensors (`0.1 °C/count`)

---

## Running the Reference Tests

```bash
pytest public/power/test_ble_power.py
```
