import unicodedata

from app.schemas import SubscriptionFilters


def text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def matches(product, raw_filters: dict | None) -> bool:
    filters = SubscriptionFilters.model_validate(raw_filters or {})

    def value(key, default=None):
        return (
            product.get(key, default)
            if isinstance(product, dict)
            else getattr(product, key, default)
        )

    from decimal import Decimal

    price = Decimal(str(value("price", 0)))
    if filters.price_min is not None and price < filters.price_min:
        return False
    if filters.price_max is not None and price > filters.price_max:
        return False
    description = text(value("title", "") + " " + value("description", ""))
    if any(text(word) in description for word in filters.stop_words):
        return False
    if filters.conditions and text(value("condition", "")) not in filters.conditions:
        return False
    if filters.cities and not set(filters.cities).intersection(
        text(city) for city in value("cities", [])
    ):
        return False
    return True
