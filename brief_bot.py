import os, json, time, threading, feedparser, requests, yfinance as yf, logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import telebot
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
import websocket

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# ── Feeds ──────────────────────────────────────────────────────────────────────

OSINT_FEEDS = [
    "https://rss.app/feeds/RJmKz0o5CtyKOk5M.xml",
    "https://rss.app/feeds/OAxXVuw3QaGC90A0.xml",
    "http://feeds.feedburner.com/LongWarJournal",
    "https://thediplomat.com/feed/",
    "https://warontherocks.com/feed/",
    "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen",
    "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=rss&timespan=24h&query=(conflict+OR+military+OR+escalation+OR+protest+OR+strike+OR+geopolitics)",
    "https://www.cfr.org/global-conflict-tracker/rss",
    "https://reliefweb.int/rss.xml",
]

MARKET_FEEDS = [
    "https://macronotes.substack.com/feed",
    "https://thedailydegen.substack.com/feed",
    "https://news.google.com/rss/search?q=markets+macro+fed+rates+economy&hl=en-US&gl=US&ceid=US%3Aen",
]

TECH_FEEDS = [
    "https://www.bensbites.co/feed",
    "https://www.deeplearning.ai/the-batch/feed/",
    "https://alphasignal.substack.com/feed",
    "https://tldr.tech/ai/feed",
]

NEWSLETTER_FEEDS = [
    ("The Daily Degen",       "https://thedailydegen.substack.com/feed"),
    ("Macro Notes",           "https://macronotes.substack.com/feed"),
    ("Delphi Digital",        "https://delphidigital.substack.com/feed"),
    ("Arthur Hayes",          "https://cryptohayes.medium.com/feed"),
    ("Chamath",               "https://chamath.substack.com/feed"),
    ("Ben's Bites",           "https://www.bensbites.co/feed"),
    ("The Batch (DL.AI)",     "https://www.deeplearning.ai/the-batch/feed/"),
    ("Alpha Signal",          "https://alphasignal.substack.com/feed"),
    ("TLDR AI",               "https://tldr.tech/ai/feed"),
]

LAST_BRIEF_FILE = "last_brief.txt"

# ── Hyperliquid liquidations ───────────────────────────────────────────────────

LIQ_THRESHOLDS = {"BTC": 200000, "ETH": 200000, "SOL": 100000}
DEFAULT_LIQ_THRESHOLD = 50000
METALS_THRESHOLD = 150000

liq_cache = []
liq_lock = threading.Lock()

def hyper_ws_listener():
    backoff = 4
    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("channel") == "liquidations":
                for liq in data.get("data", []):
                    with liq_lock:
                        liq_cache.append(liq)
                        if len(liq_cache) > 200:
                            liq_cache.pop(0)
        except Exception as e:
            log.warning(f"Hyperliquid WS parse error: {e}")

    def on_open(ws):
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "liquidations"}}))
        log.info("Hyperliquid WS connected")

    def on_error(ws, error):
        log.warning(f"Hyperliquid WS error: {error}")

    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://api.hyperliquid.xyz/ws",
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
            )
            ws.run_forever(ping_interval=25)
            backoff = 4  # reset on clean disconnect
        except Exception as e:
            log.error(f"Hyperliquid WS crashed: {e}")
        log.info(f"Reconnecting Hyperliquid WS in {backoff}s...")
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)

threading.Thread(target=hyper_ws_listener, daemon=True).start()

# ── Helpers ────────────────────────────────────────────────────────────────────

def tg_send(chat_id, text, parse_mode="Markdown"):
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    for chunk in chunks:
        try:
            bot.send_message(chat_id, chunk, parse_mode=parse_mode)
            time.sleep(0.3)
        except Exception as e:
            log.error(f"Telegram send failed: {e}")

def safe_parse_feed(url, max_entries=12):
    try:
        feed = feedparser.parse(url)
        return feed.entries[:max_entries]
    except Exception as e:
        log.warning(f"Feed failed [{url}]: {e}")
        return []

def safe_date(entry):
    try:
        if entry.get("published_parsed"):
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return None

