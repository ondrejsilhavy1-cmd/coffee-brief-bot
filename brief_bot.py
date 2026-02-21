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

OSINT_FEEDS = [
    "https://rss.app/feeds/RJmKz0o5CtyKOk5M.xml",
    "https://rss.app/feeds/OAxXVuw3QaGC90A0.xml",
    "http://feeds.feedburner.com/LongWarJournal",
    "https://thediplomat.com/feed/",
    "https://warontherocks.com/feed/",
    "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=rss&timespan=24h&query=(conflict+OR+military+OR+escalation+OR+protest+OR+strike+OR+geopolitics)",
    "https://www.globalincidentmap.com/rss.xml",
    "https://www.cfr.org/global-conflict-tracker/rss",
    "https://reliefweb.int/rss.xml",
]

NEWSLETTER_FEEDS = [
    "https://rss.beehiiv.com/feeds/qyHKIYCF6I.xml",
    "https://thedailydegen.substack.com/feed",
    "https://macronotes.substack.com/feed",
    "https://delphidigital.substack.com/feed",
    "https://cryptohayes.medium.com/feed",
    "https://chamath.substack.com/feed",
    "https://www.bensbites.co/feed",
    "https://www.deeplearning.ai/the-batch/feed/",
    "https://alphasignal.substack.com/feed",
    "https://tldr.tech/ai/feed"
]

LAST_BRIEF_FILE = "last_brief.txt"

LIQ_THRESHOLDS = {"BTC": 200000, "ETH": 200000, "SOL": 100000, "METALS": 150000}
liq_cache = []
liq_lock = threading.Lock()

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
    for url in OSINT_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:8]:
            articles.append(f"• {entry.title[:160]} [link]({entry.link})")
    return "\n".join(articles[:20]) or "• No major updates"

def get_newsletters_new_only():
    last = get_last_brief_time()
    items = []
    for url in NEWSLETTER_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if entry.published_parsed:
                pub = datetime(*entry.published_parsed[:6])
                if pub > last:
                    summary = (entry.get('summary') or entry.get('description') or entry.title)[:280]
                    items.append(f"**{entry.title}**\n{summary}...\n🔗 [link]({entry.link})")
    return "\n".join(items[:5]) or "No new newsletters today."

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
    updates = []
    for t in tickers:
        try:
            data = yf.Ticker(t).history(period="2d")['Close']
            change = ((data.iloc[-1] - data.iloc[-2]) / data.iloc[-2]) * 100
            color = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
            updates.append(f"{t}: {data.iloc[-1]:.2f} ({change:+.1f}%) {color}")
        except:
            updates.append(f"{t}: N/A")
    return "\n".join(updates)

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
    except: return "Quiet day"

def get_hyperliquid_snapshot():
    with liq_lock:
        if not liq_cache:
            return "Quiet — no big liqs"
        lines = []
        for l in sorted(liq_cache[-30:], key=lambda x: float(x.get("sz", 0)) * float(x.get("px", 0)), reverse=True):
            coin = l.get("coin", "OTHER")
            sz = float(l.get("sz", 0))
            px = float(l.get("px", 0))
            ntl = sz * px
            thresh = LIQ_THRESHOLDS.get(coin, 50000) if "METAL" not in coin.upper() else 150000
            if ntl > thresh:
                side = l.get("side", "").upper()
                direction = "🔴 LONG" if side in ["SELL", "S"] else "🟢 SHORT"
                lines.append(f"• {coin} {sz:.4f} @ {px:.2f} ({direction}) ~${ntl:,.0f} notional")
        return "\n".join(lines[:10]) or "Quiet"

def hyper_ws_listener():
    def on_message(ws, message):
        try:
