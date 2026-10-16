import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def listing(model="STR-DH790", price=279.99, available=True, id="1", source="bestbuy"):
    m = monitor.match_model(model)
    return monitor.Listing(source=source, id=id, title=f"Open Box - Sony {model}", model=m,
                           condition="Open box", price=price, seller="Best Buy", url="https://x", available=available)


class Matching(unittest.TestCase):
    def test_naming_variations(self):
        for title, want in [
            ("Open Box - Sony STRDH790 7.2 Channel Dolby Atmos AV Receiver", "STR-DH790"),
            ("Sony STR-DH590 5.2 Channel 4K Ultra HD AV Receiver", "STR-DH590"),
            ("Denon AVR S760H 7.2ch", "AVR-S760H"),
            ("Yamaha RX-V485BL 5.1-Channel AV Receiver", "RX-V485"),
            ("Pioneer VSX-834-K 7.2", "VSX-834"),
            ("Refurbished (Excellent) - Yamaha RX-V4A 5.2-Channel AV Receiver with MusicCast", "RX-V4A"),
            ("Marantz NR1711 Slim 7.2", "NR1711"),
            ("OPEN BOX-ONKYO TX-SR494 7.2", "TX-SR494"),
        ]:
            m = monitor.match_model(title)
            self.assertIsNotNone(m, title)
            self.assertEqual(m.name, want, title)

    def test_rejects(self):
        for title in [
            "Refurbished (Good) Denon AVR-S660H 8K Ultra HD A/V Surround Receiver Remote Control RC-1243",
            "MS TECH 6ft Universal AC Cable US Plug Compatible with Denon Audio Receiver 2 Prong Power Cord (AVR-S760H)",
            "Sony STR-DH190 2.0 Bluetooth, A/B Speaker, Stereo Receiver",
            "Denon AVR-S7600H",            # trailing digit: different model
            "Yamaha RX-V4850",
            "Denon AVR-X2800 8K",           # not on the list
            "Denon AVR-S760H vs AVR-S770H comparison",  # ambiguous
            "Sony HT-A3000 Soundbar",
        ]:
            self.assertIsNone(monitor.match_model(title), title)

    def test_conditions(self):
        c = monitor.condition_from_title
        self.assertEqual(c("Open Box - Sony STRDH790"), "Open box")
        self.assertEqual(c("Refurbished (Excellent) - Yamaha RX-V4A"), "Refurbished (Excellent)")
        self.assertEqual(c("Refurbished(Good) Denon"), "Refurbished (Good)")
        self.assertEqual(c("Denon AVR-S740H 7.2ch (Certified Refurbished)"), "Refurbished (certified)")
        self.assertEqual(c("Sony STR-DH590"), "New")

    def test_bestbuy_fixture_parsing(self):
        """Real Best Buy API responses: parts filtered, open-box receivers matched, sellers kept."""
        fixtures = {"query=&categoryid=20313": "bestbuy_openbox.json",
                    "query=refurbished+receiver": "bestbuy_s660h.json"}

        def fake_get_json(url, accept="application/json"):
            if "availability" in url:
                skus = url.split("skus=")[1].replace("%7C", "|").split("|")
                return {"availabilities": [{"sku": s, "shipping": {"purchasable": s == "12869608"},
                                            "pickup": {"purchasable": False}} for s in skus]}
            for q, f in fixtures.items():
                if q in url:
                    return json.loads((FIX / f).read_text())
            return {"products": [], "total": 0}

        with mock.patch.object(monitor, "get_json", fake_get_json):
            got = {l.id: l for l in monitor.bestbuy()}
        self.assertIn("13199407", got)                       # Open Box STRDH790
        self.assertNotIn("19378551", got)                    # $15.99 S660H remote
        self.assertEqual(got["12869608"].model.name, "STR-DH590")
        self.assertTrue(got["12869608"].available)
        self.assertFalse(got["13199407"].available)
        self.assertEqual(got["13199407"].condition, "Open box")
        self.assertNotIn("16384884", got)                    # TX-NR5100 is not watched

    def test_bestbuy_schema_change_raises(self):
        with mock.patch.object(monitor, "get_json", return_value={"items": []}):
            with self.assertRaises(monitor.SourceError):
                monitor.bestbuy()


class StateLogic(unittest.TestCase):
    def setUp(self):
        self.state = {"listings": {}, "sources": {}, "meta": {}}
        self.t = monitor.now()

    def test_new_then_no_repeat(self):
        self.assertIn("New", monitor.decide(listing(), self.state, self.t))
        self.assertIsNone(monitor.decide(listing(), self.state, self.t + timedelta(minutes=20)))

    def test_price_drop(self):
        monitor.decide(listing(price=349), self.state, self.t)
        self.assertIsNone(monitor.decide(listing(price=348.50), self.state, self.t))
        self.assertIn("drop", monitor.decide(listing(price=299), self.state, self.t))

    def test_over_cap_and_sold_out_ignored(self):
        self.assertIsNone(monitor.decide(listing(price=399), self.state, self.t))
        self.assertIsNone(monitor.decide(listing(available=False), self.state, self.t))
        self.assertIn("New", monitor.decide(listing(price=379), self.state, self.t))  # dropped under the cap

    def test_rise_then_fall_realerts(self):
        monitor.decide(listing(price=300), self.state, self.t)
        monitor.decide(listing(price=420), self.state, self.t)
        self.assertIn("drop", monitor.decide(listing(price=310), self.state, self.t))

    def test_back_in_stock_only_after_long_gap(self):
        monitor.decide(listing(), self.state, self.t)
        monitor.decide(listing(available=False), self.state, self.t + timedelta(hours=1))
        self.assertIsNone(monitor.decide(listing(), self.state, self.t + timedelta(hours=2)))
        monitor.decide(listing(available=False), self.state, self.t + timedelta(hours=3))
        self.assertIn("Back", monitor.decide(listing(), self.state, self.t + timedelta(hours=30)))

    def test_vanished_counts_as_unavailable(self):
        monitor.decide(listing(), self.state, self.t)
        monitor.mark_vanished(self.state, "bestbuy", set(), self.t)
        self.assertFalse(self.state["listings"]["bestbuy:1"]["available"])

    def test_broken_source_alerts_once_then_recovers(self):
        msgs = [monitor.source_health(self.state, "skadi", "Skadi", "HTTP 403", self.t) for _ in range(5)]
        self.assertEqual(sum(m is not None for m in msgs), 1)
        self.assertIn("broken", msgs[2][0])
        self.assertIn("working again", monitor.source_health(self.state, "skadi", "Skadi", None, self.t)[0])
        self.assertIsNone(monitor.source_health(self.state, "skadi", "Skadi", None, self.t))

    def test_alert_format(self):
        title, body, prio, _ = monitor.format_alert(listing("TX-SR494", 349.0), "🆕 New listing")
        self.assertIn("OVER BUDGET", title)
        self.assertIn("Onkyo reliability", body)
        self.assertIn("Seller: Best Buy", body)
        self.assertEqual(prio, 3)
        title, _, prio, _ = monitor.format_alert(listing("STR-DH790", 279.0), "🆕 New listing")
        self.assertNotIn("OVER", title)
        self.assertEqual(prio, 4)


if __name__ == "__main__":
    unittest.main()
