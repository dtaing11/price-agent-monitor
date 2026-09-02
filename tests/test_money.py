import unittest
from typing import ClassVar

from pricemon.money import format_price, parse_price


class TestParsePrice(unittest.TestCase):
    CASES: ClassVar[list] = [
        ("$1,234.56", 1234.56, "USD"),
        ("1.234,56 €", 1234.56, "EUR"),
        ("USD 45.00", 45.0, "USD"),
        ("£9.99", 9.99, "GBP"),
        ("45,00 kr", 45.0, "SEK"),
        ("¥12,800", 12800.0, "JPY"),
        ("1 299,00 zł", 1299.0, "PLN"),
        ("From $20.00 - $30.00", 20.0, "USD"),
        ("19,99", 19.99, None),
        ("Price: 1.234", 1234.0, None),
        ("₹1,49,900", 149900.0, "INR"),  # lakh grouping
        ("R$ 3.499,90", 3499.90, "BRL"),
        ("Save 20% - $79.99", 79.99, "USD"),  # percentages are not prices
        ("Free", None, None),
        ("", None, None),
        (None, None, None),
    ]

    def test_cases(self):
        for text, price, currency in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(parse_price(text), (price, currency))

    def test_format(self):
        self.assertEqual(format_price(1234.5, "USD"), "$1,234.50")
        self.assertEqual(format_price(None, "USD"), "n/a")
        self.assertEqual(format_price(10.0, "SEK"), "10.00 SEK")


if __name__ == "__main__":
    unittest.main()
