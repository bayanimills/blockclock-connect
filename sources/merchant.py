"""Merchant sources - the owner's own shop on the clock.

Shopify: today's sales metrics + sale/milestone event frames, read with the
owner's own Admin token (moved here unchanged from the original sources.py).

BTCPay Server: settled-invoice count for today via the Greenfield API with a
self-issued key - the sovereign version of the same idea.

TOKEN SECURITY (applies to BOTH the Shopify token and the BTCPay key): the
credential lives only in the saved config on the user's own box. It goes out
in exactly one place - the auth header of a request to the user's own shop -
and must NEVER appear in an API response (public_options redacts it to
*_set / *_hint) or in any log line: the fetchers reduce every failure to a
status code / exception class name.
"""

import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

from clock import LED_GREEN, LED_RED, LED_AMBER, LED_WARM_WHITE, \
    show_number_path, show_text_path
from sources import (CURRENCIES, SHOPIFY_API_VERSION, SHOPIFY_TTL_S, _cached,
                     _fit_line, _frame, _http_redacted, ccy_symbol,
                     compact_sats, fit_city, is_offline, register_source)

log = logging.getLogger("sources")

SHOPIFY_FRAMES = ["revenue_today", "order_count", "units_today", "avg_order",
                  "revenue_sats", "last_city", "goal"]
DEFAULT_SHOPIFY_FRAMES = ["revenue_today", "order_count", "revenue_sats",
                          "goal"]
SHOPIFY_FRAME_LABELS = {
    "revenue_today": "Revenue today",
    "order_count": "Orders today",
    "units_today": "Items sold today",
    "avg_order": "Average order",
    "revenue_sats": "Revenue in sats",
    "last_city": "Last sale city",
    "goal": "Daily goal %",
}

SYNTHETIC_SHOPIFY = {"revenue": 1284.50, "order_count": 7, "units": 11,
                     "max_order": 420.0, "latest_id": 1007,
                     "latest_total": 149.0, "latest_city": "Brisbane",
                     "order_ids": [1001, 1002, 1003, 1004, 1005, 1006, 1007]}
SYNTHETIC_BTCPAY_SALES = 3


def shopify_day_key(tz_offset_hours=10):
    """Today's date in the shop's local timezone (fixed-offset, like the
    proven feeder: DST shifts the boundary an hour twice a year, harmless)."""
    try:
        off = float(tz_offset_hours)
    except (TypeError, ValueError):
        off = 10.0
    return datetime.now(timezone(timedelta(hours=off))).strftime("%Y-%m-%d")


def _shopify_get(domain, token, path, params=None, timeout=20):
    """Authenticated Shopify Admin GET -> (parsed json, None) or (None, why).
    `why` is a status code or exception CLASS NAME only - nothing that could
    ever carry the token into a log line or error message."""
    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    d, why = _http_redacted(url, timeout=timeout,
                            headers={"X-Shopify-Access-Token": token})
    if why is None and not isinstance(d, (dict, list)):
        return None, "bad payload"
    return d, why


def shopify_validate(domain, token, timeout=10):
    """Prove domain+token against GET shop.json. Returns (ok, message);
    the message is safe to show the user and never contains the token."""
    if not domain or not token:
        return False, "Enter both the shop domain and the Admin API token"
    if is_offline():
        return True, "Offline Test Shop"
    data, why = _shopify_get(domain, token, "shop.json", timeout=timeout)
    if isinstance(data, dict) and isinstance(data.get("shop"), dict):
        return True, data["shop"].get("name") or domain
    if why in ("HTTP 401", "HTTP 403"):
        return False, ("Shopify rejected that token (unauthorized). Check the "
                       "Admin API access token and that it has the "
                       "read_orders scope.")
    if why == "HTTP 404":
        return False, (f"'{domain}' doesn't answer the Admin API - double-"
                       "check your .myshopify.com domain")
    return False, f"Couldn't reach {domain} ({why or 'no response'})"


