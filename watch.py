"""App Store price watcher. Python 3.12+, standard library only."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import gzip
from http.client import HTTPException
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("watchlist")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"
DEFAULTS = {
    "interval_hours": 3, "request_interval_seconds": 3.2,
    "request_timeout_seconds": 25, "max_retries": 3,
    "retry_base_seconds": 5, "missing_confirmation_checks": 3,
    "notify_initial": False, "notify_new_products": True,
    "log_level": "INFO", "log_file": None,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream, parse_float=Decimal)


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_settings(apps_path, config_path):
    config = DEFAULTS | read_json(config_path)
    for name in ("interval_hours", "request_interval_seconds", "request_timeout_seconds", "retry_base_seconds"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive number")
        config[name] = float(value)
    for name in ("max_retries", "missing_confirmation_checks"):
        value = config[name]
        minimum = 0 if name == "max_retries" else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    for name in ("notify_initial", "notify_new_products"):
        if not isinstance(config[name], bool):
            raise ValueError(f"{name} must be boolean")
    if config["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("Invalid log_level")
    apps = read_json(apps_path)["apps"]
    if not isinstance(apps, list):
        raise ValueError("apps must be an array")
    seen = set()
    for app in apps:
        app["id"] = str(app["id"])
        if not app["id"].isdigit():
            raise ValueError("App ID must contain digits only")
        if not isinstance(app.get("regions"), list) or not app["regions"]:
            raise ValueError(f"App {app['id']} requires regions")
        app["regions"] = [region.lower() for region in app["regions"]]
        for region in app["regions"]:
            if not re.fullmatch(r"[a-z]{2}", region):
                raise ValueError(f"Invalid region: {region}")
            key = (app["id"], region)
            if key in seen:
                raise ValueError(f"Duplicate App/region: {key}")
            seen.add(key)
        app.setdefault("language", "en-US")
        if not isinstance(app["language"], str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", app["language"]):
            raise ValueError("Invalid language tag")
        for name in ("track_app", "track_iap"):
            app.setdefault(name, True)
            if not isinstance(app[name], bool):
                raise ValueError(f"{name} must be boolean")
        app.setdefault("iap_ids", [])
        if not isinstance(app["iap_ids"], list):
            raise ValueError("iap_ids must be an array")
        app["iap_ids"] = [str(item) for item in app["iap_ids"]]
        if any(not item.isdigit() for item in app["iap_ids"]):
            raise ValueError("IAP IDs must contain digits only")
    return apps, config


class FetchError(Exception):
    pass


class StoreRedirectHandler(HTTPRedirectHandler):
    """Keep Apple GETs on the requested storefront; never redirect webhook POSTs."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        source, destination = urlparse(request.full_url), urlparse(new_url)
        if request.get_method() != "GET" or source.hostname not in ("itunes.apple.com", "apps.apple.com"):
            raise FetchError("Unexpected request redirect")
        if destination.scheme != "https" or destination.hostname != source.hostname:
            raise FetchError("Unexpected Apple host redirect")
        if source.hostname == "apps.apple.com":
            def storefront(path):
                match = re.match(r"/api/apps/v1/catalog/([a-z]{2})/|/([a-z]{2})/app/", path)
                return next((part for part in match.groups() if part), None) if match else None
            if not storefront(source.path) or storefront(source.path) != storefront(destination.path):
                raise FetchError("Unexpected Apple storefront redirect")
        else:
            original = dict(parse_qsl(source.query))
            target = dict(parse_qsl(destination.query))
            if any(target.get(key) != original.get(key) for key in ("country", "id") if key in original):
                raise FetchError("Unexpected Lookup storefront or App ID redirect")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def retry_delay(value, now=None):
    """Parse Retry-After seconds or an HTTP date. Invalid hints use backoff."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = (deadline - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class Client:
    def __init__(self, config):
        self.config = config
        self.last_request = 0.0
        # No CookieJar or token acquisition; works in Python and GitHub runners.
        self.opener = build_opener(StoreRedirectHandler())

    def request(self, url, *, headers=None, payload=None, apple=True):
        for attempt in range(self.config["max_retries"] + 1):
            if apple:
                wait = self.config["request_interval_seconds"] - (time.monotonic() - self.last_request)
                if wait > 0:
                    time.sleep(wait)
                self.last_request = time.monotonic()
            request_headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"} | (headers or {})
            data = None
            if payload is not None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                request_headers["Content-Type"] = "application/json"
            retry_after = None
            try:
                with self.opener.open(Request(url, data=data, headers=request_headers), timeout=self.config["request_timeout_seconds"]) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    if apple and not body:
                        raise FetchError("Empty Apple response")
                    return json.loads(body, parse_float=Decimal) if body else None
            except HTTPError as error:
                if apple and error.code == 404:
                    return None
                message = f"HTTP {error.code}"
                retry = error.code in (408, 429) or error.code >= 500
                retry_after = error.headers.get("Retry-After")
                if not apple and error.code == 429:
                    # Discord can provide the wait in JSON instead of a header.
                    try:
                        hint = json.loads(error.read(65536)).get("retry_after")
                        if retry_after is None:
                            retry_after = hint
                    except (ValueError, AttributeError, OSError):
                        pass
                error.close()
            except (URLError, TimeoutError, OSError, HTTPException):
                message, retry = "Network request failed", True
            except (ValueError, EOFError):
                raise FetchError("Invalid JSON response") from None
            if not retry or attempt == self.config["max_retries"]:
                raise FetchError(message) from None
            delay = min(60, self.config["retry_base_seconds"] * 2 ** attempt)
            hint = retry_delay(retry_after)
            if hint is not None:
                if hint > 120:
                    raise FetchError("Rate-limit wait exceeds 120s; defer to next run") from None
                delay = max(delay, hint)
            # Never include the request URL: Discord's URL contains its secret.
            LOG.warning("%s request: %s; retry %d in %.1fs", "Apple" if apple else "Discord", message, attempt + 1, delay)
            time.sleep(delay)


def money(value):
    if value is None or isinstance(value, bool):
        raise FetchError("Missing numeric price")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise InvalidOperation
        return format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        raise FetchError("Invalid numeric price") from None


def product(name, price, currency, **details):
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise FetchError("Missing or invalid currency")
    return {"name": name, "price": money(price), "currency": currency, **details}


def fetch_app(client, app, region):
    query = urlencode({"id": app["id"], "country": region, "entity": "software"})
    raw = client.request("https://itunes.apple.com/lookup?" + query)
    if raw is None:
        return {}, None
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list) or "resultCount" not in raw:
        raise FetchError("Unexpected Lookup response schema")
    results = raw["results"]
    if not results:
        if raw["resultCount"] != 0:
            raise FetchError("Inconsistent Lookup response")
        return {}, None
    match = next((item for item in results if str(item.get("trackId")) == app["id"]), None)
    if not match:
        raise FetchError("Lookup returned a different App ID")
    return {"app": product(match.get("trackName", app["id"]), match.get("price"), match.get("currency"), kind="app")}, match.get("trackName")


def fetch_iaps(client, app, region):
    query = urlencode({"platform": "web", "views": "top-in-app-purchasables", "l": app["language"].lower()})
    raw = client.request(f"https://apps.apple.com/api/apps/v1/catalog/{region}/apps/{app['id']}?{query}", headers={
        "Authorization": "Bearer", "Referer": f"https://apps.apple.com/{region}/app/id{app['id']}",
        "Accept-Language": app["language"],
    })
    if raw is None:
        return {}, None
    try:
        data = raw["data"][0]
        if str(data["id"]) != app["id"]:
            raise FetchError("Catalog returned a different App ID")
        view = data["views"]["top-in-app-purchasables"]
        entries = view["data"]
        if not isinstance(entries, list):
            raise TypeError
        # A partial/paginated response cannot prove that missing products disappeared.
        if view.get("next"):
            raise FetchError("Paginated IAP response requires parser update")
        found = {}
        for entry in entries:
            iap_id = str(entry["id"])
            if app["iap_ids"] and iap_id not in app["iap_ids"]:
                continue
            attributes = entry["attributes"]
            offers = attributes["offers"]
            if not isinstance(offers, list) or not offers:
                raise FetchError(f"IAP {iap_id} has no price offers")
            for offer in offers:
                period = offer.get("recurringSubscriptionPeriod")
                offer_type = str(offer.get("type", "default"))
                identity = json.dumps([iap_id, offer_type, period], sort_keys=True, separators=(",", ":"))
                key = "iap:" + identity
                item = product(attributes.get("name", iap_id), offer.get("price"), offer.get("currencyCode"),
                               kind="iap", iap_id=iap_id, offer_type=offer_type, period=period,
                               is_subscription=bool(attributes.get("isSubscription", False)))
                if key in found and found[key] != item:
                    raise FetchError(f"Ambiguous price offers for IAP {iap_id}")
                found[key] = item
        return found, data["attributes"].get("name")
    except (KeyError, IndexError, TypeError):
        raise FetchError("Unexpected IAP response schema") from None


def observe(state, scope_key, observed, app_name, config, now):
    scope = state["scopes"].setdefault(scope_key, {"initialized": False, "products": {}})
    scope["app_name"] = app_name or scope.get("app_name") or scope_key.split(":")[0]
    old_products = scope["products"]
    events = []
    def add_event(kind, key, old, new):
        event = {"id": uuid.uuid4().hex, "kind": kind, "scope": scope_key,
                 "app_name": scope["app_name"], "product_key": key,
                 "old": old, "new": new, "observed_at": now}
        state["pending_notifications"].append(event)
        events.append(event)
        LOG.info("Change %s %s %s", kind, scope_key, key)
    for key, item in observed.items():
        previous = old_products.get(key)
        if previous is None:
            if (scope["initialized"] and config["notify_new_products"]) or (not scope["initialized"] and config["notify_initial"]):
                add_event("added", key, None, item)
        elif not previous["active"]:
            add_event("restored", key, previous["value"], item)
        elif (previous["value"]["price"], previous["value"]["currency"]) != (item["price"], item["currency"]):
            add_event("price_changed", key, previous["value"], item)
        old_products[key] = {"value": item, "active": True, "missing_checks": 0, "last_seen": now}
    for key, previous in old_products.items():
        if key in observed or not previous["active"]:
            continue
        previous["missing_checks"] += 1
        if previous["missing_checks"] >= config["missing_confirmation_checks"]:
            previous["active"] = False
            add_event("removed", key, previous["value"], None)
    scope["initialized"] = True
    scope["last_success"] = now
    return events


def event_payload(event):
    app_id, region, _ = event["scope"].split(":")
    item = event["new"] or event["old"]
    labels = {"added": "新增商品", "restored": "商品重新出現", "removed": "商品不再列出", "price_changed": "價格變動"}
    color = 0x3498DB
    if event["kind"] == "price_changed" and event["old"]["currency"] == item["currency"]:
        decreased = Decimal(item["price"]) < Decimal(event["old"]["price"])
        color = 0x2ECC71 if decreased else 0xE67E22
        labels["price_changed"] = "降價" if decreased else "漲價"
    def display(value):
        return f"{value['currency']} {value['price']}" if value else "—"
    fields = [{"name": "地區", "value": region.upper(), "inline": True},
              {"name": "商品類型", "value": "App 本體" if item["kind"] == "app" else ("訂閱" if item.get("is_subscription") else "App 內購買"), "inline": True},
              {"name": "原價", "value": display(event["old"]), "inline": True},
              {"name": "現價", "value": display(event["new"]), "inline": True}]
    if item.get("iap_id"):
        fields.append({"name": "IAP ID", "value": item["iap_id"], "inline": True})
    if item.get("period"):
        fields.append({"name": "訂閱週期", "value": str(item["period"])[:1024], "inline": True})
    return {"allowed_mentions": {"parse": []}, "embeds": [{
        "title": f"{labels[event['kind']]} · {event['app_name']}"[:256],
        "description": item["name"][:4096], "color": color,
        "url": f"https://apps.apple.com/{region}/app/id{app_id}",
        "fields": fields, "timestamp": event["observed_at"],
        "footer": {"text": f"事件 {event['id']}"},
    }]}


def send_pending(client, state, state_path, webhook):
    sent = 0
    parsed = urlparse(webhook)
    query = dict(parse_qsl(parsed.query)) | {"wait": "true"}
    webhook = urlunparse(parsed._replace(query=urlencode(query)))
    for event in list(state["pending_notifications"]):
        client.request(webhook, payload=event_payload(event), apple=False)
        state["pending_notifications"].remove(event)
        save_state(state_path, state)
        sent += 1
        LOG.info("Discord delivered event %s", event["id"])
    return sent


def write_summary(stats):
    text = "App Store watchlist\n" + "\n".join(f"- {key}: {value}" for key, value in stats.items()) + "\n"
    LOG.info("Run summary: %s", ", ".join(f"{key}={value}" for key, value in stats.items()))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps", type=Path, default=ROOT / "apps.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--state", type=Path, default=ROOT / "data/state.json")
    parser.add_argument("--force", action="store_true", help="Ignore the check interval")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and compare without saving or sending")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        apps, config = load_settings(args.apps, args.config)
        LOG.setLevel(config["log_level"])
        if config["log_file"]:
            path = ROOT / config["log_file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            from logging.handlers import RotatingFileHandler
            handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(handler)
        state = read_json(args.state) if args.state.exists() else {
            "version": 1, "scopes": {}, "pending_notifications": [], "last_check": None,
        }
        if state.get("version") != 1:
            raise ValueError("Unsupported state version")
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            parsed = urlparse(webhook)
            if parsed.scheme != "https" or parsed.hostname not in ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com") or not parsed.path.startswith("/api/webhooks/"):
                raise ValueError("Invalid Discord webhook URL")
        if not args.dry_run and apps and not webhook:
            raise ValueError("Set DISCORD_WEBHOOK_URL or use --dry-run")
        now = utc_now()
        force = args.force or os.environ.get("FORCE_CHECK") == "true"
        due = force or not state["last_check"] or (datetime.fromisoformat(now) - datetime.fromisoformat(state["last_check"])).total_seconds() >= config["interval_hours"] * 3600
        client = Client(config)
        stats = {"successful_queries": 0, "failed_queries": 0, "changes": 0, "notifications_sent": 0}
        if due:
            for app in apps:
                for region in app["regions"]:
                    for kind, enabled, fetch in (("app", app["track_app"], fetch_app), ("iap", app["track_iap"], fetch_iaps)):
                        if not enabled:
                            continue
                        scope = f"{app['id']}:{region}:{kind}"
                        try:
                            items, name = fetch(client, app, region)
                            if kind == "iap" and app["iap_ids"] and scope in state["scopes"]:
                                # Editing the watchlist is not a storefront removal.
                                products = state["scopes"][scope]["products"]
                                for key in list(products):
                                    if products[key]["value"].get("iap_id") not in app["iap_ids"]:
                                        del products[key]
                            events = observe(state, scope, items, name, config, now)
                            stats["successful_queries"] += 1
                            stats["changes"] += len(events)
                            LOG.info("Fetched %s: %d products", scope, len(items))
                            for key, item in items.items():
                                LOG.debug("Product %s %s: %s %s (%s)", scope, key, item["currency"], item["price"], item["name"])
                            if not args.dry_run:
                                save_state(args.state, state)
                        except FetchError as error:
                            stats["failed_queries"] += 1
                            LOG.error("Fetch failed %s: %s", scope, error)
            if apps:
                state["last_check"] = now
                if not args.dry_run:
                    save_state(args.state, state)
        else:
            LOG.info("Check interval not elapsed; processing pending notifications only")
        if not apps:
            LOG.warning("apps.json is empty; add your App IDs and regions")
        notification_failed = False
        if state["pending_notifications"] and not args.dry_run:
            if not webhook:
                LOG.error("Pending notifications require DISCORD_WEBHOOK_URL")
                notification_failed = True
            else:
                try:
                    pending_before = len(state["pending_notifications"])
                    stats["notifications_sent"] = send_pending(client, state, args.state, webhook)
                except FetchError as error:
                    stats["notifications_sent"] = pending_before - len(state["pending_notifications"])
                    LOG.error("Discord delivery failed: %s; pending events retained", error)
                    notification_failed = True
        stats["pending_notifications"] = len(state["pending_notifications"])
        stats["dry_run"] = args.dry_run
        write_summary(stats)
        return int(bool(stats["failed_queries"] or notification_failed))
    except (ValueError, KeyError, TypeError, OSError) as error:
        # Configuration/state errors only; request exceptions never expose URLs.
        LOG.error("Configuration or state error (%s). Check JSON files, paths and environment settings.", type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
