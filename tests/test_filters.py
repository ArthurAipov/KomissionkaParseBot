from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.filters import matches
from app.schemas import SubscriptionFilters


def product(**kwargs):
    return {
        "title": "MacBook Pro",
        "description": "с зарядкой",
        "price": "54900",
        "condition": "Хорошее",
        "cities": ["Краснодар"],
        **kwargs,
    }


def test_price_bounds_inclusive():
    assert matches(product(), {"price_min": "54900", "price_max": "54900"})
    assert not matches(product(), {"price_max": "50000"})
    assert not matches(product(), {"price_min": "60000"})


def test_stop_words_search_both_title_and_description_case_insensitively():
    assert not matches(
        product(description="НЕИСПРАВЕН, на запчасти"), {"stop_words": ["неисправен"]}
    )
    assert not matches(product(title="MacBook на ЗАПЧАСТИ"), {"stop_words": ["запчасти"]})
    assert matches(product(), {"stop_words": ["неисправен"]})


def test_conditions_and_multiple_cities():
    assert matches(product(), {"conditions": ["хорошее"], "cities": ["москва", "краснодар"]})
    assert not matches(product(), {"conditions": ["отличное"]})
    assert not matches(product(), {"cities": ["москва"]})
    assert matches(product(), {})


@pytest.mark.parametrize(
    "values",
    [
        {"price_min": 100, "price_max": 50},
        {"price_min": -1},
        {"conditions": ["плохое"]},
        {"price_max": Decimal("NaN")},
    ],
)
def test_invalid_filters_are_rejected(values):
    with pytest.raises(ValidationError):
        SubscriptionFilters.model_validate(values)
