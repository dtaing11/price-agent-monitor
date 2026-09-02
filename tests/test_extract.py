import unittest

from pricemon.extract import extract

JSONLD = """<html><head><title>Widget</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Blue Widget",
 "offers":{"@type":"Offer","price":"24.99","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}
</script></head><body><h1>Blue Widget</h1><span class="price">€24.99</span></body></html>"""

MICRODATA = """<html><body itemscope itemtype="http://schema.org/Product">
<h1>Gadget</h1><span itemprop="price" content="149.00">$149.00</span>
<meta itemprop="priceCurrency" content="USD"><button>Add to cart</button></body></html>"""

SALE_AND_RECOMMENDATIONS = """<html><body>
<div class="product-detail"><h1>Boots</h1>
  <del class="price old-price">$120.00</del>
  <span class="price sale-price">$79.00</span>
  <button>Add to cart</button></div>
<aside class="recommendations"><ul>
  <li class="product_pod"><p class="price">$12.00</p></li>
  <li class="product_pod"><p class="price">$8.00</p></li>
</ul></aside></body></html>"""

NO_PRICE = "<html><body><h1>An article</h1><p>Words about things.</p></body></html>"


class TestExtract(unittest.TestCase):
    def test_jsonld_wins(self):
        best, _ = extract(JSONLD)
        self.assertEqual(best.price, 24.99)
        self.assertEqual(best.currency, "EUR")
        self.assertEqual(best.method, "jsonld")
        self.assertTrue(best.in_stock)
        self.assertEqual(best.title, "Blue Widget")

    def test_microdata(self):
        best, _ = extract(MICRODATA)
        self.assertEqual(best.price, 149.0)
        self.assertEqual(best.currency, "USD")

    def test_prefers_sale_price_over_struck_and_recommendations(self):
        best, _ = extract(SALE_AND_RECOMMENDATIONS)
        self.assertEqual(best.price, 79.0)

    def test_pinned_selector_beats_everything(self):
        best, _ = extract(SALE_AND_RECOMMENDATIONS, selector="del.old-price")
        self.assertEqual(best.price, 120.0)
        self.assertEqual(best.method, "selector")

    def test_selector_with_attribute(self):
        best, _ = extract(MICRODATA, selector='[itemprop="price"]@content')
        self.assertEqual(best.price, 149.0)

    def test_no_price(self):
        best, cands = extract(NO_PRICE)
        self.assertIsNone(best.price)
        self.assertEqual(cands, [])


if __name__ == "__main__":
    unittest.main()