def get_last_brief_time():
    try:
        with open(LAST_BRIEF_FILE, "r") as f:
            return datetime.fromisoformat(f.read().strip())
    except Exception:
        return datetime.now(timezone.utc) - timedelta(days=1)

def save_last_brief_time():
    tmp = LAST_BRIEF_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    os.replace(tmp, LAST_BRIEF_FILE)

# ── News fetchers ──────────────────────────────────────────────────────────────

def _fetch_feed_list(feeds, max_per_feed=10, total=30):
    articles = []
    for url in feeds:
        for entry in safe_parse_feed(url, max_entries=max_per_feed):
            title = entry.get("title", "").strip()[:160]
            link = entry.get("link", "")
            if title:
                articles.append(f"• {title} [link]({link})")
    return "\n".join(articles[:total])

def get_osint_news():
    return _fetch_feed_list(OSINT_FEEDS, max_per_feed=8, total=35) or "• No major updates"

def get_market_news():
    return _fetch_feed_list(MARKET_FEEDS, max_per_feed=10, total=20) or "• No market news"

def get_tech_news():
    return _fetch_feed_list(TECH_FEEDS, max_per_feed=10, total=20) or "• No tech news"

def get_newsletters_raw():
    """Returns raw title + link for new newsletter posts since last brief."""
    last = get_last_brief_time()
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    lines = []
    for name, url in NEWSLETTER_FEEDS:
        for entry in safe_parse_feed(url, max_entries=5):
            pub = safe_date(entry)
            if pub and pub > last:
                title = entry.get("title", "Untitled").strip()
                link = entry.get("link", "")
                lines.append(f"• *{name}* — [{title}]({link})")
    return "\n".join(lines) if lines else "• No new newsletters since last brief."

# ── Market data ────────────────────────────────────────────────────────────────

def fetch_ticker(symbol):
    try:
        data = yf.Ticker(symbol).history(period="2d")["Close"]
        if len(data) < 2:
            return symbol, None, None
        change = ((data.iloc[-1] - data.iloc[-2]) / data.iloc[-2]) * 100
        return symbol, data.iloc[-1], change
    except Exception as e:
        log.warning(f"yfinance failed [{symbol}]: {e}")
        return symbol, None, None

def get_market_update():
    tickers = {
        "^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow",
        "NVDA": "NVDA", "TSLA": "TSLA", "AAPL": "AAPL",
        "BTC-USD": "BTC", "ETH-USD": "ETH",
    }
    lines = []
    for symbol, label in tickers.items():
        sym, price, change = fetch_ticker(symbol)
        if price is not None:
            emoji = "🟢" if change > 0 else "🔴"
            lines.append(f"• {label}: {price:.2f} ({change:+.1f}%) {emoji}")
        else:
            lines.append(f"• {label}: N/A")
    return "\n".join(lines)

def get_commodities_vol():
    tickers = {"GC=F": "Gold", "CL=F": "Crude Oil", "NG=F": "Nat Gas", "^VIX": "VIX"}
    lines = []
    for symbol, label in tickers.items():
        sym, price, change = fetch_ticker(symbol)
        if price is not None:
            emoji = "🟢" if change > 0 else "🔴"
            lines.append(f"• {label}: {price:.2f} ({change:+.1f}%) {emoji}")
        else:
            lines.append(f"• {label}: N/A")
    return "\n".join(lines)

def get_fear_greed():
    try:
        d = requests.get("https://api.alternative.me/fng/", timeout=8).json()["data"][0]
        return f"{d['value_classification']} ({d['value']})"
    except Exception as e:
        log.warning(f"Fear & Greed failed: {e}")
        return "N/A"

def get_economic_calendar():
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={today}&token={os.getenv('FINNHUB_KEY')}",
            timeout=8
        ).json()
        high = [e for e in r.get("economicCalendar", []) if e.get("impact") in ("high", "medium")]
        if not high:
            return "• Quiet day"
        return "\n".join([f"• {e['time']} {e['event']} ({e.get('country','')})" for e in high[:6]])
    except Exception as e:
        log.warning(f"Economic calendar failed: {e}")
        return "• Quiet day"

