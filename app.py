import os, sqlite3, requests, time
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, g, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "liquidscan-secret-xyz")
DATABASE = os.path.join(os.path.dirname(__file__), "zones.db")

# ── Data sources — tried in order until one succeeds ─────────────
# Binance: deepest book, but geo-blocked on some server IPs
BINANCE_DEPTH  = "https://api.binance.com/api/v3/depth"
BINANCE_24H    = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_SYMS   = ["EURUSDC", "EURUSDT"]  # Binance symbols (used if Binance reachable)

# Kraken: real EUR/USD spot, 500 levels, no geo-block
KRAKEN_TICKER  = "https://api.kraken.com/0/public/Ticker"
KRAKEN_DEPTH   = "https://api.kraken.com/0/public/Depth"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIR    = "EURUSD"

# Coinbase: shallower (50 levels) but reliable fallback
COINBASE_TICKER = "https://api.coinbase.com/api/v3/brokerage/market/products/EUR-USD"
COINBASE_DEPTH  = "https://api.coinbase.com/api/v3/brokerage/market/product_book"

# CoinGecko: price only — last resort if all else fails
COINGECKO_URL  = "https://api.coingecko.com/api/v3/simple/price"

NEWS_API_KEY   = os.environ.get("NEWS_API_KEY", "")
NEWS_URL       = "https://newsapi.org/v2/everything"
SYMBOLS        = ["EURUSD"]  # unified display — actual source decided at runtime

# ── Simple in-process cache ───────────────────────────────────────
# Avoids hammering Binance on every browser poll.
# Each entry: {"data": ..., "ts": float}
_cache = {}

def cache_get(key, ttl=4):
    """Return cached value if fresher than ttl seconds, else None."""
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["data"]
    return None

def cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}

