"""Unit tests for `public/power/ble_power.py` using synthetic keys and vectors."""
from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ble_power  # noqa: E402


def test_jbd_bms_a2_parser_and_crc():
    # Construct a synthetic 36-byte 0xA2 frame:
    # cells: 3340, 3342, 3341, 3348 mV; SoC: 58.0%; V: 13.37V; I: -2.9A; SoH: 100.0%; temps: 21.5, 20.4, 20.6 C
    body = bytearray(34)
    body[0] = 0xA2
    body[1] = 0x10
    for idx, mv in enumerate((3340, 3342, 3341, 3348)):
        struct.pack_into(">H", body, 2 + idx * 2, mv)
    struct.pack_into(">H", body, 14, 580)   # 58.0% SoC
    struct.pack_into(">H", body, 16, 1337)  # 13.37 V
    struct.pack_into(">h", body, 18, -29)   # -2.9 A
    struct.pack_into(">H", body, 20, 0)     # problem_code = 0
    struct.pack_into(">H", body, 22, 1000)  # 100.0% SoH
    for idx, tc10 in enumerate((215, 204, 206, -500)):
        struct.pack_into(">h", body, 26 + idx * 2, tc10)
    crc = ble_power.crc16_modbus(bytes(body))
    frame = bytes(body) + crc.to_bytes(2, "little")

    parsed = ble_power.parse_jbd_bms_a2(frame)
    assert parsed is not None
    assert parsed["soc_pct"] == 58.0
    assert parsed["soh_pct"] == 100.0
    assert parsed["voltage_v"] == 13.37
    assert parsed["current_a"] == -2.9
    assert parsed["power_w"] == -38.8
    assert parsed["cells_v"] == [3.34, 3.342, 3.341, 3.348]
    assert parsed["cell_delta_mv"] == 8
    assert parsed["temp_c"] == 20.4

    # Corrupt 1 bit -> CRC check rejects
    bad = bytearray(frame)
    bad[16] ^= 0x01
    assert ble_power.parse_jbd_bms_a2(bytes(bad)) is None


def test_victron_instant_readout_roundtrip_with_synthetic_key():
    synth_key_hex = "0123456789abcdef0123456789abcdef"
    synth_key = bytes.fromhex(synth_key_hex)
    iv = 0x4210
    # Plaintext for DC-DC charger (type 0x04):
    # state=0 (off), err=0, input_v=1254 (12.54V), output_v=0x7FFF (off), off_reason=0x84 (engine_shutdown + switched_off_register)
    plain = struct.pack("<BBHhI", 0, 0, 1254, 0x7FFF, 0x00000084)
    ciphertext = ble_power.aes128_ctr_decrypt(synth_key, iv, plain)
    mfg = bytes([0x10, 0x02]) + struct.pack("<HBHB", 0xA3C8, 0x04, iv, synth_key[0]) + ciphertext

    decoded = ble_power.decode_victron_instant_readout(mfg, synth_key_hex)
    assert decoded is not None
    assert decoded["model_id"] == "0xa3c8"
    assert decoded["state"] == "off"
    assert decoded["input_v"] == 12.54
    assert decoded["output_v"] is None
    assert decoded["off_reasons"] == ["switched_off_register", "engine_shutdown"]


def test_orion_gatt_get_set_and_notification_codec():
    assert ble_power.build_orion_get_packet(0x10EF) == bytes.fromhex("0600001910ef")

    spec_shut = ble_power.ORION_REGISTERS["engine_shutdown_voltage"]
    pkt = ble_power.build_orion_set_packet(spec_shut, 13.10)
    # 13.10 V -> 1310 = 0x051E -> LE 1e 05
    assert pkt == bytes.fromhex("08001910ef421e05")

    spec_mode = ble_power.ORION_REGISTERS["device_mode"]
    assert ble_power.build_orion_set_packet(spec_mode, 4) == bytes.fromhex("08001902004104")

    # Parse concatenated notifications
    notif = pkt + bytes.fromhex("08001902004104")
    parsed = ble_power.parse_orion_gatt_notification(notif)
    assert parsed[0x10EF] == 1310
    assert parsed[0x0200] == 4


def test_public_workspace_dbc_and_pii_boundary():
    import subprocess

    public_root = Path(__file__).resolve().parents[1]
    canc_text = (public_root / "can" / "g55-canc.dbc").read_text()
    canb_text = (public_root / "can" / "g55-canb.dbc").read_text()

    # Excluded identity / mileage frames must never appear in public DBCs
    for forbidden_bo in ("BO_ 178 ", "BO_ 1786 ", "BO_ 88 ", "BO_ 158 "):
        assert forbidden_bo not in canc_text
        assert forbidden_bo not in canb_text

    try:
        import cantools

        db_c = cantools.database.load_file(public_root / "can" / "g55-canc.dbc")
        db_b = cantools.database.load_file(public_root / "can" / "g55-canb.dbc")
        assert len(db_c.messages) >= 20
        assert len(db_b.messages) >= 18
    except ImportError:
        pass

    # Scan all git-tracked files in public/ for forbidden PII / VIN / MAC / host strings
    tracked = subprocess.run(
        ["git", "-C", str(public_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    # The forbidden strings (a VIN prefix, two device MAC prefixes, a home path, a hostname and a private IP) are
    # stored only as (length, first 16 hex of sha256 of the lowercased string), so this file does not publish them.
    forbidden = {
        (5, "7f8be969d81537bf"),
        (8, "6ac0a694faee4d75"),
        (8, "ec0f0c1e0f9962d2"),
        (11, "987963cb9086648c"),
        (6, "70f61a234713c485"),
        (14, "6f40c8521ea1aa5a"),
    }
    lengths = sorted({n for n, _ in forbidden})
    for rel in tracked + ["power/README.md", "power/ble_power.py", "can/case-studies/w463-body-pdm-and-gateway-topology.md"]:
        p = public_root / rel
        if not p.is_file() or p.suffix in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".stl", ".3mf"}:
            continue
        txt = p.read_text(errors="ignore").lower()
        for n in lengths:
            for i in range(len(txt) - n + 1):
                h = hashlib.sha256(txt[i : i + n].encode()).hexdigest()[:16]
                assert (n, h) not in forbidden, f"Forbidden string found in public/{rel} at offset {i}"