def _shopify_fetch_today(domain, token, tz_offset_hours):
    """One live pull of today's (shop-local) orders -> metrics dict or None.
    Ported from a proven predecessor ShopifyPoller.snapshot()."""
    try:
        off = float(tz_offset_hours)
    except (TypeError, ValueError):
        off = 10.0
    tz = timezone(timedelta(hours=off))
    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0,
                                           microsecond=0)
    params = {
        "status": "any",
        "created_at_min": start_local.astimezone(timezone.utc).isoformat(),
        "limit": 250,
        "fields": "id,created_at,total_price,current_total_price,line_items,"
                  "shipping_address,billing_address,financial_status",
    }
    data, why = _shopify_get(domain, token, "orders.json", params)
    if not isinstance(data, dict) or "orders" not in data:
        log.info("shopify orders fetch failed: %s", why or "bad payload")
        return None
    orders = data.get("orders") or []
    # exclude voided/refunded from revenue but keep for 'latest'
    revenue, units, counted, max_order = 0.0, 0, 0, 0.0
    for o in orders:
        fs = (o.get("financial_status") or "").lower()
        if fs in ("voided", "refunded"):
            continue
        try:
            total = float(o.get("current_total_price")
                          or o.get("total_price") or 0)
            revenue += total
            max_order = max(max_order, total)
        except (TypeError, ValueError):
            pass
        for li in o.get("line_items") or []:
            try:
                units += int(li.get("quantity") or 0)
            except (TypeError, ValueError):
                pass
        counted += 1
    latest = max(orders, key=lambda o: o.get("created_at", "")) if orders \
        else None
    city = None
    if latest:
        addr = latest.get("shipping_address") \
            or latest.get("billing_address") or {}
        city = addr.get("city")
    try:
        latest_total = (float(latest.get("current_total_price")
                              or latest.get("total_price") or 0)
                        if latest else None)
    except (TypeError, ValueError):
        latest_total = None
    return {
        "revenue": round(revenue, 2),
        "order_count": counted,
        "units": units,
        "max_order": round(max_order, 2),
        "latest_id": latest.get("id") if latest else None,
        "latest_total": latest_total,
        "latest_city": city,
        "order_ids": sorted(o["id"] for o in orders if "id" in o),
    }


def shopify_snapshot(domain, token, tz_offset_hours=10):
    """Today's sales metrics, cached ~60s and stale-tolerant (a Shopify blip
    serves the last good snapshot instead of blanking the frames). Returns
    None when nothing is available - callers skip, never crash."""
    if is_offline():
        return dict(SYNTHETIC_SHOPIFY)
    if not domain or not token:
        return None
    return _cached(f"shopify:{domain}", SHOPIFY_TTL_S,
                   lambda: _shopify_fetch_today(domain, token,
                                                tz_offset_hours))


def goal_color(revenue, goal):
    """Daily-goal thermometer: <33% red, 33-99% amber, >=100% green."""
    pct = (revenue / goal * 100) if goal else 0
    if pct >= 100:
        return LED_GREEN
    if pct >= 33:
        return LED_AMBER
    return LED_RED


def build_sale_alert(total, city, currency="AUD", count=1):
    """Event frame for new order(s). Several orders landing in one write
    window coalesce into a single 'N SALES' frame."""
    if count > 1:
        return {"name": "sale_alert_multi", "label": f"{count} new sales!",
                "source": "shopify",
                "path": show_number_path(count, tl="SALES!", br="new orders"),
                "slotargs": {"number": count, "color": "flash"}}
    amt = int(round(total or 0))
    sym = ccy_symbol(currency)
    br = _fit_line(city) or "your shop"
    return {"name": "sale_alert", "label": "Sale!", "source": "shopify",
            "path": show_number_path(amt, sym=sym, tl="SALE!", br=br),
            "slotargs": {"number": amt, "sym": sym, "color": "flash"}}


def build_milestone(kind, value):
    """Event frame for order-count milestones / a new daily-revenue record."""
    text = {"record": "RECORD", "first": "1ST", "count": "GOAL"}.get(kind,
                                                                     "NICE")
    labels = {"record": "New daily record", "first": "First sale of the day",
              "count": f"{value} orders today"}
    return {"name": "milestone_" + kind,
            "label": labels.get(kind, "Milestone"), "source": "shopify",
            "path": show_text_path(text, tl="new", br=str(value)[:12]),
            "slotargs": {"text": text, "color": "yellow_1"}}


