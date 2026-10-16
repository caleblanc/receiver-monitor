#!/usr/bin/env python3
"""Surround receiver deal monitor for Canadian retailers -> iPhone push (ntfy or Pushover).

Usage:
  python monitor.py                 # normal check: alert on new listings / price drops, save state
  python monitor.py --dry-run       # check every source, print matches, send nothing, save nothing
  python monitor.py --test-notify   # send one test notification and exit

Notification settings come from environment variables:
  NTFY_TOPIC (required for ntfy), NTFY_SERVER (default https://ntfy.sh), NTFY_TOKEN (optional)
  PUSHOVER_USER + PUSHOVER_TOKEN (use Pushover instead of ntfy)
Only the standard library is used, so no pip install is needed.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get("MONITOR_STATE", ROOT / "state.json"))
LOG_PATH = Path(os.environ.get("MONITOR_LOG", ROOT / "monitor.log"))

log = logging.getLogger("monitor")


# --------------------------------------------------------------------------- matching

@dataclass(frozen=True)
class Model:
    name: str
    tier: int
    channels: str
    atmos: bool
    caveat: str | None
    pattern: re.Pattern


def _model_pattern(model: str) -> re.Pattern:
    # "AVR-S760H" -> AVR[-\s]?S[-\s]?760[-\s]?H, so "AVRS760H" and "AVR S760H" also match.
    # A trailing digit is not allowed (AVR-S760H must not match AVR-S7600H); trailing letters
    # are (colour suffixes such as RX-V485BL, VSX-834-K).
    runs = re.findall(r"[A-Za-z]+|\d+", model)
    body = r"[-\s_/]?".join(re.escape(r) for r in runs)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?!\d)", re.IGNORECASE)


MODELS = [Model(m, t, ch, at, cv, _model_pattern(m)) for m, t, ch, at, cv in config.MODELS]
EXCLUDE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in config.EXCLUDE_WORDS) + r")s?\b", re.IGNORECASE
)


def match_model(title: str) -> Model | None:
    """Return the watched model this title is for, or None if it is not a wanted receiver."""
    if EXCLUDE_RE.search(title):
        return None
    hits = [m for m in MODELS if m.pattern.search(title)]
    if len(hits) != 1:
        # zero: not on the list. Several: a comparison/compatibility listing, not one receiver.
        return None
    return hits[0]


def condition_from_title(title: str, default: str = "New") -> str:
    t = title.lower()
    m = re.search(r"refurbished\s*\(?\s*-?\s*(excellent|good|fair|very good)\s*\)?", t)
    if m:
        return f"Refurbished ({m.group(1).title()})"
    if "certified refurb" in t or "factory refurb" in t:
        return "Refurbished (certified)"
    if "refurb" in t or "renewed" in t:
        return "Refurbished"
    if "open box" in t or "open-box" in t:
        return "Open box"
    if re.search(r"\bused\b|pre-owned", t):
        return "Used"
    return default


# --------------------------------------------------------------------------- HTTP

class SourceError(Exception):
    """A source could not be read or returned something that no longer looks right."""


_last_hit: dict[str, float] = {}


def http_get(url: str, accept: str = "application/json", retries: int = 2) -> bytes:
    host = urllib.parse.urlparse(url).netloc
    for attempt in range(retries + 1):
        wait = _last_hit.get(host, 0) + config.MIN_DELAY_SECONDS + random.uniform(0, config.JITTER_SECONDS) - time.time()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": config.USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-CA,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=config.TIMEOUT_SECONDS) as resp:
                _last_hit[host] = time.time()
                return resp.read()
        except urllib.error.HTTPError as e:
            _last_hit[host] = time.time()
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                delay = int(e.headers.get("Retry-After") or 0) or 5 * 3 ** attempt
                log.warning("HTTP %s from %s, retrying in %ss", e.code, host, delay)
                time.sleep(min(delay, 60))
                continue
            raise SourceError(f"HTTP {e.code} from {url}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            _last_hit[host] = time.time()
            if attempt < retries:
                log.warning("Network error from %s (%s), retrying", host, e)
                time.sleep(5 * 3 ** attempt)
                continue
            raise SourceError(f"Network error for {url}: {e}") from e
    raise AssertionError("unreachable")


def get_json(url: str, accept: str = "application/json"):
    raw = http_get(url, accept)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SourceError(f"Not JSON (blocked or layout changed?) from {url}: {raw[:120]!r}") from e


# --------------------------------------------------------------------------- listings

@dataclass
class Listing:
    source: str
    id: str
    title: str
    model: Model
    condition: str
    price: float
    seller: str
    url: str
    available: bool = True
    note: str = ""          # e.g. "on sale, was $549.99", "ships" / "store pickup"
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


def _require(cond: bool, msg: str):
    if not cond:
        raise SourceError(msg)


# ---- Best Buy Canada (JSON search API + availability API)

BB_SEARCH = "https://www.bestbuy.ca/api/v2/json/search"
BB_AVAIL = "https://www.bestbuy.ca/ecomm-api/availability/products"
BB_RECEIVER_CATEGORY = "20313"  # "Home Theatre Receivers"


def bestbuy() -> list[Listing]:
    # Each search takes ~10 s server-side, so keep the count low. The whole receiver category
    # (new, open box, refurbished, clearance, Marketplace) is ~100 products, which covers the
    # open-box collection page. The two uncategorised queries catch Marketplace listings filed
    # under the wrong category.
    queries = [{"query": "", "categoryid": BB_RECEIVER_CATEGORY, "pages": 5},
               {"query": "open box receiver", "pages": 1},
               {"query": "refurbished receiver", "pages": 1}]

    products: dict[str, dict] = {}
    category_count = 0
    for q in queries:
        max_pages = q.pop("pages")
        for page in range(1, max_pages + 1):
            params = {"currentRegion": config.REGION, "lang": "en-CA", "page": page, "pageSize": 100,
                      "sortBy": "relevance", "sortDir": "desc", **q}
            data = get_json(f"{BB_SEARCH}?{urllib.parse.urlencode(params)}")
            _require(isinstance(data, dict) and isinstance(data.get("products"), list) and "total" in data,
                     "Best Buy search response no longer has 'products'/'total' (API changed?)")
            for p in data["products"]:
                _require({"sku", "name", "salePrice"} <= p.keys(), "Best Buy product missing sku/name/salePrice")
                products[str(p["sku"])] = p
            if q.get("categoryid"):
                category_count += len(data["products"])
            if page >= int(data.get("totalPages") or 1):
                break
    _require(category_count >= 10, f"Best Buy receiver category returned only {category_count} products "
                                   "(blocked, category renumbered, or API changed?)")

    candidates: list[Listing] = []
    for sku, p in products.items():
        model = match_model(p["name"])
        price = float(p.get("salePrice") or 0)
        if not model or price < config.PRICE_FLOOR:
            continue
        seller = (p.get("seller") or {}).get("name") or "Best Buy"
        if p.get("isMarketplace"):
            seller = f"{seller} (Marketplace)"
        cond = condition_from_title(p["name"])
        notes = []
        if p.get("isClearance"):
            notes.append("clearance")
        regular = float(p.get("regularPrice") or 0)
        if regular > price + 0.5:
            notes.append(f"on sale, reg. ${regular:,.2f}")
        candidates.append(Listing(
            source="bestbuy", id=sku, title=p["name"], model=model, condition=cond, price=price,
            seller=seller, url="https://www.bestbuy.ca" + (p.get("productUrl") or f"/en-ca/product/{sku}"),
            note=", ".join(notes),
        ))

    # Search results include sold-out listings (old open-box SKUs linger for years), so check stock.
    for i in range(0, len(candidates), 40):
        chunk = candidates[i:i + 40]
        params = {"accept": "application/vnd.bestbuy.standardproduct.v1+json", "accept-language": "en-CA",
                  "postalCode": config.POSTAL_PREFIX, "skus": "|".join(c.id for c in chunk)}
        data = get_json(f"{BB_AVAIL}?{urllib.parse.urlencode(params)}",
                        accept="application/vnd.bestbuy.simpleproduct.v1+json")
        _require(isinstance(data, dict) and isinstance(data.get("availabilities"), list),
                 "Best Buy availability response changed shape")
        avail = {str(a.get("sku")): a for a in data["availabilities"]}
        for c in chunk:
            a = avail.get(c.id, {})
            ships = bool((a.get("shipping") or {}).get("purchasable"))
            pickup = bool((a.get("pickup") or {}).get("purchasable"))
            c.available = ships or pickup
            how = " + ".join(x for x, ok in (("ships", ships), ("store pickup", pickup)) if ok)
            c.note = ", ".join(x for x in (c.note, how) if x)
    return candidates


# ---- MTC Factory Outlet (WooCommerce Store API)

MTC_API = "https://mtcfactoryoutlet.com/wp-json/wc/store/v1/products"


def mtc() -> list[Listing]:
    out: list[Listing] = []
    seen = 0
    for page in range(1, 11):  # whole catalog is ~2-3 pages of 100
        data = get_json(f"{MTC_API}?per_page=100&page={page}")
        _require(isinstance(data, list), "MTC Store API no longer returns a product list")
        if not data:
            break
        seen += len(data)
        for p in data:
            _require({"id", "name", "prices", "permalink"} <= p.keys(), "MTC product missing expected fields")
            title = html.unescape(p["name"])
            model = match_model(title)
            if not model:
                continue
            prices = p["prices"]
            price = int(prices["price"]) / 10 ** int(prices.get("currency_minor_unit", 2))
            if price < config.PRICE_FLOOR:
                continue
            out.append(Listing(
                source="mtc", id=str(p["id"]), title=title, model=model,
                condition=condition_from_title(title, default="Outlet (check listing for condition)"),
                price=price, seller="MTC Factory Outlet", url=p["permalink"],
                available=bool(p.get("is_in_stock")),
            ))
        if len(data) < 100:
            break
    _require(seen > 0, "MTC returned zero products (blocked or API changed?)")
    return out


# ---- Skadi (Shopify products.json)

SKADI_API = "https://www.skadi.ca/products.json"


def skadi() -> list[Listing]:
    out: list[Listing] = []
    seen = 0
    for page in range(1, 21):
        data = get_json(f"{SKADI_API}?limit=250&page={page}")
        _require(isinstance(data, dict) and isinstance(data.get("products"), list),
                 "Skadi products.json changed shape")
        items = data["products"]
        if not items:
            break
        seen += len(items)
        for p in items:
            _require({"id", "title", "handle", "variants"} <= p.keys(), "Skadi product missing expected fields")
            model = match_model(p["title"])
            if not model:
                continue
            variants = [v for v in p["variants"] if v.get("price")]
            live = [v for v in variants if v.get("available")]
            pool = live or variants
            if not pool:
                continue
            price = min(float(v["price"]) for v in pool)
            if price < config.PRICE_FLOOR:
                continue
            out.append(Listing(
                source="skadi", id=str(p["id"]), title=p["title"], model=model,
                condition=condition_from_title(p["title"]), price=price, seller="Skadi",
                url=f"https://www.skadi.ca/products/{p['handle']}", available=bool(live),
            ))
        if len(items) < 250:
            break
    _require(seen > 0, "Skadi returned zero products (blocked or API changed?)")
    return out


SOURCES = {"bestbuy": ("Best Buy Canada", bestbuy), "mtc": ("MTC Factory Outlet", mtc), "skadi": ("Skadi", skadi)}


# --------------------------------------------------------------------------- notifications

def notify(title: str, message: str, url: str | None = None, priority: int = 3, tags: list[str] | None = None) -> None:
    if os.environ.get("PUSHOVER_USER") and os.environ.get("PUSHOVER_TOKEN"):
        body = {"token": os.environ["PUSHOVER_TOKEN"], "user": os.environ["PUSHOVER_USER"],
                "title": title[:250], "message": message[:1024], "priority": max(-2, min(1, priority - 3))}
        if url:
            body.update(url=url, url_title="Open listing")
        req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                     data=urllib.parse.urlencode(body).encode(), method="POST")
    else:
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            raise RuntimeError("No notifier configured: set NTFY_TOPIC (or PUSHOVER_USER + PUSHOVER_TOKEN)")
        body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
        if url:
            body.update(click=url, actions=[{"action": "view", "label": "Open listing", "url": url}])
        headers = {"Content-Type": "application/json"}
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        req = urllib.request.Request(os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def format_alert(listing: Listing, reason: str) -> tuple[str, str, int, list[str]]:
    m = listing.model
    over = listing.price > config.BUDGET
    title = f"{'⚠️ OVER BUDGET · ' if over else ''}{m.name} ${listing.price:,.2f} · {listing.condition}"
    lines = [
        reason,
        f"Model: {m.name} ({m.channels} ch{', Dolby Atmos' if m.atmos else ''})",
        f"Condition: {listing.condition}",
        f"Price: ${listing.price:,.2f} CAD" + (f" ({listing.note})" if listing.note else ""),
        f"Seller: {listing.seller}",
        f"Tier {m.tier}" + (f" — {m.caveat}" if m.caveat else ""),
    ]
    if over:
        lines.append(f"Over your ${config.BUDGET:,.0f} budget (cap ${config.ALERT_CAP:,.0f})")
    lines.append(listing.title)
    priority = 4 if (m.tier == 1 and not over) else 3
    tags = ["loudspeaker"] + (["warning"] if over or m.caveat else [])
    return title, "\n".join(lines), priority, tags


# --------------------------------------------------------------------------- state

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    else:
        state = {}
    state.setdefault("listings", {})
    state.setdefault("sources", {})
    state.setdefault("meta", {})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def decide(listing: Listing, state: dict, at: datetime) -> str | None:
    """Update state for one listing; return the alert reason, or None for no alert."""
    rec = state["listings"].get(listing.key)
    eligible = listing.available and listing.price <= config.ALERT_CAP

    if not listing.available:
        if rec and rec.get("available", True):
            rec["available"] = False
            rec["unavailable_since"] = iso(at)
        return None
    if not eligible:
        # In stock but above the cap. Unknown listings stay untracked, so dropping under the cap
        # later alerts as new; known ones remember the higher price so the drop back down alerts.
        if rec:
            rec["price"] = max(rec["price"], listing.price)
            rec["available"] = True
            rec.pop("unavailable_since", None)
        return None

    if rec is None:
        state["listings"][listing.key] = {"price": listing.price, "alerted_at": iso(at), "available": True,
                                         "model": listing.model.name, "title": listing.title[:120]}
        return "🆕 New listing"

    reason = None
    if listing.price < rec["price"] - 0.99:
        reason = f"📉 Price drop: was ${rec['price']:,.2f}"
    elif not rec.get("available", True):
        since = datetime.fromisoformat(rec.get("unavailable_since", iso(at)))
        if at - since >= timedelta(hours=config.BACK_IN_STOCK_HOURS):
            reason = "🔁 Back in stock"
    rec["available"] = True
    rec.pop("unavailable_since", None)
    if reason:
        rec["price"] = listing.price
        rec["alerted_at"] = iso(at)
    elif listing.price > rec["price"]:
        rec["price"] = listing.price  # price went up: a later drop back down is worth hearing about
    return reason


def mark_vanished(state: dict, source: str, present_keys: set[str], at: datetime) -> None:
    """Listings that disappeared from a source that was read successfully count as unavailable."""
    for key, rec in state["listings"].items():
        if key.startswith(source + ":") and key not in present_keys and rec.get("available", True):
            rec["available"] = False
            rec["unavailable_since"] = iso(at)


def source_health(state: dict, source: str, label: str, error: str | None, at: datetime) -> tuple[str, str] | None:
    """Track failures. Returns a (title, message) notification when a source breaks or recovers."""
    s = state["sources"].setdefault(source, {"failures": 0})
    if error is None:
        was_broken = s.get("broken_alerted")
        state["sources"][source] = {"failures": 0}
        if was_broken:
            return (f"✅ {label} is working again", "The monitor can read this source again.")
        return None
    s["failures"] = s.get("failures", 0) + 1
    s["last_error"] = error[:300]
    s["last_error_at"] = iso(at)
    skip_runs = min(2 ** max(0, s["failures"] - 1), config.MAX_BACKOFF_RUNS)
    s["skip_until"] = iso(at + timedelta(minutes=20 * (skip_runs - 1))) if skip_runs > 1 else None
    if s["failures"] >= config.BROKEN_AFTER_FAILURES and not s.get("broken_alerted"):
        s["broken_alerted"] = True
        return (f"🛠️ {label} check is broken",
                f"Failed {s['failures']} runs in a row. Latest error:\n{error[:400]}\n"
                "The site may have changed or be blocking the monitor. Other sources still run.")
    return None


# --------------------------------------------------------------------------- main

def run(dry_run: bool, only: list[str] | None = None) -> int:
    state = load_state()
    at = now()
    alerts: list[tuple[Listing, str]] = []
    system_msgs: list[tuple[str, str]] = []

    for key, (label, fn) in SOURCES.items():
        if only and key not in only:
            continue
        s = state["sources"].get(key, {})
        if not dry_run and s.get("skip_until") and datetime.fromisoformat(s["skip_until"]) > at:
            log.info("%s: backing off after %s failures (until %s)", label, s.get("failures"), s["skip_until"])
            continue
        try:
            listings = fn()
            error = None
        except SourceError as e:
            listings, error = [], str(e)
            log.error("%s failed: %s", label, e)
        except Exception as e:  # unexpected bug in a parser: keep the other sources running
            listings, error = [], f"{type(e).__name__}: {e}"
            log.exception("%s crashed", label)

        if error is None:
            log.info("%s: %d matching listings (%d in stock, %d at/under $%.0f)", label, len(listings),
                     sum(l.available for l in listings),
                     sum(l.available and l.price <= config.ALERT_CAP for l in listings), config.ALERT_CAP)
            for l in sorted(listings, key=lambda l: l.price):
                log.info("   %-10s $%8.2f  %-5s %-28s %s", l.model.name, l.price,
                         "stock" if l.available else "sold", l.condition[:28], l.seller)
            for l in listings:
                reason = decide(l, state, at)
                if reason:
                    alerts.append((l, reason))
            mark_vanished(state, key, {l.key for l in listings}, at)
        msg = source_health(state, key, label, error, at)
        if msg:
            system_msgs.append(msg)

    last_hb = state["meta"].get("last_heartbeat")
    if not last_hb or at - datetime.fromisoformat(last_hb) >= timedelta(days=config.HEARTBEAT_DAYS):
        if last_hb:  # skip on the very first run
            broken = [k for k, s in state["sources"].items() if s.get("failures")]
            system_msgs.append(("💤 Receiver monitor is alive",
                                f"Weekly check-in. Watching {len(state['listings'])} known listings."
                                + (f" Failing sources: {', '.join(broken)}" if broken else " All sources OK.")))
        state["meta"]["last_heartbeat"] = iso(at)

    if dry_run:
        for l, reason in alerts:
            title, body, _, _ = format_alert(l, reason)
            print(f"\n[WOULD ALERT] {title}\n{body}\n{l.url}")
        for t, b in system_msgs:
            print(f"\n[WOULD ALERT] {t}\n{b}")
        print(f"\nDry run: {len(alerts)} listing alert(s), {len(system_msgs)} system message(s). Nothing sent or saved.")
        return 0

    sent_ok = True
    try:
        alerts.sort(key=lambda a: (a[0].model.tier, a[0].price))
        for l, reason in alerts[:config.MAX_ALERTS_PER_RUN]:
            title, body, prio, tags = format_alert(l, reason)
            notify(title, body, l.url, prio, tags)
            log.info("ALERT sent: %s", title)
        if len(alerts) > config.MAX_ALERTS_PER_RUN:
            rest = alerts[config.MAX_ALERTS_PER_RUN:]
            notify(f"…and {len(rest)} more receiver listings",
                   "\n".join(f"{l.model.name} ${l.price:,.2f} {l.condition} — {l.seller}" for l, _ in rest))
        for t, b in system_msgs:
            notify(t, b, priority=2 if t.startswith("💤") else 4, tags=["gear"])
    except Exception:
        sent_ok = False
        log.exception("Sending notifications failed; state not saved so alerts retry next run")

    if sent_ok:
        save_state(state)
    return 0 if sent_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check sources and print, send/save nothing")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--source", action="append", choices=list(SOURCES), help="only check this source")
    args = ap.parse_args()

    handlers = [logging.StreamHandler(sys.stdout)]
    if not os.environ.get("GITHUB_ACTIONS"):
        handlers.append(logging.FileHandler(LOG_PATH))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    if args.test_notify:
        notify("🔊 Receiver monitor test", "If you can read this on your iPhone, notifications work.\n"
               "Tap to open Best Buy's open-box receivers.",
               "https://www.bestbuy.ca/en-ca/collection/open-box-receivers/382433", priority=4, tags=["loudspeaker"])
        log.info("Test notification sent")
        return 0
    return run(args.dry_run, args.source)


if __name__ == "__main__":
    sys.exit(main())
