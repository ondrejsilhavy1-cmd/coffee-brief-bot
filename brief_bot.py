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
YOUR_REGIONS = "IR,IL,SY,RU,UA"

LIQ_THRESHOLDS = {"BTC": 200000, "ETH": 200000, "SOL": 100000}
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
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:4]:
            articles.append(f"• {entry.title[:140]} — {entry.link}")
    try:
        token_url = "https://acleddata.com/oauth/token"
        payload = {"username": os.getenv("ACLED_EMAIL"), "password": os.getenv("ACLED_PASSWORD"), "grant_type": "password", "client_id": "acled"}
        token = requests.post(token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}).json()["access_token"]
        url = f"https://acleddata.com/api/acled/read?_format=json&event_date=yesterday&iso={YOUR_REGIONS}&limit=10"
        data = requests.get(url, headers={"Authorization": f"Bearer {token}"}).json()
        for e in data.get('data', [])[:3]:
            articles.append(f"• {e['event_type']} in {e['country']}: {e['notes'][:80]}...")
    except: pass
    return "\n".join(articles[:12]) or "• Quiet day in OSINT"

def get_newsletters_new_only():
    last = get_last_brief_time()
    items = []
    for url in RSS_FEEDS:
        if any(x in url.lower() for x in ["beehiiv", "substack", "medium", "bensbites", "deeplearning", "alphasignal", "tldr"]):
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if entry.published_parsed:
                    pub = datetime(*entry.published_parsed[:6])
                    if pub > last:
                        summary = (entry.get('summary') or entry.get('description') or "")[:400]
                        items.append(f"**{entry.title}**\n{summary}\n🔗 {entry.link}\n")
    return "\n".join(items[:6]) or "No new newsletters today."

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
    except: return "Quiet day"

def get_hyperliquid_snapshot():
    with liq_lock:
        if not liq_cache:
            return "Quiet — no big liqs"
        lines = []
        for l in sorted(liq_cache[-20:], key=lambda x: float(x.get("sz", 0)) * float(x.get("px", 0)), reverse=True):
            coin = l.get("coin", "OTHER")
            sz = float(l.get("sz", 0))
            px = float(l.get("px", 0))
            ntl = sz * px
            thresh = LIQ_THRESHOLDS.get(coin, 50000)
            if ntl > thresh:
                side = l.get("side", "").upper()
                direction = "🔴 LONG" if side in ["SELL", "S"] else "🟢 SHORT"
                lines.append(f"• {coin} {sz:.4f} @ {px:.2f} ({direction}) ~${ntl:,.0f} notional")
        return "\n".join(lines[:8]) or "Quiet"

def hyper_ws_listener():
    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("channel") == "liquidations":
                for liq in data.get("data", []):
                    with liq_lock:
                        liq_cache.append(liq)
                        if len(liq_cache) > 100:
                            liq_cache.pop(0)
        except: pass

    def on_open(ws):
        sub = {"method": "subscribe", "subscription": {"type": "liquidations"}}
        ws.send(json.dumps(sub))

    while True:
        try:
            ws = websocket.WebSocketApp("wss://api.hyperliquid.xyz/ws", on_message=on_message, on_open=on_open)
            ws.run_forever(ping_interval=25)
        except:
            time.sleep(8)

threading.Thread(target=hyper_ws_listener, daemon=True).start()

def summarize(raw_data):
    prompt = f"""Create a clean, professional 2-5 minute coffee brief. Always use short bullets. Always fill every section.

Raw data:
{raw_data}

Output exactly:

🌅 Morning Brief — {datetime.now().strftime('%B %d, %Y')}

📰 OSINT
• bullet
• bullet

📬 Newsletters (new only)
**Title**
2-3 sentence paragraph
🔗 link

📈 Markets
One short sentence overview, then bullet list of prices with 24h % (include S&P500, Nasdaq, Dow, key stocks, BTC, ETH)

⛽ Commodities & Vol
• bullets

💥 Hyperliquid Snapshot
• bullets

🗓 Economic Calendar (today)
• bullets

📊 Sentiment
• bullet"""

    try:
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=950
        )
        return chat.choices[0].message.content.strip()
    except:
        return raw_data

def send_daily_brief():
    osint = get_osint_news()
    nl = get_newsletters_new_only()
    market = get_market_update()
    comm = get_commodities_vol()
    hyper = get_hyperliquid_snapshot()
    econ = get_economic_calendar()
    fg = get_fear_greed()

    raw = f"""OSINT:\n{osint}\n\nNewsletters:\n{nl}\n\nMarkets:\n{market}\n\nCommodities:\n{comm}\n\nHyperliquid:\n{hyper}\n\nEconomic:\n{econ}\n\nSentiment:\n{fg}"""

    summary = summarize(raw)
    bot.send_message(CHANNEL_ID, summary)
    save_last_brief_time()

@bot.message_handler(commands=['full', 'news', 'market', 'liqs', 'brief'])
def handle_command(message):
    if message.chat.type != "private": return
    cmd = message.text.lower()
    if cmd in ["/full", "/brief"]:
        send_daily_brief()
    elif cmd == "/news":
        bot.send_message(CHANNEL_ID, "📰 On-demand OSINT+Newsletters — " + datetime.now().strftime('%H:%M') + "\n\n" + get_osint_news() + "\n\n" + get_newsletters_new_only())
    elif cmd == "/market":
        bot.send_message(CHANNEL_ID, "📈 On-demand Markets — " + datetime.now().strftime('%H:%M') + "\n\n" + get_market_update())
    elif cmd == "/liqs":
        bot.send_message(CHANNEL_ID, "💥 Hyperliquid Snapshot — " + datetime.now().strftime('%H:%M') + "\n\n" + get_hyperliquid_snapshot())

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_brief, 'cron', hour=7, minute=0)
scheduler.add_job(send_daily_brief, 'cron', hour=19, minute=0)
scheduler.start()

print("🚀 Coffee Brief bot STARTED — DM me /full to test")
bot.infinity_polling()