def _shopify_frames(options, wanted=None):
    """The merchant sales frames. In the feeder's (strict) view only frames
    the user ticked AND put in the rotation render; the all-frames preview
    view shows everything. A Shopify failure (or missing token/domain)
    contributes nothing this cycle."""
    sel = [f for f in (options.get("frames") or []) if f in SHOPIFY_FRAMES]

    def want(fid):
        if wanted is None:
            return True
        return fid in wanted and fid in sel

    build = [f for f in SHOPIFY_FRAMES if want(f)]
    if not build:
        return []
    snap = shopify_snapshot(options.get("shop_domain"), options.get("token"),
                            options.get("tz_offset_hours", 10))
    if not snap:
        return []
    ccy = options.get("currency", "AUD")
    try:
        goal = float(options.get("daily_goal") or 0)
    except (TypeError, ValueError):
        goal = 0.0
    tint = goal_color(snap["revenue"], goal)   # money frames follow progress
    sym = ccy_symbol(ccy)
    money_tl = "shop today" if sym else f"today {ccy}"
    frames = []
    for name in build:
        if name == "revenue_today":
            amt = int(round(snap["revenue"]))
            frames.append(_frame(
                "revenue_today",
                show_number_path(amt, sym=sym, tl=money_tl,
                                 br=f"{snap['order_count']} orders"),
                number=amt, sym=sym, color=tint))
        elif name == "order_count":
            n = snap["order_count"]
            frames.append(_frame(
                "order_count",
                show_number_path(n, tl="orders", br="so far today"),
                number=n, color=LED_WARM_WHITE))
        elif name == "units_today":
            if snap.get("units"):
                frames.append(_frame(
                    "units_today",
                    show_number_path(snap["units"], tl="items sold",
                                     br="today"),
                    number=snap["units"], color=LED_WARM_WHITE))
        elif name == "avg_order":
            if snap["order_count"] > 0:
                aov = int(round(snap["revenue"] / snap["order_count"]))
                frames.append(_frame(
                    "avg_order",
                    show_number_path(aov, sym=sym, tl="avg order", br="today"),
                    number=aov, sym=sym, color=LED_WARM_WHITE))
        elif name == "revenue_sats":
            from sources.price import btc_price
            price = btc_price(ccy)
            sats = (int(round(snap["revenue"] / price * 100_000_000))
                    if price else None)
            val, unit = compact_sats(sats)   # never overflows the 7 digits
            if val is not None:
                frames.append(_frame(
                    "revenue_sats",
                    show_number_path(val, tl="today in sats",
                                     br=unit or "sats"),
                    number=val, color=tint))
        elif name == "last_city":
            city = fit_city(snap.get("latest_city"))
            if city:
                t = snap.get("latest_total")
                br = ((f"${int(t)}" if sym else f"{int(t)} {ccy}")
                      if t else None)
                frames.append(_frame(
                    "last_city",
                    show_text_path(city, tl="last sale from", br=br),
                    text=city, color=LED_WARM_WHITE))
        elif name == "goal":
            if goal > 0:
                pct = int(round(min(999, snap["revenue"] / goal * 100)))
                br = f"of ${int(goal)}" if sym else f"of {int(goal)} {ccy}"
                frames.append(_frame(
                    "goal",
                    show_number_path(pct, sym="%", tl="daily goal", br=br),
                    number=pct, sym="%", color=tint))
    return frames


# --------------------------------------------------------------------------- #
# BTCPay Server (Greenfield API, self-issued key - still sovereign)
# --------------------------------------------------------------------------- #

