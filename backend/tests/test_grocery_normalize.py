from datetime import date

import pytest

from app.agent.grocery.products import ProductLookupError, parse_facts
from app.services.grocery.normalize import (
    Entry,
    after_portion,
    entry_quantity,
    expiry_date,
    parse_entry,
    product_key,
    shelf_life_from,
)


def test_product_key_ignores_case_punctuation_and_spacing():
    assert product_key("  Cherry-Tomatoes! ") == "cherry tomatoes"
    assert product_key("Crème  Fraîche") == "crème fraîche"
    assert product_key("...") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("rice", Entry("rice")),
        ("2 kg rice", Entry("rice", 2000, "mass")),
        ("500g rice", Entry("rice", 500, "mass")),
        ("1,5 l milk", Entry("milk", 1500, "volume")),
        ("200 ml cream", Entry("cream", 200, "volume")),
        ("3 tomatoes", Entry("tomatoes", 3, "count")),
        ("2x oat milk", Entry("oat milk", 2, "count")),
        ("½ cabbage", Entry("cabbage", 0.5, "count")),
        ("1/2 cabbage", Entry("cabbage", 0.5, "count")),
        ("3 large eggs", Entry("large eggs", 3, "count")),
        ("2 grapefruits", Entry("grapefruits", 2, "count")),
        ("7up", Entry("7up")),
        ("2 kg", Entry("2 kg")),
    ],
)
def test_parse_entry_reads_a_leading_amount(text, expected):
    assert parse_entry(text) == expected


def test_entry_quantity_converts_to_the_product_unit():
    rice = {"unit": "g", "pack_quantity": 1000}
    tomatoes = {"unit": "piece", "pack_quantity": 4}
    assert entry_quantity(Entry("rice"), **rice) == 1000
    assert entry_quantity(Entry("rice", 500, "mass"), **rice) == 500
    # A bare count of something weighed means packs.
    assert entry_quantity(Entry("rice", 2, "count"), **rice) == 2000
    assert entry_quantity(Entry("tomatoes", 3, "count"), **tomatoes) == 3
    # Grams of something sold by the piece can't be converted.
    assert entry_quantity(Entry("tomatoes", 500, "mass"), **tomatoes) == 4


def test_storage_dates():
    assert expiry_date(date(2026, 11, 18), 7) == date(2026, 11, 25)
    assert shelf_life_from(date(2026, 11, 18), date(2026, 11, 21)) == 3
    assert shelf_life_from(date(2026, 11, 18), date(2026, 11, 10)) == 1


def test_after_portion_counts_down_to_used_up():
    assert after_portion(1000, 150) == 850
    assert after_portion(0.5, 0.5) is None
    assert after_portion(1.0, 1 / 3 * 3) is None


def test_parse_facts_validates_the_llm_answer():
    facts = parse_facts({
        "name": " Cherry tomatoes ", "category": "produce", "unit": "pack",
        "pack_quantity": 1, "portion": 2, "shelf_life_days": 6,
    })
    assert facts.name == "Cherry tomatoes"
    assert facts.portion == 1  # never more than one pack
    assert parse_facts({
        "name": "Rice", "category": "grains", "unit": "g",
        "pack_quantity": 1000, "portion": 150, "shelf_life_days": 365,
    }).category == "other"
    with pytest.raises(ProductLookupError):
        parse_facts({"name": "Rice", "unit": "kg", "pack_quantity": 1, "portion": 1, "shelf_life_days": 1})
    with pytest.raises(ProductLookupError):
        parse_facts({"name": "Rice", "unit": "g", "pack_quantity": "lots", "portion": 1, "shelf_life_days": 1})
