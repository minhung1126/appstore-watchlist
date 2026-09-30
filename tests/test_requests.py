from datetime import datetime, timezone
from decimal import Decimal
from email.utils import format_datetime
import gzip
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import watch


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class RequestTests(unittest.TestCase):
    def client(self, **overrides):
        return watch.Client(watch.DEFAULTS | overrides)

    def test_iap_request_storefront_language_and_headers_no_cookie_or_token(self):
        client = self.client(max_retries=0)
        response = Response(json.dumps({"data": [{"id": "42", "attributes": {"name": "Example"}, "views": {"top-in-app-purchasables": {"data": []}}}]}).encode())
        with patch.object(client.opener, "open", return_value=response) as opened:
            self.assertEqual(watch.fetch_iaps(client, {"id": "42", "language": "zh-TW", "iap_ids": []}, "jp"), ({}, "Example"))
        request = opened.call_args.args[0]
        parsed = urlparse(request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(parsed.path, "/api/apps/v1/catalog/jp/apps/42")
        self.assertEqual(parse_qs(parsed.query), {"platform": ["web"], "views": ["top-in-app-purchasables"], "l": ["zh-tw"]})
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer")
        self.assertEqual(headers["referer"], "https://apps.apple.com/jp/app/id42")
        self.assertEqual(headers["accept-language"], "zh-TW")
        self.assertEqual(headers["accept-encoding"], "gzip")
        self.assertNotIn("cookie", headers)
        self.assertEqual(opened.call_args.kwargs["timeout"], 25)

    def test_lookup_url_and_gzip_decimal(self):
        client = self.client(max_retries=0)
        body = b'{"resultCount":1,"results":[{"trackId":42,"trackName":"Example","price":0.1234567890123456789,"currency":"USD"}]}'
        with patch.object(client.opener, "open", return_value=Response(gzip.compress(body), {"Content-Encoding": "gzip"})) as opened:
            items, _ = watch.fetch_app(client, {"id": "42"}, "us")
        self.assertEqual(parse_qs(urlparse(opened.call_args.args[0].full_url).query), {"id": ["42"], "country": ["us"], "entity": ["software"]})
        self.assertEqual(items["app"]["price"], "0.1234567890123456789")

    def test_retry_after_seconds_date_and_discord_json(self):
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(watch.retry_delay("15", now), 15)
        self.assertEqual(watch.retry_delay(format_datetime(now.replace(second=30), usegmt=True), now), 30)
        self.assertIsNone(watch.retry_delay("NaN", now))
        client = self.client(max_retries=1)
        url = "https://discord.com/api/webhooks/test/secret"
        error = HTTPError(url, 429, "Rate limited", {}, io.BytesIO(b'{"retry_after": 12.5}'))
        with patch.object(client.opener, "open", side_effect=[error, Response(b'{"id":"message"}')]), patch.object(watch.time, "sleep") as sleep:
            client.request(url, payload={"content": "test"}, apple=False)
        sleep.assert_called_once_with(12.5)

    def test_long_rate_limit_defers_instead_of_retrying_early(self):
        client = self.client(max_retries=1)
        error = HTTPError("https://itunes.apple.com/lookup", 429, "Rate limited", {"Retry-After": "300"}, io.BytesIO())
        with patch.object(client.opener, "open", side_effect=error) as opened, patch.object(watch.time, "sleep") as sleep:
            with self.assertRaises(watch.FetchError):
                client.request("https://itunes.apple.com/lookup")
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()

    def test_apple_requests_share_throttle(self):
        client = self.client(max_retries=0)
        with patch.object(client.opener, "open", side_effect=[Response(b"{}"), Response(b"{}")]), patch.object(watch.time, "monotonic", side_effect=[100, 100, 100.5, 103.2]), patch.object(watch.time, "sleep") as sleep:
            client.request("https://itunes.apple.com/lookup")
            client.request("https://apps.apple.com/api/apps/v1/catalog/tw/apps/42")
        self.assertAlmostEqual(sleep.call_args.args[0], 2.7)

    def test_empty_or_malformed_success_response_is_not_unavailable(self):
        for body in (b"", b"<html>blocked</html>"):
            client = self.client(max_retries=0)
            with self.subTest(body=body), patch.object(client.opener, "open", return_value=Response(body)):
                with self.assertRaises(watch.FetchError):
                    client.request("https://itunes.apple.com/lookup")

    def test_redirect_restrictions(self):
        handler = watch.StoreRedirectHandler()
        source = Request("https://apps.apple.com/api/apps/v1/catalog/tw/apps/42")
        for target in ("https://apps.apple.com/api/apps/v1/catalog/us/apps/42", "https://example.com/path", "http://apps.apple.com/api/apps/v1/catalog/tw/apps/42"):
            with self.subTest(target=target), self.assertRaises(watch.FetchError):
                handler.redirect_request(source, None, 302, "redirect", {}, target)
        lookup = Request("https://itunes.apple.com/lookup?id=42&country=tw")
        with self.assertRaises(watch.FetchError):
            handler.redirect_request(lookup, None, 302, "redirect", {}, "https://itunes.apple.com/lookup?id=42&country=us")
        post = Request("https://discord.com/api/webhooks/test/secret", data=b"{}")
        with self.assertRaises(watch.FetchError):
            handler.redirect_request(post, None, 302, "redirect", {}, "https://discord.com/new")
        same = handler.redirect_request(lookup, None, 302, "redirect", {}, "https://itunes.apple.com/lookup?country=tw&id=42&entity=software")
        self.assertIsInstance(same, Request)


if __name__ == "__main__":
    unittest.main()