def btcpay_sales_today(base_url, store_id, api_key, tz_offset_hours=10):
    """Count of invoices SETTLED today (shop-local midnight), or None.
    The key goes out only in the Authorization header to the user's own
    BTCPay; failures reduce to a status/class name via _http_redacted."""
    if is_offline():
        return SYNTHETIC_BTCPAY_SALES
    base = str(base_url or "").strip().rstrip("/")
    store = str(store_id or "").strip()
    key = str(api_key or "").strip()
    if not (base.startswith(("http://", "https://")) and store and key):
        return None
    try:
        off = float(tz_offset_hours)
    except (TypeError, ValueError):
        off = 10.0
    tz = timezone(timedelta(hours=off))
    start = datetime.now(tz).replace(hour=0, minute=0, second=0,
                                     microsecond=0)

    def fetch():
        url = (f"{base}/api/v1/stores/{urllib.parse.quote(store)}/invoices?"
               + urllib.parse.urlencode({"status": "Settled",
                                         "startDate": int(start.timestamp())}))
        d, why = _http_redacted(url, headers={"Authorization": f"token {key}"})
        if isinstance(d, list):
            return len(d)
        if why:
            log.info("btcpay invoices fetch failed: %s", why)
        return None
    return _cached(f"btcpay:{base}:{store}", SHOPIFY_TTL_S, fetch)


def _btcpay_frames(options, wanted=None):
    if wanted is not None and "btcpay_sales" not in wanted:
        return []
    n = btcpay_sales_today(options.get("base_url"), options.get("store_id"),
                           options.get("api_key"),
                           options.get("tz_offset_hours", 10))
    if n is None or n > 9_999_999:
        return []
    return [_frame("btcpay_sales",
                   show_number_path(n, tl="sales", br="BTCPay today"),
                   number=n, color=LED_GREEN)]


def _btcpay_validate(src_out, fail):
    opts = src_out["options"]
    base = str(opts.get("base_url") or "").strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        fail("btcpay.base_url must start with http:// or https://")
        base = ""
    opts["base_url"] = base
    if src_out["enabled"] and not (base and opts.get("store_id")
                                   and opts.get("api_key")):
        fail("BTCPay needs the server URL, the store id and an API key")
        src_out["enabled"] = False


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

register_source(
    "shopify",
    label="Shopify (Merchant)",
    category="merchant",
    advanced=True,   # UI renders this collapsed, out of the way
    description="For shop owners: today's sales from your own Shopify store, "
                "live on the clock. Your Admin API token stays on this "
                "device and is only used to read your orders.",
    frames=[(f, SHOPIFY_FRAME_LABELS[f]) for f in SHOPIFY_FRAMES],
    builder=_shopify_frames,
    options_schema={
        "shop_domain": {"type": "text", "label": "Shop domain",
                        "default": "",
                        "placeholder": "myshop.myshopify.com"},
        "token": {"type": "password",
                  "label": "Admin API access token", "default": ""},
        "currency": {"type": "select", "label": "Currency",
                     "choices": CURRENCIES, "default": "AUD"},
        "daily_goal": {"type": "number", "label": "Daily goal",
                       "default": 0},
        "tz_offset_hours": {"type": "number",
                            "label": "Timezone offset (hours)",
                            "default": 10},
        "frames": {"type": "multi", "label": "Sales frames",
                   "choices": [{"id": f, "label": SHOPIFY_FRAME_LABELS[f]}
                               for f in SHOPIFY_FRAMES],
                   "default": DEFAULT_SHOPIFY_FRAMES},
        "flash_on_sale": {"type": "bool", "label": "Flash on new sale",
                          "default": True},
    },
    secrets=("token",),
)

register_source(
    "btcpay",
    label="BTCPay Server (Merchant)",
    category="merchant",
    advanced=True,
    description="Sales settled today on your own BTCPay Server (Greenfield "
                "API, self-issued key). The key stays on this device and "
                "only ever goes to your own server.",
    frames=[("btcpay_sales", "BTCPay sales today")],
    builder=_btcpay_frames,
    options_schema={
        "base_url": {"type": "text", "label": "BTCPay server URL",
                     "default": "",
                     "placeholder": "https://btcpay.example.com"},
        "store_id": {"type": "text", "label": "Store ID", "default": ""},
        "api_key": {"type": "password", "label": "API key (self-issued)",
                    "default": ""},
        "tz_offset_hours": {"type": "number",
                            "label": "Timezone offset (hours)",
                            "default": 10},
    },
    secrets=("api_key",),
    validate=_btcpay_validate,
)
