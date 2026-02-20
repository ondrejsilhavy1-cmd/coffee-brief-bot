import os, json, time, threading, feedparser, requests, yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv
import telebot
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
import websocket

load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# === CONFIG ===
RSS_FEEDS = [
    "https://rsshub.app/twitter/user/zerohedge",
    "https://rsshub.app/twitter/user/unusual_whales",
    "https://rsshub.app/twitter/user/MarioNawfal",
    "https://rsshub.app/twitter/user/KobeissiLetter",
    "https://rsshub.app/twitter/user/deltaone",
    "https://rsshub.app/twitter/user/watcherguru",
    "https://rsshub.app/twitter/user/spectatorindex",
    "https://liveuamap.com/rss",
    "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=rss&timespan=24h&query=conflict+OR+military+OR+escalation+OR+protest+OR+strike+OR+geopolitics",
]

LAST_BRIEF_FILE = "last_brief.txt"
YOUR_REGIONS = "IR,IL,SY,RU,UA"

LIQ_THRESHOLDS = {"BTC": 200000, "ETH": 200000, "SOL": 100000}
liq_cache = []
liq_lock = threading.Lock()
COINS_TO_WATCH = ["BTC", "ETH", "SOL"]

def get_last_brief_time():
    try:
        with open(LAST_BRIEF_FILE, "r") as f:
            return datetime.fromisoformat(f.read().strip())
    except:
        return datetime.now() - timedelta(days=1)

def save_last_brief_time():
    with open(LAST_BRIEF_FILE, "w") as f:
        f.write(datetime.now().isoformat())

def get_osint_news():
    articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            articles.append(f"• {entry.title[:120]} — {entry.link}")
    
    # ACLED OAuth (updated)
    try:
        token_url = "https://acleddata.com/oauth/token"
        payload = {
            "username": os.getenv("ACLED_EMAIL"),
            "password": os.getenv("ACLED_PASSWORD"),
            "grant_type": "password",
            "client_id": "acled"
        }
        token_resp = requests.post(token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = token_resp.json()["access_token"]
        
        url = f"https://acleddata.com/api/acled/read?_format=json&event_date=yesterday&iso={YOUR_REGIONS}&limit=10"
        data = requests.get(url, headers={"Authorization": f"Bearer {token}"}).json()
        for e in data.get('data', [])[:3]:
            articles.append(f"• {e['event_type']} in {e['country']}: {e['notes'][:80]}...")
    except: pass
    
    return "\n".join(articles[:12])

def get_newsletters_new_only():
    last = get_last_brief_time()
    new_items = []
    for url in RSS_FEEDS:
        if any(x in url.lower() for x in ["morningbrew", "tldr", "finimize", "thehustle"]):
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if entry.published_parsed:
                    pub = datetime(*entry.published_parsed[:6])
                    if pub > last:
                        new_items.append(f"• {entry.title} — {entry.link}")
    return "\n".join(new_items[:6]) or "No new newsletters."

def get_market_update():
    tickers = ["^GSPC", "^IXIC", "^DJI", "NVDA", "TSLA", "AAPL", "BTC-USD", "ETH-USD"]
    updates = []
    for t in tickers:
        try:
            data = yf.Ticker(t).history(period="2d")['Close']
            change = ((data.iloc[-1] - data.iloc[-2]) / data.iloc[-2]) * 100
            updates.append(f"{t}: {data.iloc[-1]:.2f} ({change:+.1f}%)")
        except:
            updates.append(f"{t}: N/A")
    return "\n".join(updates)

def get_commodities_vol():
    tickers = ["GC=F", "CL=F", "NG=F", "^VIX"]
    return "\n".join([f"{t}: {yf.Ticker(t).history(period='1d')['Close'].iloc[-1]:.2f}" for t in tickers])

def get_fear_greed():
    try:
        d = requests.get("https://api.alternative.me/fng/").json()["data"][0]
        return f"{d['value_classification']} ({d['value']})"
    except: return "N/A"

def get_economic_calendar():
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        r = requests.get(f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={today}&token={os.getenv('FINNHUB_KEY')}").json()
        high = [e for e in r.get("economicCalendar", []) if e.get("impact") in ("high", "medium")]
        return "\n".join([f"• {e['time']} {e['event']} ({e.get('country','')})" for e in high[:6]])
    except: return "No high-impact events."

def get_hyperliquid_snapshot():
    with liq_lock:
        if not liq_cache:
            return "Quiet — no big liqs"
        lines = []
        for l in sorted(liq_cache[-15:], key=lambda x: float(x.get("sz", 0)) * float(x.get("px", 0)), reverse=True):
            coin = l.get("coin", "OTHER")
            ntl = float(l.get("sz", 0)) * float(l.get("px", 0))
            thresh = LIQ_THRESHOLDS.get(coin, 50000)
            if ntl > thresh:
                lines.append(f"• {coin} ~${ntl:,.0f} notional")
        return "\n".join(lines[:8]) or "Quiet"

def hyper_ws_listener():
    def on_message(ws, message):
        try:
            data = json.loads(message)
            if isinstance(data, dict) and data.get("channel") == "trades":
                for trade in data.get("data", []):
                    coin = trade.get("coin")
                    sz = float(trade.get("sz", 0))
                    px = float(trade.get("px", 0))
                    ntl = sz * px
                    thresh = LIQ_THRESHOLDS.get(coin, 50000)
                    if ntl > thresh:
                        with liq_lock:
                            liq_cache.append(trade)
                            if len(liq_cache) > 100:
                                liq_cache.pop(0)
        except: pass

    def on_open(ws):
        for coin in COINS_TO_WATCH:
            sub = {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}
            ws.send(json.dumps(sub))

    while True:
        try:
            ws = websocket.WebSocketApp("wss://api.hyperliquid.xyz/ws", on_message=on_message, on_open=on_open)
            ws.run_forever(ping_interval=25)
        except:
            time.sleep(8)

threading.Thread(target=hyper_ws_listener, daemon=True).start()

def summarize(full_text):
    prompt = f"""Output ONLY this exact Markdown. One short line per bullet. Numbers & impact first. No intro, no conclusions, no fluff. Prioritize BIG-PICTURE macro trends, geopolitics, market-moving signals from @zerohedge @unusual_whales @MarioNawfal @KobeissiLetter @deltaone @watcherguru @spectatorindex.

🌅 Morning Brief — {datetime.now().strftime('%B %d, %Y')}

📰 OSINT
{full_text.get('osint', '')}

📬 Newsletters (new only)
{full_text.get('newsletters', '')}
