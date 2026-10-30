#!/usr/bin/env python3
"""Reference codecs for Victron Orion-Tr Smart BLE (Instant Readout + GATT registers)
and JBD / Eco-Worthy 12 V LiFePO4 BMS notifications.

License: MIT (see ../LICENSE).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

VICTRON_GATT_SERVICE = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
VICTRON_GATT_KEEPALIVE_CHAR = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
VICTRON_GATT_REG_CHAR = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
VICTRON_GATT_STREAM_CHAR = "306b0004-b081-4037-83dc-e59fcc3cdfd0"

KEEPALIVE_PACKET = bytes.fromhex("05008119")
SUBSCRIBE_PACKET = bytes.fromhex("040001")
PING_PACKET = bytes.fromhex("0241")

VICTRON_CHARGER_STATES = {
    0: "off",
    1: "low_power",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    6: "storage",
    7: "equalize",
    8: "standby",
    11: "power_supply",
    245: "starting",
    252: "external_control",
}

VICTRON_OFF_REASONS = {
    0x00000001: "no_input_power",
    0x00000002: "switched_off_switch",
    0x00000004: "switched_off_register",
    0x00000008: "remote_input",
    0x00000010: "protection_active",
    0x00000020: "paygo",
    0x00000040: "bms",
    0x00000080: "engine_shutdown",
    0x00000100: "analysing_input_voltage",
}


@dataclass(frozen=True)
class OrionRegisterSpec:
    name: str
    reg: int
    width: int  # 1 (0x41 uint8) or 2 (0x42 uint16 LE)
    scale: float
    unit: str
    min_val: float
    max_val: float
    writable: bool = True


ORION_REGISTERS: dict[str, OrionRegisterSpec] = {
    "device_mode": OrionRegisterSpec("device_mode", 0x0200, 1, 1.0, "mode", 1.0, 4.0, True),
    "charger_state": OrionRegisterSpec("charger_state", 0x0201, 1, 1.0, "state", 0.0, 255.0, False),
    "output_voltage": OrionRegisterSpec("output_voltage", 0xED8D, 2, 0.01, "V", 0.0, 655.35, False),
    "input_voltage": OrionRegisterSpec("input_voltage", 0xED8E, 2, 0.01, "V", 0.0, 655.35, False),
    "input_lockout_voltage": OrionRegisterSpec("input_lockout_voltage", 0x10EC, 2, 0.01, "V", 11.50, 13.00, True),
    "input_restart_voltage": OrionRegisterSpec("input_restart_voltage", 0x10ED, 2, 0.01, "V", 12.00, 13.50, True),
    "engine_shutdown_voltage": OrionRegisterSpec("engine_shutdown_voltage", 0x10EF, 2, 0.01, "V", 12.00, 14.00, True),
    "engine_start_voltage": OrionRegisterSpec("engine_start_voltage", 0x10F0, 2, 0.01, "V", 12.20, 14.50, True),
    "engine_delayed_start_voltage": OrionRegisterSpec(
        "engine_delayed_start_voltage", 0x10F1, 2, 0.01, "V", 12.20, 14.40, True
    ),
    "engine_delayed_start_delay": OrionRegisterSpec(
        "engine_delayed_start_delay", 0x10F2, 2, 1.0, "s", 0.0, 3600.0, True
    ),
}

ORION_REG_BY_ID: dict[int, OrionRegisterSpec] = {s.reg: s for s in ORION_REGISTERS.values()}


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS (polynomial 0xA001, init 0xFFFF, little-endian on wire)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


def parse_jbd_bms_a2(frame: bytes) -> dict[str, Any] | None:
    """Decode a 36-byte 0xA2 status notification from a JBD / Eco-Worthy 4S LiFePO4 BMS."""
    if len(frame) < 36 or frame[0] != 0xA2:
        return None
    expected_crc = int.from_bytes(frame[34:36], "little")
    if crc16_modbus(frame[:34]) != expected_crc:
        return None
    soc_pct = round(struct.unpack(">H", frame[14:16])[0] / 10.0, 1)
    soh_pct = round(struct.unpack(">H", frame[22:24])[0] / 10.0, 1)
    voltage_v = round(struct.unpack(">H", frame[16:18])[0] / 100.0, 2)
    current_a = round(struct.unpack(">h", frame[18:20])[0] / 10.0, 2)
    problem_code = struct.unpack(">H", frame[20:22])[0]
    cells_v = [round(struct.unpack(">H", frame[2 + i * 2 : 4 + i * 2])[0] / 1000.0, 3) for i in range(4)]
    temps_raw = [round(struct.unpack(">h", frame[26 + i * 2 : 28 + i * 2])[0] / 10.0, 1) for i in range(4)]
    temps_c = [t for t in temps_raw if -40.0 < t < 100.0]
    return {
        "soc_pct": soc_pct,
        "soh_pct": soh_pct,
        "voltage_v": voltage_v,
        "current_a": current_a,
        "power_w": round(voltage_v * current_a, 1),
        "problem_code": problem_code,
        "cells_v": cells_v,
        "cell_min_v": min(cells_v),
        "cell_max_v": max(cells_v),
        "cell_delta_mv": round((max(cells_v) - min(cells_v)) * 1000),
        "temps_c": temps_c,
        "temp_c": temps_c[1] if len(temps_c) > 1 else (temps_c[0] if temps_c else None),
    }


def aes128_ctr_decrypt(key: bytes, iv_le16: int, ciphertext: bytes) -> bytes:
    """Decrypt Victron Instant Readout ciphertext using AES-128-CTR with a 16-bit LE nonce."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    counter_block = iv_le16.to_bytes(2, "little") + b"\x00" * 14
    cipher = Cipher(algorithms.AES(key), modes.CTR(counter_block))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def decode_victron_instant_readout(mfg_data: bytes, key_hex: str) -> dict[str, Any] | None:
    """Decode Victron ManufacturerData (company ID 0x02e1 stripped) for a DC-DC charger (type 0x04)."""
    if len(mfg_data) < 14 or mfg_data[0] != 0x10:
        return None
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError("Victron advertisement key must be 16 bytes (32 hex chars)")
    model_id = struct.unpack("<H", mfg_data[2:4])[0]
    readout_type = mfg_data[4]
    iv = struct.unpack("<H", mfg_data[5:7])[0]
    if mfg_data[7] != key[0] or readout_type != 0x04:
        return None
    plain = aes128_ctr_decrypt(key, iv, mfg_data[8:])
    if len(plain) < 10:
        return None
    state_code = plain[0]
    error_code = plain[1]
    input_v_raw = struct.unpack("<H", plain[2:4])[0]
    output_v_raw = struct.unpack("<h", plain[4:6])[0]
    off_reason_mask = struct.unpack("<I", plain[6:10])[0]
    return {
        "model_id": f"0x{model_id:04x}",
        "state_code": state_code,
        "state": VICTRON_CHARGER_STATES.get(state_code, f"unknown_{state_code}"),
        "error": error_code,
        "input_v": round(input_v_raw / 100.0, 2) if input_v_raw != 0xFFFF else None,
        "output_v": round(output_v_raw / 100.0, 2) if output_v_raw not in (0x7FFF, -1) else None,
        "off_reason_code": off_reason_mask,
        "off_reasons": [name for bit, name in VICTRON_OFF_REASONS.items() if off_reason_mask & bit],
    }


