"""Turning what a person pastes into something a search engine can use."""

import unittest
from typing import ClassVar

from pricemon.search import _looks_like_product, normalize_query


class TestNormalizeQuery(unittest.TestCase):
    CASES: ClassVar[list[tuple[str, str]]] = [
        # A pasted shop title: keep brand + model, drop the marketing.
        (
            (
                "Logitech MX Master 3S - Wireless Performance Mouse with "
                "Ultra-fast Scrolling, Ergo, 8K DPI, Quiet Clicks, USB-C"
            ),
            "Logitech MX Master 3S",
        ),
        (
            (
                "Sony WH-1000XM5 Wireless Industry Leading Noise Cancelling "
                "Headphones with Auto Noise Cancelling Optimizer, Black"
            ),
            "Sony WH-1000XM5",
        ),
        # Dimensions and colours after a comma are noise.
        ('BILLY Bookcase, white, 31 1/2x11x79 1/2"', "BILLY Bookcase"),
        # A pipe-separated title.
        ("Dyson V15 Detect Absolute Cordless Vacuum | Yellow | New", "Dyson V15"),
        # Already short: leave it alone.
        ("logitech mx master 3s", "logitech mx master 3s"),
        ("air fryer", "air fryer"),
        # Nothing to work with.
        ("", ""),
    ]

    def test_cases(self):
        for pasted, expected in self.CASES:
            with self.subTest(pasted=pasted[:40]):
                self.assertEqual(normalize_query(pasted), expected)

    def test_never_returns_empty_for_real_text(self):
        for text in ("with and for the", "!!!", "a"):
            with self.subTest(text=text):
                self.assertTrue(normalize_query(text))

    def test_stays_short(self):
        long_title = " ".join(f"word{i}" for i in range(40))
        self.assertLessEqual(len(normalize_query(long_title).split()), 8)


class TestProductUrlFilter(unittest.TestCase):
    def test_accepts_real_product_pages(self):
        for url in (
            "https://www.amazon.com/dp/B08N5WRWNW",
            "https://www.walmart.com/ip/Some-Thing/12345",
            "https://www.ebay.com/itm/123456789",
            "https://www.ikea.com/us/en/p/billy-bookcase-white-00263850/",
            "https://shop.example.com/products/blue-widget",
        ):
            with self.subTest(url=url):
                self.assertTrue(_looks_like_product(url))

    def test_rejects_categories_blogs_and_encyclopedias(self):
        for url in (
            "https://www.ikea.com/us/en/cat/billy-series-28102/",
            "https://en.wikipedia.org/wiki/IKEA_Billy",
            "https://www.amazon.com/s?k=mouse",
            "https://ikeahackers.net/2025/06/billy-bookcase-hacks.html",
            "https://www.reddit.com/r/ikea/comments/abc/billy/",
        ):
            with self.subTest(url=url):
                self.assertFalse(_looks_like_product(url))


if __name__ == "__main__":
    unittest.main()
