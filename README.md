# MT5 Zone Feed — Flask App

## Architecture

```
Your PC (MT5 + mt5_zone_client.py)  ──HTTP──►  Namecheap Server (Flask + SQLite)
                                     ◄──JSON──  /api/zones?symbol=XAUUSD
```

The Flask app lives on Namecheap (always online). Your local MT5 client
polls it over your regular internet connection. No special networking needed.

---

## Namecheap Deployment (cPanel Python App)

1. **Log in to cPanel → Setup Python App**
2. Create app:
   - Python version: 3.10+ (or highest available)
   - Application root: `zones_app`  (relative to your home dir)
   - Application URL: your domain or subdomain
   - Application startup file: `passenger_wsgi.py`
   - Application Entry point: `application`
3. Upload ALL files (except `mt5_zone_client.py`) via File Manager
   or FTP into the application root folder.
4. In the Python App panel, click **"pip install"** and install:
   ```
   flask
   ```
   Or SSH in and run:
   ```bash
   source /path/to/virtualenv/bin/activate
   pip install flask
   ```
5. Click **Restart** in the Python App panel.
6. Visit your domain — the app auto-creates `zones.db` on first load.

### First-time DB init (if needed)
SSH into Namecheap and run:
```bash
cd ~/zones_app
source ../virtualenv/zones_app/3.10/bin/activate
python -c "from app import init_db; init_db()"
```

---

## Local MT5 Client Setup

```bash
pip install MetaTrader5 requests
```

Edit `mt5_zone_client.py` — change these three lines:
```python
BASE_URL   = "https://yourdomain.com"   # ← your actual domain
SYMBOLS    = ["XAUUSD", "NAS100"]       # ← symbols you trade
POLL_EVERY = 60                          # ← seconds between zone refreshes
```

Run it:
```bash
python mt5_zone_client.py
```

Keep it running alongside MT5. It will:
- Fetch active zones from your server every 60s
- Detect OBs and FVGs from MT5 candle data
- Score confluence in real time
- Print alerts when price approaches a zone

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/zones` | GET | All active zones (JSON) |
| `/api/zones?symbol=XAUUSD` | GET | Filter by symbol |
| `/api/zones?active=all` | GET | Include inactive zones |
| `/api/zones?min_weight=3` | GET | Minimum weight filter |
| `/api/zones/<id>` | GET | Single zone detail |
| `/api/hit` | POST | Log a zone hit from MT5 |
| `/api/health` | GET | Server health check |

### Zone hit payload (POST /api/hit)
```json
{ "zone_id": 5, "hit_price": 2341.50, "outcome": "bounce" }
```

---

## Confluence Score

| Points | Source |
|---|---|
| 1–5 | Zone weight (set manually) |
| +2–3.5 | Order block overlap (proportional to OB strength) |
| +2 | FVG overlap |
| +1 | Bias match (zone bias == OB direction) |
| +1.5 | Price already inside the zone |

| Total Score | Tier |
|---|---|
| < 6 | 👁 Watch |
| 6–8 | 🟡 High probability |
| 9+ | 🔥 Bold bet |

---

## Zone Weight Guide

| Weight | Use for |
|---|---|
| 1 | Custom reference, low-confidence levels |
| 2 | Daily S&D zones, intraday structure |
| 3 | HTF structure, weekly high/low |
| 4 | Monthly/macro levels, major liquidity |
| 5 | Once-in-a-while bold bet targets |
