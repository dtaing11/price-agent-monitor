"""Persistence: every field a Product carries must survive a round trip.

This exists because an INSERT once listed fewer columns than the model had, so
titles, images and groups were silently dropped on insert and only appeared
later when a check happened to update the row.
"""

import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from pricemon.models import Extraction, Product
from pricemon.storage import Store


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "prices.db")


FULL = {
    "name": "widget",
    "url": "https://shop.test/p/1",
    "title": "A Real Product Title",
    "image": "https://shop.test/img.jpg",
    "group": "widgets",
    "selector": "span.price",
    "learned_selector": "div.now",
    "target_price": 25.0,
    "currency": "GBP",
    "active": False,
    "notes": "a note",
    "last_checked": "2026-09-01T10:00:00+00:00",
    "last_price": 31.5,
    "last_in_stock": True,
    "fail_count": 2,
}


class TestProductRoundTrip(unittest.TestCase):
    def test_every_field_survives_insert(self):
        store = _store()
        store.add_product(Product(**FULL))
        loaded = store.get_product("widget")
        assert loaded is not None
        for key, value in FULL.items():
            with self.subTest(field=key):
                self.assertEqual(getattr(loaded, key), value)

    def test_model_has_no_field_the_insert_forgets(self):
        # Guards against adding a field to Product and forgetting the INSERT.
        store = _store()
        store.add_product(Product(**FULL))
        loaded = store.get_product("widget")
        assert loaded is not None
        ignore = {"id", "created_at"}
        for f in fields(Product):
            if f.name in ignore:
                continue
            with self.subTest(field=f.name):
                self.assertEqual(getattr(loaded, f.name), FULL[f.name])

    def test_every_field_survives_update(self):
        store = _store()
        product = store.add_product(Product(name="widget", url="https://shop.test/p/1"))
        for key, value in FULL.items():
            setattr(product, key, value)
        store.update_product(product)
        loaded = store.get_product("widget")
        assert loaded is not None
        for key, value in FULL.items():
            with self.subTest(field=key):
                self.assertEqual(getattr(loaded, key), value)


class TestGroups(unittest.TestCase):
    def test_members_and_listing(self):
        store = _store()
        for shop in ("walmart", "amazon"):
            store.add_product(
                Product(
                    name=f"mouse@{shop}", url=f"https://{shop}.test/p", group="mouse"
                )
            )
        store.add_product(Product(name="loner", url="https://x.test/p"))
        self.assertEqual(store.groups(), ["mouse"])
        self.assertEqual(
            [p.name for p in store.group_members("mouse")],
            ["mouse@amazon", "mouse@walmart"],
        )


class TestPriceContext(unittest.TestCase):
    def test_context_needs_history(self):
        store = _store()
        product = store.add_product(Product(name="w", url="https://x.test/p"))
        self.assertEqual(store.price_context(product, 10.0)["points"], 0)

    def test_context_reports_low_and_wins(self):
        store = _store()
        product = store.add_product(Product(name="w", url="https://x.test/p"))
        for price in (30.0, 25.0, 40.0):
            store.record(
                product, Extraction(price=price, currency="USD", method="test")
            )
        ctx = store.price_context(product, 26.0)
        self.assertEqual(ctx["points"], 3)
        self.assertEqual(ctx["low"], 25.0)
        self.assertEqual(ctx["high"], 40.0)
        self.assertEqual(ctx["beats"], 2)  # cheaper than the 30 and the 40


if __name__ == "__main__":
    unittest.main()
