from app.services.location import _nav_tum_address, tum_room_id


def test_room_id_from_tumonline_location():
    assert tum_room_id("00.5901.051, Hörsaal (5901.EG.051)") == "5901.EG.051"
    assert tum_room_id("MW 0001, Gustav-Niemann-Hörsaal (5510.EG.001)") == "5510.EG.001"


def test_room_id_ignores_plain_addresses():
    assert tum_room_id("Oliver Wyman GmbH, Müllerstraße 3, 80469 München") is None
    assert tum_room_id("Englischer Garten") is None
    assert tum_room_id("") is None
    assert tum_room_id(None) is None


def test_address_from_computed_props():
    payload = {
        "props": {
            "computed": [
                {"name": "Raumkennung", "text": "5901.EG.051"},
                {"name": "Adresse", "text": "Hans-Piloty-Str. 1, 85748 Garching b. München"},
            ]
        },
        "coords": {"lat": 48.26566, "lon": 11.66256},
    }
    assert _nav_tum_address(payload) == "Hans-Piloty-Str. 1, 85748 Garching b. München"


def test_address_falls_back_to_coords():
    payload = {"coords": {"lat": 48.26566, "lon": 11.66256}}
    assert _nav_tum_address(payload) == "48.26566,11.66256"


def test_address_none_when_payload_empty():
    assert _nav_tum_address({}) is None
