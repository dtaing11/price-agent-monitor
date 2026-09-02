"""Alert decisions: the rules about when the agent speaks and when it stays quiet."""

import tempfile
import unittest
from pathlib import Path

from pricemon import config as config_mod
from pricemon.agent import Agent
from pricemon.models import Extraction, Product
from pricemon.storage import Store


def _agent(**alert_overrides):
    cfg = {
        "fetch": dict(config_mod.DEFAULTS["fetch"]),
        "llm": {"backend": "off", "model": "none"},
        "alerts": {**config_mod.DEFAULTS["alerts"], **alert_overrides},
        "notify": {"console": False, "desktop": False},
    }
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    return Agent(store, cfg, verbose=False), store


def _product(store, **kw):
    return store.add_product(Product(name="widget", url="https://shop.test/p/1", **kw))


class TestTargetAlerts(unittest.TestCase):
    def test_fires_when_crossing_into_target(self):
        agent, store = _agent()
        p = _product(store, target_price=50.0, last_price=60.0, currency="USD")
        alerts = agent._decide(p, Extraction(price=45.0, currency="USD"))
        self.assertEqual([a.kind for a in alerts], ["target_hit"])
        self.assertEqual(alerts[0].url, p.url)

    def test_silent_while_it_just_sits_below_target(self):
        agent, store = _agent()
        p = _product(store, target_price=50.0, last_price=45.0, currency="USD")
        self.assertEqual(agent._decide(p, Extraction(price=45.0, currency="USD")), [])

    def test_speaks_again_on_a_new_low_below_target(self):
        agent, store = _agent()
        p = _product(store, target_price=50.0, last_price=45.0, currency="USD")
        alerts = agent._decide(p, Extraction(price=39.0, currency="USD"))
        self.assertEqual([a.kind for a in alerts], ["target_hit"])

    def test_drop_needs_to_clear_the_threshold(self):
        agent, store = _agent(drop_pct=10.0)
        p = _product(store, last_price=100.0, currency="USD")
        self.assertEqual(agent._decide(p, Extraction(price=95.0, currency="USD")), [])
        alerts = agent._decide(p, Extraction(price=80.0, currency="USD"))
        self.assertEqual([a.kind for a in alerts], ["price_drop"])

    def test_alerts_use_the_product_title_not_the_slug(self):
        agent, store = _agent()
        p = _product(
            store,
            title="BILLY Bookcase, oak",
            target_price=50.0,
            last_price=60.0,
            currency="USD",
        )
        alerts = agent._decide(p, Extraction(price=45.0, currency="USD"))
        self.assertIn("BILLY Bookcase, oak", alerts[0].message)
        self.assertNotIn("widget", alerts[0].message)


class TestImplausibleGuard(unittest.TestCase):
    def test_ordinary_moves_pass(self):
        agent, store = _agent()
        p = _product(store, last_price=100.0, currency="USD")
        self.assertIsNone(agent._implausible(p, Extraction(price=70.0)))

    def test_wild_fall_is_flagged(self):
        agent, store = _agent()
        p = _product(store, last_price=900.0, currency="USD")
        reason = agent._implausible(p, Extraction(price=9.99))
        self.assertIsNotNone(reason)
        self.assertIn("fall", reason)

    def test_wild_jump_is_flagged(self):
        agent, store = _agent()
        p = _product(store, last_price=10.0, currency="USD")
        self.assertIn("jump", agent._implausible(p, Extraction(price=900.0)))

    def test_first_ever_reading_is_never_implausible(self):
        agent, store = _agent()
        p = _product(store, last_price=None)
        self.assertIsNone(agent._implausible(p, Extraction(price=9.99)))

    def test_threshold_is_configurable_and_disablable(self):
        agent, store = _agent(implausible_pct=0)
        p = _product(store, last_price=900.0)
        self.assertIsNone(agent._implausible(p, Extraction(price=1.0)))

    def test_unconfirmable_change_reports_instead_of_crying_wolf(self):
        # With no LLM available the change cannot be confirmed, so the agent
        # must not fire a price_drop off a reading it does not trust.
        agent, store = _agent()
        p = _product(store, last_price=900.0, target_price=100.0, currency="USD")
        extraction, reason = agent._second_opinion(
            p, Extraction(price=9.99, currency="USD")
        )
        self.assertIsNotNone(reason)
        self.assertEqual(extraction.price, 9.99)


if __name__ == "__main__":
    unittest.main()
