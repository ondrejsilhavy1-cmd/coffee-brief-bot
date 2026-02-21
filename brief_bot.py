import os, json, time, threading, feedparser, requests, yfinance as yf, logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from dotenv import load_dotenv
import telebot
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
import websocket

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = telebot.TeleBot(os.getenv("TELEGRAM_TOKEN"))
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Your self-hosted RSSHub base URL on Railway
RSSHUB_URL = os.getenv("RSSHUB_URL", "https://your-rsshub-url.railway.app")

# ACLED credentials (register free at acleddata.com)
ACLED_EMAIL    = os.getenv("ACLED_EMAIL", "")
ACLED_PASSWORD = os.getenv("ACLED_PASSWORD", "")
_acled_session = None   # requests.Session, populated lazily

# Feeds

# Geopolitics / OSINT - pure news RSS, no newsletters
OSINT_FEEDS = [
    RSSHUB_URL + "/twitter/user/zerohedge?exclude_rts=1",
    RSSHUB_URL + "/twitter/user/DeItaone?exclude_rts=1",
    RSSHUB_URL + "/twitter/user/spectatorindex?exclude_rts=1",
    RSSHUB_URL + "/twitter/user/SITREP_artorias?exclude_rts=1",
    RSSHUB_URL + "/twitter/user/sentdefender?exclude_rts=1",
    "http://feeds.feedburner.com/LongWarJournal",
    "https://thediplomat.com/feed/",
    "https://warontherocks.com/feed/",
    "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen",
    "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=rss&timespan=24h&query=(conflict+OR+military+OR+escalation+OR+protest+OR+strike+OR+geopolitics)",
    "https://www.cfr.org/global-conflict-tracker/rss",
    "https://reliefweb.int/rss.xml",
]

# Markets / macro - pure news RSS, no newsletters
MARKET_FEEDS = [
    RSSHUB_URL + "/twitter/user/KobeissiLetter?exclude_rts=1",
    RSSHUB_URL + "/twitter/user/unusual_whales?exclude_rts=1",
    RSSHUB_URL + "/twitter/user/TheBlock__?exclude_rts=1",
    "https://news.google.com/rss/search?q=markets+macro+fed+rates+economy&hl=en-US&gl=US&ceid=US%3Aen",
    "https://news.google.com/rss/search?q=stock+market+S%26P+nasdaq+earnings&hl=en-US&gl=US&ceid=US%3Aen",
    "https://feeds.bloomberg.com/markets/news.rss",
]

# AI / tech - pure news RSS, no newsletters
TECH_FEEDS = [
    "https://news.google.com/rss/search?q=artificial+intelligence+AI+tech+startups&hl=en-US&gl=US&ceid=US%3Aen",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://venturebeat.com/feed/",
]

# Newsletters - only used for newsletter section, never as a news source
NEWSLETTER_FEEDS = [
    ("The Daily Degen",   "https://thedailydegen.substack.com/feed"),
    ("Macro Notes",       "https://macronotes.substack.com/feed"),
    ("Delphi Digital",    "https://delphidigital.substack.com/feed"),
    ("Arthur Hayes",      "https://cryptohayes.medium.com/feed"),
    ("Chamath",           "https://chamath.substack.com/feed"),
    ("Ben's Bites",       "https://www.bensbites.co/feed"),
    ("The Batch (DL.AI)", "https://www.deeplearning.ai/the-batch/feed/"),
    ("Alpha Signal",      "https://alphasignal.substack.com/feed"),
    ("TLDR AI",           "https://tldr.tech/ai/feed"),
]

LAST_BRIEF_FILE = "last_brief.txt"

# Push-notification accounts -- polled every 10 min, alert sent immediately
PUSH_ACCOUNTS = [
    ("SITREP_artorias", "geo"),
]
# Tracks the most-recent tweet ID seen per account so we don't re-alert
_push_seen = {}   # { "SITREP_artorias": set_of_ids, ... }

