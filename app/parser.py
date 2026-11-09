"""Pure HTML parsing. No network, JavaScript execution or internal shop API calls."""

import base64
import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.schemas import CategoryData, ListingPage, ProductData, normalize_query, validate_cursor

BASE_URL = "https://komissionki.ru"


class ParseError(ValueError):
    pass


def safe_shop_url(url: str) -> str:
    parts = urlsplit(url)
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid shop URL") from exc
    if (
        parts.scheme != "https"
        or not (host == "komissionki.ru" or host.endswith(".komissionki.ru"))
        or parts.username
        or parts.password
        or port not in (None, 443)
        or (parts.path != "/robots.txt" and not parts.path.startswith("/catalog/"))
        or "\\" in url
        or any(c in url for c in "\r\n\x00")
    ):
        raise ValueError("Only public HTTPS catalog URLs on komissionki.ru are allowed")
    return urlunsplit(("https", host, parts.path, parts.query, ""))


def listing_url(kind: str, value: str, cursor: str | None = None) -> str:
    params = {"fieldSort": "createdAt", "sort": "desc"}
    if kind == "search":
        path = "/catalog/search"
        params["search"] = normalize_query(value)
    elif kind == "category":
        if not re.fullmatch(r"/catalog/(?:[a-z0-9-]+/){1,3}", value):
            raise ValueError("Invalid category path")
        path = value
    else:
        raise ValueError("Invalid source kind")
    if cursor:
        params["sa"] = validate_cursor(cursor)
    return safe_shop_url(BASE_URL + path + "?" + urlencode(sorted(params.items())))


def clean_text(text: str) -> str:
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True) if "<" in text else text.strip()


def parse_categories(html: str) -> list[CategoryData]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, CategoryData] = {}
    for a in soup.select("a.catalog-categories__category, a.catalog-categories__subcategory"):
        path = urlsplit(urljoin(BASE_URL, a.get("href", ""))).path
        if not re.fullmatch(r"/catalog/(?:[a-z0-9-]+/){1,3}", path):
            continue
        # Counts are nested spans, not part of the category name.
        name = " ".join(str(n).strip() for n in a.find_all(string=True, recursive=False)).strip()
        if not name:
            continue
        segments = path.strip("/").split("/")
        parent = "/" + "/".join(segments[:-1]) + "/" if len(segments) > 2 else None
        found[path] = CategoryData(name=name, path=path, parent_path=parent)
    if not found:
        raise ParseError("Category selectors no longer match the shop HTML")
    return sorted(found.values(), key=lambda c: (c.path.count("/"), c.path))


