from ode_lookup_db.languages import UNMAPPED_CODE, to_iso_code


def test_known_language():
    assert to_iso_code("English") == "en"
    assert to_iso_code("german") == "de"


def test_unknown_language_falls_back():
    assert to_iso_code("Klingon") == UNMAPPED_CODE