# Hyperliquid liquidations
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
            log.warning("Hyperliquid WS parse error: %s", e)

    def on_open(ws):
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "liquidations"}}))
        log.info("Hyperliquid WS connected")

    def on_error(ws, error):
        log.warning("Hyperliquid WS error: %s", error)

    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://api.hyperliquid.xyz/ws",
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
            )
            ws.run_forever(ping_interval=25)
            backoff = 4
        except Exception as e:
            log.error("Hyperliquid WS crashed: %s", e)
        log.info("Reconnecting Hyperliquid WS in %ss...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)


threading.Thread(target=hyper_ws_listener, daemon=True).start()


# Helpers

def tg_send(chat_id, text, parse_mode="Markdown"):
    MAX = 4000
    chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)]
    for chunk in chunks:
        try:
            bot.send_message(chat_id, chunk, parse_mode=parse_mode)
            time.sleep(0.3)
        except Exception as e:
            log.error("Telegram send failed: %s", e)


def safe_parse_feed(url, max_entries=12):
    try:
        feed = feedparser.parse(url)
        return feed.entries[:max_entries]
    except Exception as e:
        log.warning("Feed failed [%s]: %s", url, e)
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
            dt = datetime.fromisoformat(f.read().strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except Exception:
        return datetime.now(timezone.utc) - timedelta(days=1)


def save_last_brief_time():
    tmp = LAST_BRIEF_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    os.replace(tmp, LAST_BRIEF_FILE)


# ACLED integration

def _acled_login():
    """Authenticate with ACLED and return a logged-in session, or None on failure."""
    global _acled_session
    if not ACLED_EMAIL or not ACLED_PASSWORD:
        return None
    try:
        s = requests.Session()
        resp = s.post(
            "https://acleddata.com/user/login?_format=json",
            json={"name": ACLED_EMAIL, "pass": ACLED_PASSWORD},
            timeout=10
        )
        if resp.status_code == 200:
            _acled_session = s
            log.info("ACLED login OK")
            return s
        else:
            log.warning("ACLED login failed: %s", resp.status_code)
            return None
    except Exception as e:
        log.warning("ACLED login error: %s", e)
        return None


def get_acled_news():
    """Fetch last 24h high-severity conflict events from ACLED API."""
    global _acled_session
    if not ACLED_EMAIL or not ACLED_PASSWORD:
        return []
    if _acled_session is None:
        _acled_login()
    if _acled_session is None:
        return []
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        r = _acled_session.get(
            "https://acleddata.com/api/acled/read",
            params={
                "event_date": yesterday + "|" + today,
                "event_date_where": "BETWEEN",
                "event_type": "Battles|Explosions/Remote violence|Violence against civilians",
                "limit": 20,
                "fields": "event_date|event_type|sub_event_type|actor1|actor2|country|location|notes",
            },
            timeout=15
        )
        if r.status_code == 401:
            log.info("ACLED session expired, re-logging in")
            _acled_login()
            return []
        data = r.json().get("data", [])
        entries = []
        for ev in data:
            country  = ev.get("country", "")
            location = ev.get("location", "")
            actor1   = ev.get("actor1", "")
            etype    = ev.get("sub_event_type", ev.get("event_type", ""))
            notes    = ev.get("notes", "")[:120]
            title    = "[ACLED] " + etype + " -- " + location + ", " + country
            if actor1:
                title += " (" + actor1 + ")"
            entries.append({"title": title, "link": "https://acleddata.com/data-export-tool/", "notes": notes})
        return entries
    except Exception as e:
        log.warning("ACLED fetch failed: %s", e)
        return []


# SITREP / push-notification poller

def _check_push_accounts():
    """Poll each push account via RSSHub every 10 min. Send alert for new posts."""
    for handle, category in PUSH_ACCOUNTS:
        url = RSSHUB_URL + "/twitter/user/" + handle + "?exclude_rts=1"
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                continue
            seen = _push_seen.setdefault(handle, set())
            new_entries = []
            for entry in feed.entries[:5]:
                entry_id = entry.get("id") or entry.get("link", "")
                if entry_id and entry_id not in seen:
                    new_entries.append(entry)
                    seen.add(entry_id)
            # On first run just seed seen IDs, don't spam
            if len(seen) <= len(new_entries):
                log.info("Push poller: seeded %d IDs for @%s", len(seen), handle)
                continue
            for entry in new_entries:
                title = entry.get("title", "").strip()[:300]
                link  = entry.get("link", "")
                emoji = "\U0001f6a8" if category == "geo" else "\U0001f4ca"
                msg   = emoji + " *@" + handle + "*\n" + title
                if link:
                    msg += "\n[link](" + link + ")"
                tg_send(CHANNEL_ID, msg)
                log.info("Push alert sent for @%s", handle)
        except Exception as e:
            log.warning("Push poll failed for @%s: %s", handle, e)


# Deduplication
# Removes articles whose titles are too similar to ones already seen.
# Uses a simple ratio threshold so near-duplicate stories from different
# sources are collapsed into one.

def deduplicate(articles, threshold=0.82):
    seen_titles = []
    unique = []
    for item in articles:
        title = item.get("title", "")
        is_dupe = any(
            SequenceMatcher(None, title.lower(), seen.lower()).ratio() > threshold
            for seen in seen_titles
        )
        if not is_dupe:
            seen_titles.append(title)
            unique.append(item)
    return unique


# News fetchers (RSS only - newsletters never included here)

def _fetch_entries(feeds, max_per_feed=10, total=35):
    raw = []
    for url in feeds:
        for entry in safe_parse_feed(url, max_entries=max_per_feed):
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            if title and link:
                raw.append({"title": title[:160], "link": link})
    deduped = deduplicate(raw)
    return deduped[:total]


def _format_entries(entries):
    # Produces clean Markdown: "- title [link](url)"
    # The URL is embedded behind the word "link" - no raw URLs exposed
    lines = []
    for e in entries:
        lines.append("- " + e["title"] + " [link](" + e["link"] + ")")
    return "\n".join(lines)


def get_osint_news():
    entries = _fetch_entries(OSINT_FEEDS, max_per_feed=8, total=35)
    # Append ACLED conflict events
    acled = get_acled_news()
    entries = entries + acled
    return _format_entries(entries[:40]) or "- No major updates"


def get_market_news():
    entries = _fetch_entries(MARKET_FEEDS, max_per_feed=10, total=20)
    return _format_entries(entries) or "- No market news"


def get_tech_news():
    entries = _fetch_entries(TECH_FEEDS, max_per_feed=10, total=20)
    return _format_entries(entries) or "- No tech news"


def get_newsletters_raw():
    last = get_last_brief_time()
    lines = []
    for name, url in NEWSLETTER_FEEDS:
        for entry in safe_parse_feed(url, max_entries=5):
            pub = safe_date(entry)
            if pub and pub > last:
                title = entry.get("title", "Untitled").strip()
                link = entry.get("link", "")
                lines.append("- *" + name + "* - [" + title + "](" + link + ")")
    return "\n".join(lines) if lines else "- No new newsletters since last brief."


# Market data (fetched directly, never passed through Groq)

def fetch_ticker(symbol):
    try:
        data = yf.Ticker(symbol).history(period="2d")["Close"]
        if len(data) < 2:
            return symbol, None, None
        change = ((data.iloc[-1] - data.iloc[-2]) / data.iloc[-2]) * 100
        return symbol, data.iloc[-1], change
    except Exception as e:
        log.warning("yfinance failed [%s]: %s", symbol, e)
        return symbol, None, None


def get_market_update():
    tickers = {
        "^GSPC":   "S&P 500",
        "^IXIC":   "Nasdaq",
        "^DJI":    "Dow",
        "NVDA":    "NVDA",
        "TSLA":    "TSLA",
        "AAPL":    "AAPL",
        "BTC-USD": "BTC",
        "ETH-USD": "ETH",
    }
    lines = []
    for symbol, label in tickers.items():
        _, price, change = fetch_ticker(symbol)
        if price is not None:
            emoji = "\U0001f7e2" if change > 0 else "\U0001f534"
            sign = "+" if change > 0 else ""
            lines.append("- " + label + ": " + "{:.2f}".format(price) + " (" + sign + "{:.1f}".format(change) + "%) " + emoji)
        else:
            lines.append("- " + label + ": N/A")
    return "\n".join(lines)


def get_commodities_vol():
    tickers = {
        "GC=F": "Gold",
        "CL=F": "Crude Oil",
        "NG=F": "Nat Gas",
        "^VIX": "VIX",
    }
    lines = []
    for symbol, label in tickers.items():
        _, price, change = fetch_ticker(symbol)
        if price is not None:
            emoji = "\U0001f7e2" if change > 0 else "\U0001f534"
            sign = "+" if change > 0 else ""
            lines.append("- " + label + ": " + "{:.2f}".format(price) + " (" + sign + "{:.1f}".format(change) + "%) " + emoji)
        else:
            lines.append("- " + label + ": N/A")
    return "\n".join(lines)


def get_fear_greed():
    try:
        d = requests.get("https://api.alternative.me/fng/", timeout=8).json()["data"][0]
        return d["value_classification"] + " (" + d["value"] + ")"
    except Exception as e:
        log.warning("Fear & Greed failed: %s", e)
        return "N/A"


def get_economic_calendar():
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        url = (
            "https://finnhub.io/api/v1/calendar/economic?from="
            + today + "&to=" + today
            + "&token=" + os.getenv("FINNHUB_KEY", "")
        )
        r = requests.get(url, timeout=8).json()
        high = [e for e in r.get("economicCalendar", []) if e.get("impact") in ("high", "medium")]
        if not high:
            return "- Quiet day"
        return "\n".join(
            ["- " + e["time"] + " " + e["event"] + " (" + e.get("country", "") + ")" for e in high[:6]]
        )
    except Exception as e:
        log.warning("Economic calendar failed: %s", e)
        return "- Quiet day"


def get_hyperliquid_snapshot():
    with liq_lock:
        if not liq_cache:
            return "- Quiet -- no significant liquidations"
        lines = []
        for l in sorted(
            liq_cache[-50:],
            key=lambda x: float(x.get("sz", 0)) * float(x.get("px", 0)),
            reverse=True
        ):
            coin = l.get("coin", "OTHER")
            sz = float(l.get("sz", 0))
            px = float(l.get("px", 0))
            ntl = sz * px
            thresh = LIQ_THRESHOLDS.get(
                coin,
                METALS_THRESHOLD if "METAL" in coin.upper() else DEFAULT_LIQ_THRESHOLD
            )
            if ntl > thresh:
                side = l.get("side", "").upper()
                direction = "\U0001f534 LONG liq" if side in ["SELL", "S"] else "\U0001f7e2 SHORT liq"
                lines.append(
                    "- " + coin + " " + "{:.4f}".format(sz)
                    + " @ " + "{:.2f}".format(px)
                    + " (" + direction + ") ~$" + "{:,.0f}".format(ntl)
                )
        return "\n".join(lines[:10]) if lines else "- Quiet -- no significant liquidations"


# Groq summarizer
# Groq only ever receives deduplicated RSS headline text.
# All structured data is assembled in Python and appended AFTER this returns.

def summarize(raw_data, mode="all"):
    if mode == "geo":
        prompt = (
            "You are an intelligence analyst. Summarize the following geopolitical and conflict "
            "news headlines into concise, sharp bullets.\n\n"
            "Rules:\n"
            "- One distinct topic per bullet\n"
            "- 1-2 sentences max per bullet\n"
            "- Format links as [link](url) -- never show raw URLs\n"
            "- No market data, no tech news, no newsletter content\n"
            "- Group by region where possible (Europe, Middle East, Asia, Americas)\n\n"
            "Raw headlines:\n" + raw_data[:6000]
        )
        max_tokens = 1000

    elif mode == "market":
        prompt = (
            "You are a macro analyst. Summarize the following market and macro news headlines "
            "into concise bullets.\n\n"
            "Rules:\n"
            "- One distinct topic per bullet\n"
            "- 1-2 sentences max per bullet\n"
            "- Format links as [link](url) -- never show raw URLs\n"
            "- Focus strictly on: rates, central banks, equities, crypto, commodities, economic data\n"
            "- No geopolitics unless directly market-moving, no tech product news, no newsletter content\n\n"
            "Raw headlines:\n" + raw_data[:6000]
        )
        max_tokens = 1000

    elif mode == "tech":
        prompt = (
            "You are an AI and tech analyst. Summarize the following AI and tech news headlines "
            "into concise bullets.\n\n"
            "Rules:\n"
            "- One distinct topic per bullet\n"
            "- 1-2 sentences max per bullet\n"
            "- Format links as [link](url) -- never show raw URLs\n"
            "- Focus strictly on: AI models, research, startups, big tech, developer tools\n"
            "- No market data, no geopolitics, no newsletter content\n\n"
            "Raw headlines:\n" + raw_data[:6000]
        )
        max_tokens = 1000

    else:
        prompt = (
            "You are a sharp intelligence and market analyst. Summarize the headlines below "
            "into three clearly separated sections.\n\n"
            "Rules:\n"
            "- Only use the RSS headlines provided -- do NOT reference newsletter content\n"
            "- One distinct topic per bullet, 1-2 sentences max\n"
            "- Format ALL links as [link](url) -- never show raw URLs in your output\n"
            "- Strictly separate the three sections -- do not mix topics across them\n"
            "- Group geopolitics by region where possible (Europe, Middle East, Asia, Americas)\n"
            "- Do NOT add sections for newsletters, indicators, commodities, liquidations, or sentiment\n\n"
            "Output EXACTLY this structure and nothing else:\n\n"
            "Geopolitics & Conflicts\n"
            "- bullet [link](url)\n\n"
            "Markets & Macro\n"
            "- bullet [link](url)\n\n"
            "AI & Tech\n"
            "- bullet [link](url)\n\n"
            "Raw headlines:\n" + raw_data[:9000]
        )
        max_tokens = 2000

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
            log.warning("Groq attempt %d failed: %s", attempt + 1, e)
            time.sleep(3 * (attempt + 1))

    log.error("Groq failed after 3 attempts, returning raw headlines")
    return raw_data[:3500]


# Brief builder

def build_and_send_brief(chat_id):
    log.info("Building morning brief for %s", chat_id)

    # Step 1: fetch and deduplicate RSS headlines for Groq
    osint     = get_osint_news()
    mkt_news  = get_market_news()
    tech_news = get_tech_news()

    news_raw = (
        "GEOPOLITICS HEADLINES:\n" + osint
        + "\n\nMARKET/MACRO HEADLINES:\n" + mkt_news
        + "\n\nAI/TECH HEADLINES:\n" + tech_news
    )

    # Step 2: structured data fetched in Python, never touches Groq
    indicators  = get_market_update()
    commodities = get_commodities_vol()
    hyper       = get_hyperliquid_snapshot()
    econ        = get_economic_calendar()
    fg          = get_fear_greed()
    newsletters = get_newsletters_raw()

    # Step 3: Groq summarizes only the three news sections
    news_summary = summarize(news_raw, mode="all")

    # Step 4: assemble -- structured blocks appended directly by bot
    date_str = datetime.now().strftime("%B %d, %Y %H:%M UTC")
    full_message = (
        "*Morning Brief -- " + date_str + "*\n\n"
        + news_summary + "\n\n"
        + "\U0001f4ca *Key Indicators*\n" + indicators + "\n\n"
        + "\U0001f6e2 *Commodities & Vol*\n" + commodities + "\n\n"
        + "\U0001f4a5 *Hyperliquid Liquidations*\n" + hyper + "\n\n"
        + "\U0001f4c5 *Economic Calendar*\n" + econ + "\n\n"
        + "\U0001f628 *Sentiment:* " + fg + "\n\n"
        + "\U0001f4f0 *New Newsletters*\n" + newsletters
    )

    tg_send(chat_id, full_message)
    save_last_brief_time()


def send_scheduled_brief():
    build_and_send_brief(CHANNEL_ID)


# Commands

@bot.message_handler(commands=["help"])
def cmd_help(message):
    text = (
        "\U0001f4cb *Commands*\n\n"
        "/all -- full morning brief\n"
        "/geo -- geopolitics & conflicts\n"
        "/market -- markets, macro, tickers & sentiment\n"
        "/tech -- AI & tech news\n"
        "/liqs -- Hyperliquid liquidation snapshot\n"
        "/help -- show this menu\n"
    )
    tg_send(message.chat.id, text)


@bot.message_handler(commands=["all", "brief", "full"])
def cmd_all(message):
    log.info("Received /all from chat_id=%s", message.chat.id)
    tg_send(message.chat.id, "Building your morning brief...")
    build_and_send_brief(message.chat.id)


@bot.message_handler(commands=["geo", "geopolitics"])
def cmd_geo(message):
    log.info("Received /geo from chat_id=%s", message.chat.id)
    tg_send(message.chat.id, "Fetching geopolitics...")
    data = get_osint_news()
    summary = summarize(data, mode="geo")
    tg_send(
        message.chat.id,
        "\U0001f30d *Geopolitics & Conflicts -- " + datetime.now().strftime("%H:%M UTC") + "*\n\n" + summary
    )


@bot.message_handler(commands=["market"])
def cmd_market(message):
    log.info("Received /market from chat_id=%s", message.chat.id)
    tg_send(message.chat.id, "Fetching markets...")
    news        = get_market_news()
    summary     = summarize(news, mode="market")
    indicators  = get_market_update()
    commodities = get_commodities_vol()
    fg          = get_fear_greed()
    full = (
        "\U0001f4c8 *Markets & Macro -- " + datetime.now().strftime("%H:%M UTC") + "*\n\n"
        + summary + "\n\n"
        + "\U0001f4ca *Key Indicators*\n" + indicators + "\n\n"
        + "\U0001f6e2 *Commodities & Vol*\n" + commodities + "\n\n"
        + "\U0001f628 *Sentiment:* " + fg
    )
    tg_send(message.chat.id, full)


@bot.message_handler(commands=["tech"])
def cmd_tech(message):
    log.info("Received /tech from chat_id=%s", message.chat.id)
    tg_send(message.chat.id, "Fetching AI & tech...")
    data    = get_tech_news()
    summary = summarize(data, mode="tech")
    tg_send(
        message.chat.id,
        "\U0001f916 *AI & Tech -- " + datetime.now().strftime("%H:%M UTC") + "*\n\n" + summary
    )


@bot.message_handler(commands=["liqs"])
def cmd_liqs(message):
    log.info("Received /liqs from chat_id=%s", message.chat.id)
    snapshot = get_hyperliquid_snapshot()
    tg_send(
        message.chat.id,
        "\U0001f4a5 *Hyperliquid Snapshot -- " + datetime.now().strftime("%H:%M UTC") + "*\n\n" + snapshot
    )


# Scheduler

scheduler = BackgroundScheduler(timezone="Europe/Rome")
scheduler.add_job(send_scheduled_brief, "cron", hour=6, minute=0)
scheduler.add_job(send_scheduled_brief, "cron", hour=19, minute=0)
scheduler.add_job(_check_push_accounts, "interval", minutes=10)
scheduler.start()

# Seed push-seen IDs on startup (so we don't flood on first boot)
_check_push_accounts()

log.info("Coffee Brief bot STARTED")
bot.infinity_polling()
