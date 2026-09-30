import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.state = {"version": 1, "scopes": {}, "pending_notifications": [], "last_check": None}
        self.config = watch.DEFAULTS.copy()
        self.item = watch.product("Lifetime", "100.00", "TWD", kind="iap", iap_id="123")

    def observe(self, items):
        return watch.observe(self.state, "42:tw:iap", items, "Example", self.config, watch.utc_now())

    def test_baseline_price_changes_and_no_repeat(self):
        self.assertEqual(self.observe({"sku": self.item}), [])
        self.assertEqual(self.observe({"sku": self.item}), [])
        cheaper = self.item | {"price": "0"}
        self.assertEqual(self.observe({"sku": cheaper})[0]["kind"], "price_changed")
        self.assertEqual(self.observe({"sku": cheaper}), [])
        self.assertEqual(self.observe({"sku": self.item})[0]["kind"], "price_changed")
        currency_change = self.item | {"currency": "USD"}
        self.assertEqual(self.observe({"sku": currency_change})[0]["kind"], "price_changed")

    def test_new_missing_and_restored(self):
        self.observe({"sku": self.item})
        self.assertEqual(self.observe({"sku": self.item, "new": self.item})[0]["kind"], "added")
        self.assertEqual(self.observe({"sku": self.item}), [])
        self.assertEqual(self.observe({"sku": self.item}), [])
        self.assertEqual(self.observe({"sku": self.item})[0]["kind"], "removed")
        self.assertEqual(self.observe({"sku": self.item}), [])
        self.assertEqual(self.observe({"sku": self.item, "new": self.item})[0]["kind"], "restored")

    def test_failed_query_does_not_mark_missing(self):
        self.observe({"sku": self.item})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apps.json").write_text(json.dumps({"apps": [{"id": "42", "regions": ["tw"], "track_app": False}]}))
            (root / "config.json").write_text("{}")
            watch.save_state(root / "state.json", self.state)
            with patch.object(watch, "fetch_iaps", side_effect=watch.FetchError("HTTP 429")):
                result = watch.main(["--apps", str(root / "apps.json"), "--config", str(root / "config.json"), "--state", str(root / "state.json"), "--dry-run"])
            self.assertEqual(result, 1)
            saved = watch.read_json(root / "state.json")
            self.assertEqual(saved["scopes"]["42:tw:iap"]["products"]["sku"]["missing_checks"], 0)

    def test_notification_failure_retains_unsent_events(self):
        self.observe({"sku": self.item})
        self.observe({"sku": self.item | {"price": "50"}, "new": self.item})
        class Client:
            calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise watch.FetchError("HTTP 429")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            watch.save_state(path, self.state)
            with self.assertRaises(watch.FetchError):
                watch.send_pending(Client(), self.state, path, "https://discord.com/api/webhooks/example")
            self.assertEqual(len(watch.read_json(path)["pending_notifications"]), 1)
            self.assertEqual(len(self.state["pending_notifications"]), 1)

    def test_iap_parser_stable_identity_and_subscription(self):
        entries = [{"id": "123", "attributes": {"name": "Monthly", "isSubscription": True, "offers": [
            {"type": "buy", "price": 9.99, "currencyCode": "USD", "recurringSubscriptionPeriod": "P1M"},
            {"type": "buy", "price": 99.99, "currencyCode": "USD", "recurringSubscriptionPeriod": "P1Y"},
        ]}}]
        raw = {"data": [{"id": "42", "attributes": {"name": "Example"}, "views": {"top-in-app-purchasables": {"data": entries}}}]}
        class Client:
            def request(self, *args, **kwargs):
                return raw
        app = {"id": "42", "language": "en-US", "iap_ids": []}
        first, _ = watch.fetch_iaps(Client(), app, "us")
        entries[0]["attributes"]["offers"].reverse()
        second, _ = watch.fetch_iaps(Client(), app, "us")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertTrue(all(item["is_subscription"] for item in first.values()))
        raw["data"][0]["views"] = {}
        with self.assertRaises(watch.FetchError):
            watch.fetch_iaps(Client(), app, "us")

    def test_prices_reject_bad_values(self):
        self.assertEqual(watch.money("9.9900"), "9.99")
        for value in (None, True, "NaN", "Infinity", -1, "free"):
            with self.assertRaises(watch.FetchError):
                watch.money(value)

    def test_empty_watchlist_runs_without_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apps.json").write_text('{"apps": []}')
            (root / "config.json").write_text("{}")
            with patch.dict("os.environ", {}, clear=True):
                result = watch.main(["--apps", str(root / "apps.json"), "--config", str(root / "config.json"), "--state", str(root / "state.json")])
            self.assertEqual(result, 0)
            self.assertFalse((root / "state.json").exists())

    def test_interval_skips_fetch_but_retries_pending(self):
        self.observe({"sku": self.item})
        self.observe({"sku": self.item | {"price": "50"}})
        self.state["last_check"] = watch.utc_now()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apps.json").write_text(json.dumps({"apps": [{"id": "42", "regions": ["tw"]}]}))
            (root / "config.json").write_text("{}")
            watch.save_state(root / "state.json", self.state)
            with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test/secret"}), patch.object(watch, "fetch_app") as fetch, patch.object(watch.Client, "request", return_value={}) as send:
                result = watch.main(["--apps", str(root / "apps.json"), "--config", str(root / "config.json"), "--state", str(root / "state.json")])
            self.assertEqual(result, 0)
            fetch.assert_not_called()
            self.assertEqual(send.call_count, 1)
            self.assertEqual(watch.read_json(root / "state.json")["pending_notifications"], [])

    def test_http_retry_does_not_log_secret(self):
        from urllib.error import HTTPError
        secret_url = "https://discord.com/api/webhooks/test/DO_NOT_PRINT"
        config = self.config | {"max_retries": 1}
        error = HTTPError(secret_url, 429, "Too many requests", {}, None)
        client = watch.Client(config)
        with patch.object(client.opener, "open", side_effect=error), patch.object(watch.time, "sleep"), self.assertLogs(watch.LOG, level="WARNING") as logs:
            with self.assertRaises(watch.FetchError):
                client.request(secret_url, payload={}, apple=False)
        self.assertNotIn("DO_NOT_PRINT", " ".join(logs.output))

    def test_full_flow_multiple_apps_regions_currencies_and_notifications(self):
        """Exercise real parsers, state persistence and Discord payloads across three runs."""
        from urllib.parse import parse_qs, urlparse
        prices = {"tw": ("TWD", "300", "90"), "us": ("USD", "9.99", "2.99"), "jp": ("JPY", "1500", "500")}
        phase = 0
        sent = []

        def request(client, url, *, headers=None, payload=None, apple=True):
            if not apple:
                sent.append(payload)
                return {"id": "discord-message"}
            parsed = urlparse(url)
            if parsed.hostname == "itunes.apple.com":
                query = parse_qs(parsed.query)
                region, app_id = query["country"][0], query["id"][0]
                currency, app_price, _ = prices[region]
                if phase:
                    app_price = str(watch.Decimal(app_price) / 2)
                return {"resultCount": 1, "results": [{"trackId": int(app_id), "trackName": "App " + app_id, "price": app_price, "currency": currency}]}
            region, app_id = parsed.path.split("/")[-3], parsed.path.split("/")[-1]
            currency, _, iap_price = prices[region]
            if phase:
                iap_price = str(watch.Decimal(iap_price) * 2)
            entries = [
                {"id": "123", "attributes": {"name": "Lifetime", "isSubscription": False, "offers": [{"price": iap_price, "currencyCode": currency}]}},
                {"id": "456", "attributes": {"name": "Monthly", "isSubscription": True, "offers": [{"price": iap_price, "currencyCode": currency, "recurringSubscriptionPeriod": "P1M"}]}},
            ]
            return {"data": [{"id": app_id, "attributes": {"name": "App " + app_id}, "views": {"top-in-app-purchasables": {"data": entries}}}]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apps.json").write_text(json.dumps({"apps": [{"id": app_id, "regions": list(prices)} for app_id in ("42", "43")]}))
            (root / "config.json").write_text("{}")
            argv = ["--apps", str(root / "apps.json"), "--config", str(root / "config.json"), "--state", str(root / "state.json"), "--force"]
            with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test/secret"}), patch.object(watch.Client, "request", request):
                self.assertEqual(watch.main(argv), 0)
                self.assertEqual(sent, [])  # First successful fetch is a baseline.
                phase = 1
                self.assertEqual(watch.main(argv), 0)
                self.assertEqual(len(sent), 18)  # 2 Apps x 3 regions x (App + IAP + subscription).
                for region, (currency, _, _) in prices.items():
                    embeds = [payload["embeds"][0] for payload in sent if f"/{region}/" in payload["embeds"][0]["url"]]
                    self.assertEqual(len(embeds), 6)
                    types = set()
                    for embed in embeds:
                        fields = {field["name"]: field["value"] for field in embed["fields"]}
                        self.assertEqual(fields["地區"], region.upper())
                        self.assertTrue(fields["原價"].startswith(currency + " "))
                        self.assertTrue(fields["現價"].startswith(currency + " "))
                        types.add(fields["商品類型"])
                    self.assertEqual(types, {"App 本體", "App 內購買", "訂閱"})
                self.assertEqual(watch.main(argv), 0)
                self.assertEqual(len(sent), 18)  # Unchanged prices do not notify again.
                saved = watch.read_json(root / "state.json")
                self.assertEqual(len(saved["scopes"]), 12)
                self.assertEqual(saved["pending_notifications"], [])

    def test_app_or_iap_only_selection(self):
        for app_enabled, iap_enabled in ((True, False), (False, True)):
            with self.subTest(track_app=app_enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "apps.json").write_text(json.dumps({"apps": [{"id": "42", "regions": ["tw"], "track_app": app_enabled, "track_iap": iap_enabled}]}))
                (root / "config.json").write_text("{}")
                with patch.object(watch, "fetch_app", return_value=({}, "Example")) as app_fetch, patch.object(watch, "fetch_iaps", return_value=({}, "Example")) as iap_fetch:
                    self.assertEqual(watch.main(["--apps", str(root / "apps.json"), "--config", str(root / "config.json"), "--state", str(root / "state.json"), "--force", "--dry-run"]), 0)
                self.assertEqual(app_fetch.call_count, int(app_enabled))
                self.assertEqual(iap_fetch.call_count, int(iap_enabled))


if __name__ == "__main__":
    unittest.main()
