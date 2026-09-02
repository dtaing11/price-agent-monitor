"""Reading a page the way it renders, not the way it is marked up."""

import unittest

from pricemon.extract import from_rendered
from pricemon.vision import RenderedPage, SeenPrice


def page(*prices, images=None):
    return RenderedPage(
        html="", url="https://shop.test/p/1", prices=list(prices), images=images or []
    )


class TestProminence(unittest.TestCase):
    def test_big_bold_text_near_the_top_wins(self):
        main = SeenPrice(
            text="$39.00", selector="span.now", font_size=30, font_weight="700", y=420
        )
        small = SeenPrice(text="$41.00", selector="span.other", font_size=12, y=2400)
        self.assertGreater(main.prominence, small.prominence)

    def test_crossed_out_is_never_the_price(self):
        was = SeenPrice(text="$59.00", selector="del", font_size=30, y=400, struck=True)
        self.assertEqual(was.prominence, 0.0)

    def test_hidden_is_never_the_price(self):
        gone = SeenPrice(text="$1.00", selector="span", font_size=30, hidden=True)
        self.assertEqual(gone.prominence, 0.0)

    def test_screen_reader_text_is_trusted(self):
        # The shop's own accessible copy of the price, invisible to sighted
        # users but the cleanest string on the page.
        sr = SeenPrice(
            text="$859.99",
            selector="span.a-offscreen",
            screen_reader=True,
            y=700,
            context="$859.99",
        )
        tiny_visible = SeenPrice(text="$4.30", selector="span", font_size=12, y=700)
        self.assertGreater(sr.prominence, tiny_visible.prominence)

    def test_instalments_and_was_prices_are_marked_down(self):
        monthly = SeenPrice(
            text="$16.99",
            selector="span",
            font_size=24,
            y=300,
            context="or $16.99/month with financing",
        )
        plain = SeenPrice(text="$199.00", selector="span", font_size=24, y=300)
        self.assertLess(monthly.prominence, plain.prominence)


class TestFromRendered(unittest.TestCase):
    def test_picks_the_price_over_the_struck_original(self):
        got = from_rendered(
            page(
                SeenPrice(
                    text="$59.00", selector="del.was", font_size=18, y=400, struck=True
                ),
                SeenPrice(
                    text="$39.00",
                    selector="span.now",
                    font_size=30,
                    font_weight="700",
                    y=402,
                ),
            )
        )
        assert got is not None
        self.assertEqual(got.price, 39.00)
        self.assertEqual(got.selector, "span.now")

    def test_declines_when_only_promotions_are_visible(self):
        # Exactly the Amazon case: the buy box never rendered, leaving a
        # finance offer and a bundle total. Saying nothing lets the markup and
        # embedded JSON answer instead of reporting an offer as the price.
        self.assertIsNone(
            from_rendered(
                page(
                    SeenPrice(
                        text="$809.99",
                        selector="span",
                        font_size=14,
                        y=540,
                        context="Get $50 off instantly: Pay $809.99 upon approval",
                    ),
                    SeenPrice(
                        text="$1,159.97",
                        selector="span",
                        font_size=18,
                        y=2338,
                        context="Total price:$1,159.97",
                    ),
                    SeenPrice(
                        text="$119.99",
                        selector="span",
                        font_size=14,
                        y=1063,
                        context="4-Year Protection Plan for $119.99",
                    ),
                )
            )
        )

    def test_declines_when_nothing_is_visible(self):
        self.assertIsNone(from_rendered(page()))

    def test_carries_the_product_photo(self):
        got = from_rendered(
            page(
                SeenPrice(text="$39.00", selector="span.now", font_size=30, y=400),
                images=[
                    {
                        "src": "https://shop.test/photo.jpg",
                        "width": 800,
                        "height": 800,
                        "top": 0,
                        "area": 640000,
                        "alt": "Widget",
                    }
                ],
            )
        )
        assert got is not None
        self.assertEqual(got.image, "https://shop.test/photo.jpg")


if __name__ == "__main__":
    unittest.main()
