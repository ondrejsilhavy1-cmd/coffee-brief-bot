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
    "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen",
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
        for entry in feed.entries[:15]:   # more entries for better 8-hour coverage
            articles.append(f"• {entry.title[:160]} [link]({entry.link})")
    return "\n".join(articles[:50]) or "• No major updates"

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
                    items.append(f"**{entry.title}**\n{summary}...\n🔗 {entry.link}")
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

def summarize(raw_data, mode="all"):
    if mode == "geopolitics":
        prompt = f"Focus ONLY on geopolitics and conflicts. Synthesize cohesive bullets from all sources. Raw data: {raw_data}"
    elif mode == "market":
        prompt = f"Focus ONLY on market movements and macroeconomics. Synthesize cohesive bullets from all sources. Raw data: {raw_data}"
    elif mode == "tech":
        prompt = f"Focus ONLY on AI and tech news. Synthesize cohesive bullets from all sources. Raw data: {raw_data}"
    else:
        prompt = f"""Create a sharp, detailed 3-5 minute coffee brief. Synthesize cohesive summaries from all sources into three sections. No single-source attribution.

Raw data:
{raw_data}

Output EXACTLY this structure:

**Condensed Big-Picture Overview – {datetime.now().strftime('%B %d, %Y')}**

### OSINT & Twitter Scan
**Geopolitics and Conflicts**
• cohesive bullet [link]

**Market and Macroeconomics**
• cohesive bullet [link]

**AI and Tech**
• cohesive bullet [link]

### Newsletters (new only)
**Title**
short summary
🔗 full url

### Markets
One sharp sentence overview.
• S&P500: price (change) 🟢 or 🔴
• Nasdaq: ...
• Dow: ...
• NVDA: ...
• TSLA: ...
• AAPL: ...
• BTC: ...
• ETH: ...

### Commodities & Vol
• Gold: price (change) 🟢 or 🔴
• Crude Oil: ...
• Natural Gas: ...
• VIX: ...

### Hyperliquid Liquidations
• ticker size @ price (🔴 LONG or 🟢 SHORT) ~$notional

### Economic Calendar (today)
• bullets or Quiet day

### Sentiment
• Fear & Greed status

Last updated: {datetime.now().strftime('%H:%M UTC')}"""

    try:
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
            max_tokens=1200
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

    summary = summarize(raw, mode="all")
    bot.send_message(CHANNEL_ID, summary)
    save_last_brief_time()

@bot.message_handler(commands=['full', 'all', 'news', 'market', 'liqs', 'brief', 'geopolitics', 'tech'])
def handle_command(message):
    cmd = message.text.lower()
    if cmd in ["/full", "/all", "/brief"]:
        send_daily_brief()
    elif cmd == "/geopolitics":
        summary = summarize(get_osint_news(), mode="geopolitics")
        bot.send_message(CHANNEL_ID, "🌍 Extended Geopolitics & Conflicts — " + datetime.now().strftime('%H:%M') + "\n\n" + summary)
    elif cmd == "/market":
        summary = summarize(get_osint_news(), mode="market")
        bot.send_message(CHANNEL_ID, "📈 Extended Market & Macro — " + datetime.now().strftime('%H:%M') + "\n\n" + summary)
    elif cmd == "/tech":
        summary = summarize(get_osint_news(), mode="tech")
        bot.send_message(CHANNEL_ID, "🤖 Extended AI & Tech — " + datetime.now().strftime('%H:%M') + "\n\n" + summary)
    elif cmd == "/news":
        bot.send_message(CHANNEL_ID, "📰 On-demand OSINT+Newsletters — " + datetime.now().strftime('%H:%M') + "\n\n" + get_osint_news() + "\n\n" + get_newsletters_new_only())
    elif cmd == "/market":
        bot.send_message(CHANNEL_ID, "📈 On-demand Markets — " + datetime.now().strftime('%H:%M') + "\n\n" + get_market_update())
    elif cmd == "/liqs":
        bot.send_message(CHANNEL_ID, "💥 Hyperliquid Snapshot — " + datetime.now().strftime('%H:%M') + "\n\n" + get_hyperliquid_snapshot())

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_brief, 'cron', hour=5, minute=0)   # 6:00 AM CET
scheduler.add_job(send_daily_brief, 'cron', hour=19, minute=0)
scheduler.start()

print("🚀 Coffee Brief bot STARTED — type /all, /geopolitics, /market, /tech in the channel")
bot.infinity_polling()
