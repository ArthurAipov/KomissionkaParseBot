from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.crawler import retry_after_seconds, robots_allowed
from app.db import utcnow
from app.parser import (
    ParseError,
    listing_url,
    parse_categories,
    parse_listing,
    parse_product,
    safe_shop_url,
)
from app.schemas import normalize_query


def test_real_catalog_has_parent_child_tree(fixture_text):
    categories = parse_categories(fixture_text("catalog.html"))
    assert len(categories) == 148
    laptops = next(c for c in categories if c.path.endswith("/noutbuki/"))
    assert laptops.name == "Ноутбуки"
    assert laptops.parent_path == "/catalog/kompyuternaya-tehnika/"
    assert all(
        c.parent_path is None or any(p.path == c.parent_path for p in categories)
        for c in categories
    )


def test_real_search_contains_macbook_and_full_size_photos(fixture_text):
    page = parse_listing(fixture_text("search.html"))
    assert page.total == 226
    assert len(page.products) == 20
    assert page.next_cursor
    product = next(p for p in page.products if p.external_id == "4470969")
    assert product.title == "MacBook Neo"
    assert product.price == Decimal("54900")
    assert product.condition == "удовлетворительное"
    assert product.description == "б\\к"
    assert len(product.images) == 3
    assert all(url.endswith("_XL.webp") for url in product.images)
    assert product.published_at.tzinfo is not None


def test_real_html_cursor_page_differs(fixture_text):
    first = parse_listing(fixture_text("laptops.html"))
    second = parse_listing(fixture_text("cursor.html"))
    assert first.products[0].external_id != second.products[0].external_id
    assert first.next_cursor != second.next_cursor
    assert first.newest_first


def test_search_relevance_disables_unsafe_early_stop(fixture_text):
    assert not parse_listing(fixture_text("search.html")).newest_first


def test_cities_are_taken_only_from_city_home_links():
    from app.parser import parse_cities

    result = parse_cities("""<a href="https://краснодар.komissionki.ru/">Краснодар</a>
      <a href="https://краснодар.komissionki.ru/catalog/a/b/item/">Товар</a>
      <a href="https://c.komissionki.ru/">Фото</a>""")
    assert result == [{"name": "Краснодар", "hostname": "xn--80aalwqglfe.komissionki.ru"}]


def test_real_search_with_zero_matches_is_valid(fixture_text):
    page = parse_listing(fixture_text("empty.html"))
    assert page.total == 0
    assert page.products == []
    assert page.next_cursor is None


def test_product_example(fixture_text):
    url = "https://xn--80aalwqglfe.komissionki.ru/catalog/kompyuternaya-tehnika/noutbuki/macbook-neo-2014501034202-1/"
    product = parse_product(fixture_text("product.html"), url)
    assert product.title == "MacBook Neo"
    assert product.price == Decimal("54900")
    assert product.condition == "Удовлетворительное"
    assert product.description == "б\\к"
    assert product.cities == ["Краснодар"]
    assert len(product.images) == 3


@pytest.mark.parametrize("body", ["<html>Access denied</html>", "<h1>Empty</h1>"])
def test_changed_markup_is_not_an_empty_catalog(body):
    with pytest.raises(ParseError):
        parse_listing(body)


def test_url_encoding_and_normalization():
    url = listing_url("search", "  MACBOOK   & айфон ", "abc+/==")
    query = parse_qs(urlsplit(url).query)
    assert query["search"] == ["macbook & айфон"]
    assert query["sa"] == ["abc+/=="]
    assert normalize_query(" ＭacBook  Pro ") == "macbook pro"


@pytest.mark.parametrize(
    "url",
    [
        "http://komissionki.ru/catalog/",
        "https://127.0.0.1/catalog/",
        "https://komissionki.ru.evil.test/catalog/",
        "https://evil.test/catalog/",
        "https://user:pass@komissionki.ru/catalog/",
        "https://komissionki.ru:8443/catalog/",
        "https://komissionki.ru/api/products",
        "https://komissionki.ru/product/x/",
    ],
)
def test_only_shop_html_urls_allowed(url):
    with pytest.raises(ValueError):
        safe_shop_url(url)


def test_shop_robots_wildcards_and_agent_groups(fixture_text):
    body = fixture_text("robots.txt")
    agent = "KomissionkiWatcher/0.1"
    assert robots_allowed(body, "https://komissionki.ru/catalog/", agent)
    assert not robots_allowed(body, listing_url("search", "macbook"), agent)
    assert not robots_allowed(body, listing_url("category", "/catalog/telefony/"), agent)
    assert not robots_allowed(body, "https://komissionki.ru/catalog/", "AhrefsBot")
    assert robots_allowed(
        "User-agent: *\nDisallow: /catalog/\nAllow: /catalog/free/$",
        "https://komissionki.ru/catalog/free/",
        agent,
    )


def test_retry_after_http_date():
    from datetime import timedelta
    from email.utils import format_datetime

    now = utcnow().replace(microsecond=0)
    assert retry_after_seconds("120", now) == 120
    assert retry_after_seconds(format_datetime(now + timedelta(minutes=5)), now) == 300
    assert retry_after_seconds("invalid", now) == 0