def parse_cities(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cities = {}
    for anchor in soup.select("a[href]"):
        parts = urlsplit(urljoin(BASE_URL, anchor["href"]))
        host = (parts.hostname or "").encode("idna").decode("ascii")
        name = anchor.get_text(" ", strip=True)
        if (
            parts.path in ("", "/")
            and host.endswith(".komissionki.ru")
            and host not in ("www.komissionki.ru", "c.komissionki.ru")
            and 1 < len(name) < 100
        ):
            cities[host] = {"name": name, "hostname": host}
    return sorted(cities.values(), key=lambda c: c["name"])


def image_url(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "c.komissionki.ru":
        return None
    return url


def parse_listing(html: str) -> ListingPage:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("#catalogCards[data-pagination]")
    if node is None:
        raise ParseError(
            "Missing catalogCards/data-pagination; refusing to treat this as an empty page"
        )
    try:
        data = json.loads(node["data-pagination"])
        rows, total = data["data"], int(data["total"])
        if not isinstance(rows, list) or total < 0:
            raise ValueError("Invalid listing shape")
        products = []
        ids: set[str] = set()
        for row in rows:
            if row.get("isDelete") or row.get("isActive") is False:
                continue
            external_id = str(row["id"])
            if external_id in ids:
                continue
            ids.add(external_id)
            parent = row["parentCategory"]["code"]
            child = row["category"]["code"]
            code = row["code"]
            if not all(re.fullmatch(r"[a-z0-9-]+", v) for v in (parent, child, code)):
                raise ValueError("Invalid product path")
            category_path = f"/catalog/{parent}/{child}/"
            card = soup.select_one(f'.card[data-id="{int(row["id"])}"]')
            anchor = card.select_one("a.card__name") if card else None
            url = safe_shop_url(
                urljoin(BASE_URL, anchor["href"])
                if anchor
                else BASE_URL + category_path + code + "/"
            )
            photos = row.get("photosResized") or {}
            images = [
                f"https://c.komissionki.ru/{key}/{key}_XL.webp"
                for key in sorted(photos, key=lambda key: photos[key])
                if re.fullmatch(r"[A-Za-z0-9]+", key)
            ]
            products.append(
                ProductData(
                    external_id=external_id,
                    url=url,
                    title=row.get("title") or row["name"],
                    price=row["price"],
                    condition=row.get("presentation"),
                    description=clean_text(row.get("description") or ""),
                    images=images,
                    cities=[c.get("title") or c["name"] for c in row.get("city") or []],
                    category_path=category_path,
                    published_at=row.get("createdAt"),
                )
            )
        cursor = validate_cursor(data.get("sa")) if rows else None
        if rows and total == 0:
            raise ValueError("Nonempty page with total=0")
        if len(rows) < 20 or len(rows) >= total:
            cursor = None
        if len(rows) >= 20 and total > len(rows) and not cursor:
            raise ValueError("Pagination cursor is missing")
        dates = [p.published_at for p in products]
        newest_first = all(d is not None for d in dates) and all(
            a >= b for a, b in zip(dates, dates[1:], strict=False)
        )
        if data.get("sa"):
            try:
                # Search cursors prepend a relevance score; a known hit may precede newer hits.
                ordering = json.loads(base64.b64decode(data["sa"]))
                if len(ordering) > 3:
                    newest_first = False
            except (ValueError, TypeError):
                newest_first = False
        return ListingPage(
            products=products, total=total, next_cursor=cursor, newest_first=newest_first
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ParseError("Shop listing HTML has an unsupported data format") from exc


def parse_product(html: str, url: str) -> ProductData:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(".product[data-id][data-price]")
    if node is None:
        raise ParseError("Missing product selectors")
    try:
        title = soup.select_one("h1.product__title")
        condition = soup.select_one(".product__condition-label-active")
        description = soup.select_one(".product__description-condition")
        images = list(
            dict.fromkeys(
                image_url(a["href"])
                for a in soup.select("#productGallery a[href]")
                if image_url(a["href"])
            )
        )
        location = json.loads(node.get("data-location", "[]"))
        path = urlsplit(url).path.rsplit("/", 2)[0] + "/"
        return ProductData(
            external_id=node["data-id"],
            url=safe_shop_url(url),
            title=title.get_text(" ", strip=True) if title else node["data-title"],
            price=node["data-price"],
            condition=condition.get_text(" ", strip=True) if condition else None,
            description=description.get_text("\n", strip=True) if description else "",
            images=images,
            cities=list(dict.fromkeys(x["city"]["name"] for x in location if x.get("city"))),
            category_path=path,
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ParseError("Shop product HTML has an unsupported data format") from exc


def parse_page(html: str, url: str) -> dict:
    if urlsplit(url).path.rstrip("/") == "/catalog":
        return {
            "kind": "catalog",
            "categories": [c.model_dump() for c in parse_categories(html)],
            "cities": parse_cities(html),
        }
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one(".product[data-id]"):
        return {"kind": "product", "product": parse_product(html, url).model_dump(mode="json")}
    return {"kind": "listing", **parse_listing(html).model_dump(mode="json")}


def cursor_from_url(url: str) -> str | None:
    return parse_qs(urlsplit(url).query).get("sa", [None])[0]