def build_orion_get_packet(reg: int) -> bytes:
    """Build a GATT register read request (`06 00 00 19 <reg_be16>`) for `306b0003-…`."""
    return b"\x06\x00\x00\x19" + reg.to_bytes(2, "big")


def build_orion_set_packet(spec: OrionRegisterSpec, value: float) -> bytes:
    """Build a GATT register write request (`08 00 19 <reg_be16> <type> <val_le>`) for `306b0003-…`."""
    if not spec.writable:
        raise ValueError(f"Register {spec.name} (0x{spec.reg:04X}) is read-only")
    if not (spec.min_val - 1e-6 <= value <= spec.max_val + 1e-6):
        raise ValueError(f"{spec.name}={value} outside [{spec.min_val}, {spec.max_val}] {spec.unit}")
    raw_int = int(round(value / spec.scale))
    type_byte = 0x41 if spec.width == 1 else 0x42
    return b"\x08\x00\x19" + spec.reg.to_bytes(2, "big") + bytes([type_byte]) + raw_int.to_bytes(spec.width, "little")


def parse_orion_gatt_notification(payload: bytes) -> dict[int, int]:
    """Extract `{reg_id: raw_unsigned_int}` from a `306b0003-…` notification buffer."""
    found: dict[int, int] = {}
    i = 0
    n = len(payload)
    while i <= n - 7:
        if payload[i : i + 3] == b"\x08\x00\x19":
            reg = int.from_bytes(payload[i + 3 : i + 5], "big")
            tcode = payload[i + 5]
            if tcode == 0x41 and i + 7 <= n:
                found[reg] = payload[i + 6]
                i += 7
                continue
            if tcode == 0x42 and i + 8 <= n:
                found[reg] = int.from_bytes(payload[i + 6 : i + 8], "little")
                i += 8
                continue
        i += 1
    return found