def get_hyperliquid_snapshot():
    with liq_lock:
        if not liq_cache:
            return "• Quiet — no significant liquidations"
        lines = []
        for l in sorted(liq_cache[-50:], key=lambda x: float(x.get("sz", 0)) * float(x.get("px", 0)), reverse=True):
            coin = l.get("coin", "OTHER")
            sz = float(l.get("sz", 0))
            px = float(l.get("px", 0))
            ntl = sz * px
            thresh = LIQ_THRESHOLDS.get(coin, METALS_THRESHOLD if "METAL" in coin.upper() else DEFAULT_LIQ_THRESHOLD)
            if ntl > thresh:
                side = l.get("side", "").upper()
                direction = "🔴 LONG liq" if side in ["SELL", "S"] else "🟢 SHORT liq"
                lines.append(f"• {coin} {sz:.4f} @ {px:.2f} ({direction}) ~${ntl:,.0f}")
        return "\n".join(lines[:10]) if lines else "• Quiet — no significant liquidations"

# ── Groq summarizer ────────────────────────────────────────────────────────────

def summarize(raw_data, mode="all"):
    date_str = datetime.now().strftime("%B %d, %Y")
    time_str = datetime.now().strftime("%H:%M UTC")

    if mode == "geo":
        prompt = f"""You are an intelligence analyst. Summarize the following geopolitical and conflict news into concise, sharp bullets.
Rules:
- One distinct topic per bullet
- Keep each bullet to 1-2 sentences max
- Preserve source links in [link](url) format
- No market data, no tech news, no newsletters
- Group by region if possible (Europe, Middle East, Asia, etc.)

Raw headlines:
{raw_data[:6000]}"""
        max_tokens = 1000

    elif mode == "market":
        prompt = f"""You are a macro analyst. Summarize the following market and macro news into concise bullets.
Rules:
- One distinct topic per bullet
- Keep each bullet to 1-2 sentences max
- Preserve source links in [link](url) format
- Focus strictly on: rates, central banks, equities, crypto, commodities, economic data
- No geopolitics unless directly market-moving, no tech product news, no newsletters

Raw headlines:
{raw_data[:6000]}"""
        max_tokens = 1000

    elif mode == "tech":
        prompt = f"""You are an AI and tech analyst. Summarize the following AI and tech news into concise bullets.
Rules:
- One distinct topic per bullet
- Keep each bullet to 1-2 sentences max
- Preserve source links in [link](url) format
- Focus strictly on: AI models, research, startups, big tech, developer tools
- No markets, no geopolitics, no newsletters

Raw headlines:
{raw_data[:6000]}"""
        max_tokens = 1000

    else:  # /all morning brief
        prompt = f"""You are a sharp intelligence and market analyst. Create a structured morning brief from the raw data below.
Rules:
- Short, separate bullets — one topic per bullet, 1-2 sentences max
- Preserve source links in [link](url) format where available
- Do NOT invent or paraphrase links
- Newsletters section is handled separately — skip it in your output

Output EXACTLY this structure (no extra sections):

*Morning Brief — {date_str} {time_str}*

🌍 *Geopolitics & Conflicts*
• bullet [link](url)

📈 *Markets & Macro*
• bullet [link](url)

🤖 *AI & Tech*
• bullet [link](url)

📊 *Key Indicators*
[paste the indicators block as-is from raw data]

🛢 *Commodities & Vol*
[paste the commodities block as-is from raw data]

💥 *Hyperliquid Liquidations*
[paste the liquidations block as-is from raw data]

📅 *Economic Calendar*
[paste the calendar block as-is from raw data]

😨 *Sentiment*
[paste fear & greed as-is from raw data]

Raw data:
{raw_data[:9000]}"""
        max_tokens = 3000

    for attempt in range(3):
        try:
            chat = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=max_tokens,
            )
            return chat.choices[0].message.content.strip()
        except Exception as e:
            log.warning(f"Groq attempt {attempt+1} failed: {e}")
            time.sleep(3 * (attempt + 1))

    log.error("Groq failed after 3 attempts, returning raw data")
    return raw_data[:3500]

# ── Brief builder ──────────────────────────────────────────────────────────────

