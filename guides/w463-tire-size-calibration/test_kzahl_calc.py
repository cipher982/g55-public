from kzahl_calc import SLOT1, error_pct, hex16, kzahl, odometer_pct


def test_stock():
    assert kzahl(2419) == 1664
    assert hex16(kzahl(2419)) == "0x0680"


def test_35_inch():
    assert kzahl(2666) == 1510
    assert hex16(kzahl(2666)) == "0x05E6"
    assert SLOT1 == 0x3900FA


def test_errors():
    assert round(error_pct(2666), 1) == 10.2
    assert round(odometer_pct(2666), 1) == 9.3