# ── DB ────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(e):
    db = getattr(g, "_database", None)
    if db: db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS zones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL DEFAULT 'EURUSD',
                zone_type   TEXT    NOT NULL,
                price_high  REAL    NOT NULL,
                price_low   REAL    NOT NULL,
                timeframe   TEXT    NOT NULL DEFAULT 'D1',
                bias        TEXT    NOT NULL DEFAULT 'neutral',
                notes       TEXT,
                weight      INTEGER NOT NULL DEFAULT 2,
                active      INTEGER NOT NULL DEFAULT 1,
                created_by  TEXT    NOT NULL DEFAULT 'admin',
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                price       REAL    NOT NULL,
                zone_id     INTEGER,
                score       REAL,
                tier        TEXT,
                reason      TEXT,
                fired_at    TEXT    NOT NULL
            );
        """)
        db.commit()

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def row_to_dict(r):
    return dict(r)

def _get(url, params=None, timeout=10):
    """Raw HTTP GET — returns parsed JSON or raises."""
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ── Source detection cache — remember which source worked ─────────
_working_source = {"ticker": None, "depth": None}

def fetch_ticker():
    """
    Try Binance first. On geo-block or error, fall through to
    Kraken, then Coinbase, then CoinGecko (price only).
    Returns a normalised dict regardless of source.
    """
    cached = cache_get("ticker_data", ttl=30)
    if cached:
        return cached

    # ── 1. Binance (EURUSDC) ──────────────────────────────────────
    try:
        d = _get(BINANCE_24H, {"symbol": "EURUSDC"}, timeout=8)
        if isinstance(d, dict) and "code" in d and d["code"] != 200:
            raise Exception("Binance geo-block: " + str(d.get("msg", "")))
        price    = float(d["lastPrice"])
        open_p   = float(d["openPrice"])
        high     = float(d["highPrice"])
        low      = float(d["lowPrice"])
        result = _normalise_ticker(price, open_p, high, low,
                                   float(d["bidPrice"]), float(d["askPrice"]),
                                   float(d["volume"]), float(d["priceChangePercent"]),
                                   source="binance")
        cache_set("ticker_data", result)
        _working_source["ticker"] = "binance"
        return result
    except Exception as e:
        pass  # fall through

    # ── 2. Kraken (EURUSD — real forex spot) ─────────────────────
    try:
        data = _get(KRAKEN_TICKER, {"pair": KRAKEN_PAIR}, timeout=10)
        if data.get("error"):
            raise Exception(str(data["error"]))
        pd = (data.get("result", {}).get("EURUSD")
              or data.get("result", {}).get("ZEURZUSD")
              or list(data.get("result", {}).values())[0])
        price  = float(pd["c"][0])
        open_p = float(pd["o"])
        high   = float(pd["h"][0])
        low    = float(pd["l"][0])
        result = _normalise_ticker(price, open_p, high, low,
                                   float(pd["b"][0]), float(pd["a"][0]),
                                   float(pd["v"][0]),
                                   round((price - open_p) / open_p * 100, 4),
                                   source="kraken")
        cache_set("ticker_data", result)
        _working_source["ticker"] = "kraken"
        return result
    except Exception as e:
        pass

    # ── 3. CoinGecko (price only — last resort) ───────────────────
    try:
        data = _get(COINGECKO_URL,
                    {"ids": "euro", "vs_currencies": "usd",
                     "include_24hr_change": "true"}, timeout=10)
        price = float(data["euro"]["usd"])
        chg   = float(data["euro"].get("usd_24h_change", 0))
        result = _normalise_ticker(price, price * (1 - chg/100),
                                   price * 1.002, price * 0.998,
                                   price - 0.00020, price + 0.00020,
                                   0, chg, source="coingecko")
        cache_set("ticker_data", result)
        _working_source["ticker"] = "coingecko"
        return result
    except Exception as e:
        raise Exception("All ticker sources failed")


def fetch_depth():
    """
    Try Binance depth first. Fall through to Kraken (500 levels),
    then Coinbase (50 levels aggregated).
    Returns (bids, asks) where each is [[price, qty], ...].
    """
    cached = cache_get("depth_data", ttl=900)
    if cached:
        return cached["bids"], cached["asks"], cached["source"]

    # ── 1. Binance ────────────────────────────────────────────────
    try:
        d = _get(BINANCE_DEPTH, {"symbol": "EURUSDC", "limit": 150}, timeout=8)
        if isinstance(d, dict) and "code" in d:
            raise Exception("Binance geo-block")
        bids = [[float(p), float(q)] for p, q in d["bids"]]
        asks = [[float(p), float(q)] for p, q in d["asks"]]
        cache_set("depth_data", {"bids": bids, "asks": asks, "source": "binance"})
        _working_source["depth"] = "binance"
        return bids, asks, "binance"
    except Exception:
        pass

    # ── 2. Kraken (500 levels — best fallback) ────────────────────
    try:
        data = _get(KRAKEN_DEPTH, {"pair": KRAKEN_PAIR, "count": 150}, timeout=10)
        if data.get("error"):
            raise Exception(str(data["error"]))
        pd   = (data.get("result", {}).get("EURUSD")
                or data.get("result", {}).get("ZEURZUSD")
                or list(data.get("result", {}).values())[0])
        bids = [[float(p), float(q)] for p, q, _ in pd["bids"]]
        asks = [[float(p), float(q)] for p, q, _ in pd["asks"]]
        cache_set("depth_data", {"bids": bids, "asks": asks, "source": "kraken"})
        _working_source["depth"] = "kraken"
        return bids, asks, "kraken"
    except Exception:
        pass

    # ── 3. Coinbase (50 aggregated levels) ───────────────────────
    try:
        data = _get(COINBASE_DEPTH, {"product_id": "EUR-USD", "limit": 50}, timeout=10)
        pb   = data.get("pricebook", {})
        bids = [[float(x["price"]), float(x["size"])] for x in pb.get("bids", [])]
        asks = [[float(x["price"]), float(x["size"])] for x in pb.get("asks", [])]
        cache_set("depth_data", {"bids": bids, "asks": asks, "source": "coinbase"})
        _working_source["depth"] = "coinbase"
        return bids, asks, "coinbase"
    except Exception:
        pass

    raise Exception("All depth sources failed")


def _normalise_ticker(price, open_p, high, low, bid, ask, volume, change_pct, source=""):
    change_abs = price - open_p
    trend      = "bullish" if change_abs > 0 else "bearish" if change_abs < 0 else "flat"
    return {
        "symbol":          "EURUSD",
        "price":           round(price, 5),
        "open":            round(open_p, 5),
        "high":            round(high, 5),
        "low":             round(low, 5),
        "bid":             round(bid, 5),
        "ask":             round(ask, 5),
        "spread":          round(ask - bid, 5),
        "volume":          round(volume, 2),
        "change_pct":      round(change_pct, 4),
        "change_abs":      round(change_abs, 5),
        "trend":           trend,
        "trend_pips":      round(abs(change_abs) / 0.0001, 1),
        "pips_from_high":  round((high - price) / 0.0001, 1),
        "pips_from_low":   round((price - low)  / 0.0001, 1),
        "day_range_pips":  round((high - low)   / 0.0001, 1),
        "range_position":  round((price - low) / (high - low) * 100, 1)
                           if high != low else 50,
        "source":          source,
    }


# ── Pages ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", symbols=SYMBOLS)


@app.route("/api/price")
def api_price():
    """
    Price + trend context. Tries Binance → Kraken → CoinGecko.
    Cached 30s.
    """
    try:
        t = fetch_ticker()
        result = {"tickers": [t], "ts": now_utc(), "source": t.get("source")}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "tickers": []}), 502

@app.route("/api/ticker")
def api_ticker():
    """Alias of /api/price — used by liquidity page."""
    try:
        t = fetch_ticker()
        return jsonify([t])
    except Exception as e:
        return jsonify([{"error": str(e)}]), 502


@app.route("/api/nearest_walls")
def api_nearest_walls():
    top_by_vol = int(request.args.get("top_by_vol", 30))
    n          = int(request.args.get("n", 8))
    min_gap    = float(request.args.get("min_gap", 0.0003))
    cache_key  = f"nearest_{top_by_vol}_{n}_{min_gap}"
    cached     = cache_get(cache_key, ttl=900)
    if cached:
        return jsonify(cached)
    try:
        # Get mid price from ticker
        t   = fetch_ticker()
        mid = (t["bid"] + t["ask"]) / 2

        # Get depth — fallback chain handles source selection
        bids, asks, depth_source = fetch_depth()

        # Combine and deduplicate within 0.0001
        combined = ([{"price": p, "qty": q, "side": "bid", "symbol": depth_source}
                     for p, q in bids] +
                    [{"price": p, "qty": q, "side": "ask", "symbol": depth_source}
                     for p, q in asks])
        combined.sort(key=lambda x: x["price"])
        deduped = []
        for lv in combined:
            last = deduped[-1] if deduped else None
            if (last and abs(lv["price"] - last["price"]) < 0.0001
                    and lv["side"] == last["side"]):
                if lv["qty"] > last["qty"]:
                    deduped[-1] = lv
            else:
                deduped.append(lv)

        # Sort by qty desc — volume/value ranking
        deduped.sort(key=lambda x: x["qty"], reverse=True)
        top_magnets = deduped[:top_by_vol]
        max_qty     = top_magnets[0]["qty"] if top_magnets else 1

        for lv in top_magnets:
            lv["dist"]     = round(abs(lv["price"] - mid), 5)
            lv["pips"]     = round(abs(lv["price"] - mid) / 0.0001, 1)
            lv["dist_pct"] = round(abs(lv["price"] - mid) / mid * 100, 4) if mid else 0
            lv["is_wall"]  = lv["qty"] >= max_qty * 0.35
            lv["bar_pct"]  = round(lv["qty"] / max_qty * 100, 1)

        bid_walls = sorted(
            [lv for lv in top_magnets if lv["side"] == "bid"
             and lv["price"] < mid and lv["dist"] >= min_gap],
            key=lambda x: x["dist"]
        )[:n]
        ask_walls = sorted(
            [lv for lv in top_magnets if lv["side"] == "ask"
             and lv["price"] > mid and lv["dist"] >= min_gap],
            key=lambda x: x["dist"]
        )[:n]

        result = {
            "mid":          round(mid, 5),
            "ts":           now_utc(),
            "ticker_source": t.get("source"),
            "depth_source": depth_source,
            "bid_walls":    bid_walls,
            "ask_walls":    ask_walls,
            "all_top":      top_magnets,
        }
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
@app.route("/zones")
def zones_page():
    db    = get_db()
    zones = db.execute(
        "SELECT * FROM zones ORDER BY active DESC, weight DESC, created_at DESC"
    ).fetchall()
    return render_template("zones.html", zones=[row_to_dict(z) for z in zones])

@app.route("/zones/add", methods=["GET", "POST"])
def add_zone():
    if request.method == "POST":
        f = request.form
        errors = []
        try:
            ph = float(f.get("price_high", ""))
            pl = float(f.get("price_low",  ""))
            if ph <= pl: errors.append("High must be greater than Low.")
        except:
            errors.append("Prices must be numbers.")
        try:
            w = int(f.get("weight", "2"))
            if not 1 <= w <= 5: raise ValueError
        except:
            errors.append("Weight must be 1–5.")
        if not f.get("zone_type"):
            errors.append("Zone type required.")
        if errors:
            for e in errors: flash(e, "error")
            return render_template("add_zone.html", form=f)
        ts = now_utc()
        get_db().execute(
            """INSERT INTO zones (symbol,zone_type,price_high,price_low,timeframe,
               bias,notes,weight,active,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,1,?,?,?)""",
            (f.get("symbol","EURUSD"), f.get("zone_type"), ph, pl,
             f.get("timeframe","D1"), f.get("bias","neutral"),
             f.get("notes",""), w, f.get("created_by","admin"), ts, ts))
        get_db().commit()
        flash("Zone saved.", "success")
        return redirect(url_for("zones_page"))
    return render_template("add_zone.html", form={})

@app.route("/zones/toggle/<int:zid>", methods=["POST"])
def toggle_zone(zid):
    db = get_db()
    z  = db.execute("SELECT active FROM zones WHERE id=?", (zid,)).fetchone()
    if z:
        db.execute("UPDATE zones SET active=?,updated_at=? WHERE id=?",
                   (0 if z["active"] else 1, now_utc(), zid))
        db.commit()
    return redirect(url_for("zones_page"))

@app.route("/zones/delete/<int:zid>", methods=["POST"])
def delete_zone(zid):
    get_db().execute("DELETE FROM zones WHERE id=?", (zid,))
    get_db().commit()
    return redirect(url_for("zones_page"))

# ── API: single combined endpoint (one round trip from browser) ───
@app.route("/api/dashboard")
def api_dashboard():
    """Combined dashboard endpoint — ticker + depth. Fallback chain."""
    try:
        t            = fetch_ticker()
        bids, asks, depth_src = fetch_depth()
        all_qty  = [q for _, q in bids + asks]
        max_qty  = max(all_qty) if all_qty else 1
        total_b  = sum(q for _, q in bids)
        total_a  = sum(q for _, q in asks)
        whale_t  = max_qty * 0.35
        book = {
            "bids_by_size":  sorted(bids, key=lambda x: x[1], reverse=True),
            "asks_by_size":  sorted(asks, key=lambda x: x[1], reverse=True),
            "bids_by_price": sorted(bids, key=lambda x: x[0], reverse=True),
            "asks_by_price": sorted(asks, key=lambda x: x[0]),
            "total_bid":     round(total_b, 2),
            "total_ask":     round(total_a, 2),
            "bid_pct":       round(total_b / (total_b + total_a) * 100, 1) if (total_b+total_a) else 50,
            "max_qty":       round(max_qty, 4),
            "whale_t":       round(whale_t, 4),
            "source":        depth_src,
        }
        return jsonify({
            "tickers": [t],
            "books":   {"EURUSD": book},
            "ts":      now_utc(),
            "source":  t.get("source"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502
@app.route("/api/candles")
def api_candles():
    """Candles — Binance primary, Kraken fallback (OHLC)."""
    interval = request.args.get("interval", "5m")
    limit    = int(request.args.get("limit", 80))
    cache_key = f"candles_{interval}"
    cached    = cache_get(cache_key, ttl=15 if interval in ("1m","5m") else 60)
    if cached:
        return jsonify(cached)
    # Binance
    try:
        raw = _get(BINANCE_KLINES, {"symbol": "EURUSDC", "interval": interval, "limit": limit})
        if isinstance(raw, dict) and "code" in raw:
            raise Exception("geo-block")
        candles = [{"t": c[0], "o": float(c[1]), "h": float(c[2]),
                    "l": float(c[3]), "c": float(c[4]), "v": float(c[5])} for c in raw]
        cache_set(cache_key, candles)
        return jsonify(candles)
    except Exception:
        pass
    # Kraken OHLC — map interval (Kraken uses minutes: 1,5,15,30,60,240,1440)
    interval_map = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}
    k_interval = interval_map.get(interval, 5)
    try:
        data = _get(KRAKEN_OHLC, {"pair": KRAKEN_PAIR, "interval": k_interval})
        if data.get("error"):
            raise Exception(str(data["error"]))
        pd = list(data.get("result", {}).values())[0]
        # Kraken OHLC: [time, open, high, low, close, vwap, volume, count]
        candles = [{"t": int(c[0])*1000, "o": float(c[1]), "h": float(c[2]),
                    "l": float(c[3]), "c": float(c[4]), "v": float(c[6])}
                   for c in pd[-limit:]]
        cache_set(cache_key, candles)
        return jsonify(candles)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
@app.route("/api/zones")
def api_zones():
    db    = get_db()
    sym   = request.args.get("symbol", "").upper()
    active = request.args.get("active", "1")
    min_w  = request.args.get("min_weight", "1")
    q, p   = "SELECT * FROM zones WHERE 1=1", []
    if sym:
        q += " AND symbol=?"; p.append(sym)
    if active != "all":
        try: q += " AND active=?"; p.append(int(active))
        except: pass
    try: q += " AND weight>=?"; p.append(int(min_w))
    except: pass
    q += " ORDER BY weight DESC, created_at DESC"
    rows = db.execute(q, p).fetchall()
    return jsonify({"count": len(rows), "zones": [row_to_dict(r) for r in rows]})

@app.route("/api/zones/walls")
def api_zones_walls():
    """Cross-reference depth walls with saved zones. Fallback chain."""
    sym       = request.args.get("symbol", "EURUSD").upper()
    tolerance = float(request.args.get("tolerance", "0.0010"))
    db        = get_db()
    try:
        t            = fetch_ticker()
        mid          = (t["bid"] + t["ask"]) / 2
        bids, asks, depth_src = fetch_depth()

        all_levels = ([{"price": p, "qty": q, "side": "bid"} for p, q in bids] +
                      [{"price": p, "qty": q, "side": "ask"} for p, q in asks])
        all_levels.sort(key=lambda x: x["qty"], reverse=True)

        zones = [row_to_dict(z) for z in
                 db.execute("SELECT * FROM zones WHERE active=1 ORDER BY weight DESC").fetchall()]

        confluent = []
        for wall in all_levels[:50]:
            wp = wall["price"]
            for zone in zones:
                zl, zh = zone["price_low"], zone["price_high"]
                if (zl - tolerance) <= wp <= (zh + tolerance):
                    confluent.append({
                        "wall_price":  wp, "wall_qty": wall["qty"],
                        "wall_side":   wall["side"],
                        "zone_id":     zone["id"], "zone_type": zone["zone_type"],
                        "zone_high":   zh, "zone_low": zl,
                        "zone_weight": zone["weight"], "zone_bias": zone["bias"],
                        "zone_notes":  zone["notes"],
                        "score":       round(zone["weight"] + min(wall["qty"] / 1000, 4), 2),
                    })
                    break
        confluent.sort(key=lambda x: x["score"], reverse=True)
        return jsonify({"count": len(confluent), "confluent": confluent, "source": depth_src})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
@app.route("/api/hit", methods=["POST"])
def api_hit():
    data = request.get_json(silent=True) or {}
    zid  = data.get("zone_id")
    hp   = data.get("hit_price")
    if not zid or hp is None:
        return jsonify({"error": "zone_id and hit_price required"}), 400
    get_db().execute(
        "INSERT INTO signals (symbol,direction,price,zone_id,score,tier,reason,fired_at) VALUES (?,?,?,?,?,?,?,?)",
        (data.get("symbol","EURUSD"), data.get("direction",""), hp, zid,
         data.get("score", 0), data.get("tier","watch"), data.get("reason",""), now_utc()))
    get_db().commit()
    return jsonify({"status": "logged"}), 201

# ── News ──────────────────────────────────────────────────────────
@app.route("/api/news")
def api_news():
    cached = cache_get("news", ttl=120)   # cache news for 2 minutes
    if cached:
        return jsonify(cached)
    if not NEWS_API_KEY:
        return jsonify({"error": "NEWS_API_KEY not set", "articles": []})
    try:
        r = requests.get(NEWS_URL, params={
            "q": "EUR USD forex", "language": "en",
            "sortBy": "publishedAt", "pageSize": 10,
            "apiKey": NEWS_API_KEY,
        }, timeout=10)
        data = r.json()
        result = {"articles": [{
            "title":       a["title"],
            "source":      a["source"]["name"],
            "url":         a["url"],
            "published":   a["publishedAt"][:16].replace("T", " "),
            "description": (a.get("description") or "")[:140],
        } for a in data.get("articles", [])]}
        cache_set("news", result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "articles": []})

@app.route("/api/health")
def api_health():
    cache_info = {k: round(time.time() - v["ts"], 1) for k, v in _cache.items()}
    return jsonify({
        "status":         "ok",
        "time":           now_utc(),
        "active_sources": _working_source,
        "cache_age_s":    cache_info,
    })


@app.route("/api/liquidity_zones")
def api_liquidity_zones():
    """
    Full depth pull for the Liquidity page.
    Tries Binance → Kraken → Coinbase.
    Cached 15 minutes server-side.
    """
    min_qty  = float(request.args.get("min_qty",  0))
    min_gap  = float(request.args.get("min_gap",  0.001))
    side     = request.args.get("side", "all").lower()
    lim      = int(request.args.get("limit", 300))
    cache_key = f"liq_zones_{min_qty}_{min_gap}_{side}"
    cached    = cache_get(cache_key, ttl=900)
    if cached:
        return jsonify(cached)
    try:
        t           = fetch_ticker()
        mid         = (t["bid"] + t["ask"]) / 2
        bids, asks, depth_source = fetch_depth()

        combined = ([{"price": p, "qty": q, "side": "bid", "symbol": depth_source}
                     for p, q in bids] +
                    [{"price": p, "qty": q, "side": "ask", "symbol": depth_source}
                     for p, q in asks])

        # Deduplicate within 0.0001
        combined.sort(key=lambda x: x["price"])
        deduped = []
        for lv in combined:
            last = deduped[-1] if deduped else None
            if (last and abs(lv["price"] - last["price"]) < 0.0001
                    and lv["side"] == last["side"]):
                if lv["qty"] > last["qty"]:
                    deduped[-1] = lv
            else:
                deduped.append(lv)

        # Apply filters
        if side != "all":
            deduped = [lv for lv in deduped if lv["side"] == side]
        if min_qty > 0:
            deduped = [lv for lv in deduped if lv["qty"] >= min_qty]
        if min_gap > 0 and mid > 0:
            deduped = [lv for lv in deduped if abs(lv["price"] - mid) >= min_gap]

        # Sort by qty descending
        deduped.sort(key=lambda x: x["qty"], reverse=True)

        max_qty = deduped[0]["qty"] if deduped else 1
        for i, lv in enumerate(deduped):
            lv["rank"]     = i + 1
            lv["dist"]     = round(abs(lv["price"] - mid), 5)
            lv["pips"]     = round(abs(lv["price"] - mid) / 0.0001, 1)
            lv["dist_pct"] = round(abs(lv["price"] - mid) / mid * 100, 4) if mid else None
            lv["is_wall"]  = lv["qty"] >= max_qty * 0.4
            lv["bar_pct"]  = round(lv["qty"] / max_qty * 100, 1)

        result = deduped[:lim]
        out = {
            "count":        len(result),
            "total_found":  len(deduped),
            "mid_price":    round(mid, 5),
            "min_qty":      min_qty,
            "min_gap":      min_gap,
            "pulled_at":    now_utc(),
            "source":       depth_source,
            "levels":       result,
        }
        cache_set(cache_key, out)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
@app.route("/liquidity")
def liquidity_page():
    return render_template("liquidity.html", symbols=SYMBOLS)



# ── Technical levels calculation helpers ─────────────────────────

def calc_pivots_classic(h, l, c):
    pp = (h + l + c) / 3
    return {
        "pp": pp,
        "r1": 2*pp - l,     "s1": 2*pp - h,
        "r2": pp + (h - l), "s2": pp - (h - l),
        "r3": h + 2*(pp-l), "s3": l - 2*(h-pp),
    }

def calc_pivots_camarilla(h, l, c):
    r = h - l
    return {
        "r4": c + r*1.1/2,  "s4": c - r*1.1/2,
        "r3": c + r*1.1/4,  "s3": c - r*1.1/4,
        "r2": c + r*1.1/6,  "s2": c - r*1.1/6,
        "r1": c + r*1.1/12, "s1": c - r*1.1/12,
    }

def calc_pivots_woodie(h, l, c, o):
    pp = (h + l + 2*c) / 4
    return {
        "pp": pp,
        "r1": 2*pp - l,     "s1": 2*pp - h,
        "r2": pp + (h - l), "s2": pp - (h - l),
        "r3": h + 2*(pp-l), "s3": l - 2*(h-pp),
    }

def calc_fibonacci(h, l):
    r = h - l
    levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    ext    = [1.272, 1.618, 2.0, 2.618]
    return {
        "high": h, "low": l, "range": r,
        "retracements": {
            f"{int(lvl*100)}%": round(h - r*lvl, 5) for lvl in levels
        },
        "extensions": {
            f"{int(lvl*100)}%": round(l - r*(lvl-1), 5) for lvl in ext
        },
    }

def calc_murray_math(price):
    """
    Murray Math levels — 8 octave lines on a power-of-8 price grid.
    For EUR/USD the natural grid is 0.125 (1/8 of 1.0).
    """
    grid = 0.125  # EUR/USD octave size
    base = round(price / grid) * grid
    lines = {}
    labels = ["0/8 — Major S", "1/8 — Weak S", "2/8 — S pivot",
              "3/8 — Lower trading", "4/8 — Major pivot",
              "5/8 — Upper trading", "6/8 — R pivot",
              "7/8 — Weak R", "8/8 — Major R"]
    for i in range(9):
        lines[labels[i]] = round(base - grid + i * (grid/8), 5)
    return {"grid": grid, "base": round(base, 5), "levels": lines}

def calc_vwap(candles):
    """
    VWAP from a list of candle dicts {h, l, c, v}.
    Returns vwap, upper_1, upper_2, lower_1, lower_2.
    """
    if not candles:
        return None
    cum_tv = 0.0
    cum_v  = 0.0
    sq_sum = 0.0
    for c in candles:
        tp      = (c["h"] + c["l"] + c["c"]) / 3
        cum_tv += tp * c["v"]
        cum_v  += c["v"]
    if cum_v == 0:
        return None
    vwap = cum_tv / cum_v
    # Standard deviation bands
    for c in candles:
        tp      = (c["h"] + c["l"] + c["c"]) / 3
        sq_sum += c["v"] * (tp - vwap) ** 2
    variance = sq_sum / cum_v if cum_v else 0
    std      = variance ** 0.5
    return {
        "vwap":    round(vwap, 5),
        "upper_1": round(vwap + std, 5),
        "upper_2": round(vwap + 2*std, 5),
        "lower_1": round(vwap - std, 5),
        "lower_2": round(vwap - 2*std, 5),
        "std":     round(std, 5),
    }

def get_kraken_ohlc(interval_min, limit=2):
    """
    Fetch OHLC from Kraken. Returns list of candle dicts.
    interval_min: 1,5,15,30,60,240,1440,10080
    limit: how many candles to return (most recent)
    """
    cache_key = f"ohlc_{interval_min}_{limit}"
    ttl = 60 if interval_min <= 15 else 300 if interval_min <= 60 else 900
    cached = cache_get(cache_key, ttl)
    if cached:
        return cached
    data = _get(KRAKEN_OHLC, {"pair": KRAKEN_PAIR, "interval": interval_min})
    if data.get("error"):
        raise Exception(str(data["error"]))
    raw = list(data.get("result", {}).values())[0]
    # [time, open, high, low, close, vwap, volume, count]
    candles = [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                "l": float(c[3]), "c": float(c[4]), "v": float(c[6])}
               for c in raw]
    result = candles[-limit:] if limit else candles
    cache_set(cache_key, result)
    return result


@app.route("/api/levels")
def api_levels():
    """
    Calculate all technical levels for a given timeframe.
    Uses the PREVIOUS completed candle for pivots/fib (not the current forming candle)
    and all available candles for VWAP.

    Query params:
      tf  — timeframe: 15m, 30m, h1, h4, d1 (default: d1)

    Returns: pivots (classic, camarilla, woodie), fibonacci, murray, vwap
    Cached per timeframe TTL.
    """
    tf_map = {
        "15m": (15,   "15 min"),
        "30m": (30,   "30 min"),
        "h1":  (60,   "H1"),
        "h4":  (240,  "H4"),
        "d1":  (1440, "Daily"),
    }
    tf = request.args.get("tf", "d1").lower()
    if tf not in tf_map:
        return jsonify({"error": f"Unknown tf. Use: {list(tf_map)}"}), 400

    interval_min, tf_label = tf_map[tf]
    cache_key = f"levels_{tf}"
    ttl = 60 if interval_min <= 15 else 300 if interval_min <= 60 else 900
    cached = cache_get(cache_key, ttl)
    if cached:
        return jsonify(cached)

    try:
        ticker  = fetch_ticker()
        price   = ticker["price"]

        # Get enough candles:
        # - 2 for pivot (we use the previous completed one, index -2)
        # - 200 for VWAP (full session)
        candles_all = get_kraken_ohlc(interval_min, limit=0)  # all available
        if len(candles_all) < 2:
            return jsonify({"error": "Not enough candle data"}), 502

        prev = candles_all[-2]   # previous completed candle
        curr = candles_all[-1]   # current forming candle

        # Pivots use previous candle H/L/C/O
        h, l, c, o = prev["h"], prev["l"], prev["c"], prev["o"]

        classic    = calc_pivots_classic(h, l, c)
        camarilla  = calc_pivots_camarilla(h, l, c)
        woodie     = calc_pivots_woodie(h, l, c, o)

        # Fibonacci: use the range of candles_all (swing high/low of last N candles)
        lookback = min(len(candles_all), 50)
        recent   = candles_all[-lookback:]
        fib_h    = max(c["h"] for c in recent)
        fib_l    = min(c["l"] for c in recent)
        fibonacci = calc_fibonacci(fib_h, fib_l)

        # Murray Math from current price
        murray = calc_murray_math(price)

        # VWAP — use all available candles for the session
        # For intraday TFs use up to 200 candles; for daily use 20
        vwap_candles = candles_all[-200:] if interval_min < 1440 else candles_all[-20:]
        vwap = calc_vwap(vwap_candles)

        # Round all levels to 5dp for clean display
        def rnd(d):
            return {k: round(v, 5) if isinstance(v, float) else v
                    for k, v in d.items()}

        result = {
            "tf":          tf,
            "tf_label":    tf_label,
            "price":       price,
            "prev_candle": {"h": h, "l": l, "c": c, "o": o,
                            "t": prev["t"]},
            "classic":     rnd(classic),
            "camarilla":   rnd(camarilla),
            "woodie":      rnd(woodie),
            "fibonacci":   fibonacci,
            "murray":      murray,
            "vwap":        vwap,
            "source":      ticker.get("source"),
            "ts":          now_utc(),
        }
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/levels")
def levels_page():
    return render_template("levels.html")


@app.route("/depth")
def depth_page():
    return render_template("depth.html")


@app.route("/api/depth_profile")
def api_depth_profile():
    """
    Volume profile / tail chart data.
    Groups bid and ask levels into price bands (buckets) of configurable
    pip width, sums the qty in each bucket, and returns them sorted by
    price so the chart can draw a horizontal bar chart — bid bars going
    left, ask bars going right, current price in the centre.

    This shows which price bands have the most liquidity concentration
    and whether buyers or sellers dominate near current price.

    Query params:
      bucket_pips  — pip width of each band (default 5)
      window_pips  — how many pips above/below mid to include (default 80)

    Cached 15 minutes (same as depth pull).
    """
    bucket_pips = float(request.args.get("bucket_pips", 5))
    window_pips = float(request.args.get("window_pips", 80))
    cache_key   = f"depth_profile_{bucket_pips}_{window_pips}"
    cached      = cache_get(cache_key, ttl=900)
    if cached:
        return jsonify(cached)
    try:
        t           = fetch_ticker()
        mid         = (t["bid"] + t["ask"]) / 2
        bids, asks, src = fetch_depth()

        bucket_size = bucket_pips * 0.0001
        window      = window_pips * 0.0001
        p_min       = mid - window
        p_max       = mid + window

        def bucket_levels(levels, side):
            buckets = {}
            for price, qty in levels:
                if price < p_min or price > p_max:
                    continue
                # Snap price to bucket boundary
                b_idx = round((price - p_min) / bucket_size)
                b_price = round(p_min + b_idx * bucket_size, 5)
                if b_price not in buckets:
                    buckets[b_price] = {"price": b_price, "qty": 0.0,
                                        "side": side, "count": 0}
                buckets[b_price]["qty"]   += qty
                buckets[b_price]["count"] += 1
            return list(buckets.values())

        bid_buckets = bucket_levels(bids, "bid")
        ask_buckets = bucket_levels(asks, "ask")

        # Combine and sort by price descending (highest price at top)
        all_buckets = bid_buckets + ask_buckets
        all_buckets.sort(key=lambda x: x["price"], reverse=True)

        # Normalise — max qty for bar scaling
        all_qty   = [b["qty"] for b in all_buckets]
        max_qty   = max(all_qty) if all_qty else 1
        total_bid = sum(b["qty"] for b in bid_buckets)
        total_ask = sum(b["qty"] for b in ask_buckets)

        for b in all_buckets:
            b["qty"]     = round(b["qty"], 2)
            b["bar_pct"] = round(b["qty"] / max_qty * 100, 1)
            b["pips_from_mid"] = round((b["price"] - mid) / 0.0001, 1)

        # Key zones — top 5 buckets by qty on each side
        top_bids = sorted(bid_buckets, key=lambda x: x["qty"], reverse=True)[:5]
        top_asks = sorted(ask_buckets, key=lambda x: x["qty"], reverse=True)[:5]

        result = {
            "mid":         round(mid, 5),
            "p_min":       round(p_min, 5),
            "p_max":       round(p_max, 5),
            "bucket_pips": bucket_pips,
            "window_pips": window_pips,
            "source":      src,
            "ts":          now_utc(),
            "total_bid":   round(total_bid, 2),
            "total_ask":   round(total_ask, 2),
            "bid_pct":     round(total_bid / (total_bid + total_ask) * 100, 1)
                           if (total_bid + total_ask) else 50,
            "buckets":     all_buckets,
            "top_bids":    top_bids,
            "top_asks":    top_asks,
        }
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