def build_and_send_brief(chat_id):
    log.info(f"Building morning brief for {chat_id}")

    # Collect all data
    osint      = get_osint_news()
    mkt_news   = get_market_news()
    tech_news  = get_tech_news()
    indicators = get_market_update()
    commodities = get_commodities_vol()
    hyper      = get_hyperliquid_snapshot()
    econ       = get_economic_calendar()
    fg         = get_fear_greed()
    newsletters = get_newsletters_raw()

    raw = f"""GEOPOLITICS HEADLINES:
{osint}

MARKET/MACRO HEADLINES:
{mkt_news}

AI/TECH HEADLINES:
{tech_news}

KEY INDICATORS:
{indicators}

COMMODITIES & VOL:
{commodities}

HYPERLIQUID LIQUIDATIONS:
{hyper}

ECONOMIC CALENDAR:
{econ}

SENTIMENT (Fear & Greed):
{fg}"""

    # Groq handles the news sections; indicators/liqs/cal/sentiment are pasted as-is
    summary = summarize(raw, mode="all")

    # Append newsletters separately (raw, no Groq)
    nl_block = f"\n\n📰 *New Newsletters*\n{newsletters}"
    full_message = summary + nl_block

    tg_send(chat_id, full_message)
    save_last_brief_time()

def send_scheduled_brief():
    build_and_send_brief(CHANNEL_ID)

# ── Commands ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["help"])
def cmd_help(message):
    text = (
        "📋 *Commands*\n\n"
        "/all — full morning brief\n"
        "/geo — geopolitics & conflicts\n"
        "/market — markets, macro, tickers & sentiment\n"
        "/tech — AI & tech news\n"
        "/liqs — Hyperliquid liquidation snapshot\n"
    )
    tg_send(message.chat.id, text)

@bot.message_handler(commands=["all", "brief", "full"])
def cmd_all(message):
    tg_send(message.chat.id, "⏳ Building your morning brief...")
    build_and_send_brief(message.chat.id)

@bot.message_handler(commands=["geo", "geopolitics"])
def cmd_geo(message):
    tg_send(message.chat.id, "⏳ Fetching geopolitics...")
    data = get_osint_news()
    summary = summarize(data, mode="geo")
    header = f"🌍 *Geopolitics & Conflicts — {datetime.now().strftime('%H:%M UTC')}*\n\n"
    tg_send(message.chat.id, header + summary)

@bot.message_handler(commands=["market"])
def cmd_market(message):
    tg_send(message.chat.id, "⏳ Fetching markets...")
    news = get_market_news()
    summary = summarize(news, mode="market")
    indicators = get_market_update()
    commodities = get_commodities_vol()
    fg = get_fear_greed()

    full = (
        f"📈 *Markets & Macro — {datetime.now().strftime('%H:%M UTC')}*\n\n"
        f"{summary}\n\n"
        f"📊 *Key Indicators*\n{indicators}\n\n"
        f"🛢 *Commodities & Vol*\n{commodities}\n\n"
        f"😨 *Sentiment:* {fg}"
    )
    tg_send(message.chat.id, full)

@bot.message_handler(commands=["tech"])
def cmd_tech(message):
    tg_send(message.chat.id, "⏳ Fetching AI & tech...")
    data = get_tech_news()
    summary = summarize(data, mode="tech")
    header = f"🤖 *AI & Tech — {datetime.now().strftime('%H:%M UTC')}*\n\n"
    tg_send(message.chat.id, header + summary)

@bot.message_handler(commands=["liqs"])
def cmd_liqs(message):
    snapshot = get_hyperliquid_snapshot()
    tg_send(message.chat.id, f"💥 *Hyperliquid Snapshot — {datetime.now().strftime('%H:%M UTC')}*\n\n{snapshot}")

# ── Scheduler ──────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="Europe/Rome")
scheduler.add_job(send_scheduled_brief, "cron", hour=6, minute=0)
scheduler.add_job(send_scheduled_brief, "cron", hour=19, minute=0)
scheduler.start()

log.info("🚀 Coffee Brief bot STARTED")
bot.infinity_polling()
