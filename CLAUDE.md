# CLAUDE.md

## Purpose

**Coffee Brief Bot** is a Telegram bot that posts a "Morning Brief" covering market and world news to a Telegram channel twice a day. It also answers on-demand commands. A brief combines:

- **News summaries** (made by an LLM through Groq) in three sections: Geopolitics & Conflicts, Markets & Macro, AI & Tech. The sources are RSS feeds, X/Twitter accounts read through a self-hosted RSSHub, GDELT, and optionally ACLED conflict events.
- **Structured data** that Python builds directly. This data is never passed through the LLM:
  - stock index, single-stock, and crypto prices (yfinance)
  - commodities and VIX
  - Hyperliquid liquidations
  - the Finnhub economic calendar
  - the Crypto Fear & Greed index (alternative.me)
  - new newsletter posts from Substack and similar feeds
- **Push alerts**: every 10 minutes the bot checks selected X accounts (currently `SITREP_artorias`) and sends each new post to the channel right away.

Bot commands: `/all` (aliases `/brief` and `/full`), `/geo` (alias `/geopolitics`), `/market`, `/tech`, `/liqs`, `/help`.

## Stack

- Python 3. The version is not pinned anywhere in the repo.
- `pyTelegramBotAPI` (`telebot`), which runs long-polling with `bot.infinity_polling()`. No webhook.
- `groq`, using the model `llama-3.3-70b-versatile`.
- `apscheduler` `BackgroundScheduler`, with timezone `Europe/Rome`.
- `feedparser`, `requests`, `yfinance`, `python-dotenv`.
- `websocket-client` is listed in requirements and imported, but nothing uses it.
- No versions are pinned in `requirements.txt`.

## Structure

The whole app is one file.

| Path | What it is |
|---|---|
| `brief_bot.py` | The entire bot (~790 lines). Importing it has side effects: it starts the liquidation poller thread, the scheduler, and Telegram polling. |
| `requirements.txt` | Dependencies, all unpinned. |
| `README.md` | A one-line description. |
| `last_brief.txt` | Created at runtime, not in git. Holds the timestamp of the last brief sent. |

Layout of `brief_bot.py`, from top to bottom:

1. Config and env vars (lines ~10–25).
2. Feed lists: `OSINT_FEEDS`, `MARKET_FEEDS`, `TECH_FEEDS`, `NEWSLETTER_FEEDS`, and `PUSH_ACCOUNTS`. Many feed URLs are built from `RSSHUB_URL`.
3. The Hyperliquid liquidation poller: `_poll_hyperliquid_liquidations`. It runs in a daemon thread and polls the REST endpoint every 120 s.
4. Helpers:
   - `tg_send` sanitizes Markdown, splits text into 4000-character chunks, and resends as plain text if Telegram rejects the Markdown.
   - `safe_parse_feed`, `safe_date`.
   - `sanitize_markdown` wraps bare URLs as `[link](url)`.
   - `deduplicate` drops titles that are too similar (`SequenceMatcher`, ratio above 0.82).
5. ACLED login and fetch (`_acled_login`, `get_acled_news`). Skipped when credentials are not set.
6. The push-alert poller (`_check_push_accounts`).
7. News fetchers (`_fetch_entries` uses a 12 h window) and data fetchers (`get_market_update`, `get_commodities_vol`, `get_fear_greed`, `get_economic_calendar`, `get_hyperliquid_snapshot`).
8. `summarize(raw, mode)`: the Groq prompts for the `geo`, `market`, `tech`, and `all` modes. It tries 3 times; if all fail it falls back to the raw headlines.
9. `build_and_send_brief`: puts the full brief together.
10. Telegram command handlers.
11. Scheduler setup and `bot.infinity_polling()`.

## How to run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# create a .env in the repo root with the variables listed below (load_dotenv() reads it)
python brief_bot.py
```

Notes:
- There is no `.env.example` and no `.gitignore` in the repo. **Make sure `.env` is never committed.** Adding a `.gitignore` that lists `.env` and `last_brief.txt` is recommended.
- **Do not run locally with the production `TELEGRAM_TOKEN` while the Railway deployment is up.** Telegram allows only one `getUpdates` consumer per token. A second one causes 409 Conflict errors, and both instances would post scheduled briefs. Use a separate test bot token and a test `CHANNEL_ID`.
- The bot runs `_check_push_accounts()` right away at startup. Scheduled briefs go out at 06:00 and 19:00 Europe/Rome time.
- If `RSSHUB_URL` is not set, it defaults to a placeholder URL. The Twitter/X feeds then fail quietly and return no entries.

## How to test

There are no automated tests and no test framework, linter, or CI.

- Manual check: run the bot with a test token, then send `/help`, `/geo`, `/market`, `/tech`, `/liqs`, and `/all` to it in a DM.
- `brief_bot.py` connects to Telegram and starts its threads at import time. To unit test functions such as `sanitize_markdown`, `deduplicate`, `_format_entries`, or `_fmt_usd`, first move the startup code into `if __name__ == "__main__":` (or into a `main()` function).
- Syntax check that has no side effects: `python -m py_compile brief_bot.py`.

## Deployment

These facts have been verified:

- The default branch is **`main`**.
- The repo is a single-file Python Telegram bot: `brief_bot.py`, `requirements.txt`, `README.md`.
- The repo has **no GitHub Actions workflows** and **no `railway.json`, `railway.toml`, `Procfile`, or `Dockerfile`**. All build and start settings live in the Railway dashboard or come from Railway's automatic Python detection, not in this repo.
- A comment in `brief_bot.py` (above `RSSHUB_URL`) mentions a **self-hosted RSSHub base URL on Railway**. RSSHub is a separate Railway service that this bot depends on for the X/Twitter feeds.
- GitHub Deployments show **`railway-app[bot]` creating a deployment to the environment `intelligent-endurance / production` for each commit on `main`**. Railway's GitHub integration deploys automatically.

What this means in practice:
- **Every push or merge to `main` goes straight to production.** Do work on branches and open PRs.
- Production runs as one long-lived worker process, not a web service. It must run as **exactly one replica**, because of Telegram polling and because each replica would send its own scheduled briefs.
- The filesystem on Railway is probably ephemeral, so `last_brief.txt` and the in-memory caches (liquidations, push-seen IDs) are lost on every redeploy.
- Environment variables are set in the Railway service settings.

## Environment variables

Names only. All of them are read with `os.getenv` in `brief_bot.py`.

| Name | Required | Used for |
|---|---|---|
| `TELEGRAM_TOKEN` | yes | Telegram bot token (`telebot.TeleBot`) |
| `GROQ_API_KEY` | yes | Groq client for summaries |
| `CHANNEL_ID` | yes | Where scheduled briefs and push alerts are sent |
| `RSSHUB_URL` | effectively yes | Base URL of the self-hosted RSSHub, used for the X/Twitter feeds and push alerts |
| `FINNHUB_KEY` | optional | Finnhub economic calendar |
| `ACLED_EMAIL` | optional | ACLED login. ACLED is skipped if this is empty. |
| `ACLED_PASSWORD` | optional | ACLED login. ACLED is skipped if this is empty. |

## Known issues / TODOs

Found by reading the code.

**Bugs**
- **The liquidation poller crashes every cycle.** `_liq_seen_tids` is declared `global` in `_poll_hyperliquid_liquidations` but never initialized at module level. The first `tid in _liq_seen_tids` raises `NameError`, which gets caught and logged, and the loop retries every 30 s, so nothing is ever collected. Fix: add `_liq_seen_tids = set()` near `liq_cache`.
- **The liquidation snapshot is always empty, even once the poller is fixed.** `get_hyperliquid_snapshot` filters on `l.get("ts", 0) >= cutoff`, but the poller never sets `ts` on its entries. Store `time.time()` (or the trade's own `time` field) as `ts`. As a result, `/liqs` and the brief always say "Quiet".
- **Hyperliquid `recentTrades` probably does not include a `dir` field with "Liquidated…" values.** Check the API response format before relying on this filter.
- **Liquidation thresholds are never used.** `LIQ_THRESHOLDS`, `DEFAULT_LIQ_THRESHOLD`, and `METALS_THRESHOLD` are defined, but nothing filters by size.
- **The seen-IDs trim keeps arbitrary IDs.** It uses `set(list(s)[-1000:])`, and sets have no order, so this does not keep the most recent ones.
- **Timestamps are labelled "UTC" but are not UTC.** They come from `datetime.now()`, which is the container's local time, while the scheduler runs on Europe/Rome.
- **`last_brief.txt` is never read for anything.** `get_last_brief_time()` is never called; all fetch windows are a fixed 12 h.

**Robustness**
- **Markdown errors cause silent fallbacks.** Feed titles and LLM output are not escaped for Telegram Markdown (`_`, `*`, `[`), and chunks are split at a fixed 4000 characters, which can cut a Markdown entity in half. Either way, Telegram rejects the message and `tg_send` resends it as plain text.
- **A missing or invalid `FINNHUB_KEY` is hidden.** The calendar just shows "Quiet day".
- **Expired ACLED sessions skip a brief.** On a 401 the bot logs in again but returns `[]` for that run.
- **Some feeds may be dead.** `feeds.reuters.com` has been discontinued, and Bloomberg and FT may block or limit RSS access. Feed failures are swallowed, so check the logs.
- **The first push poll after a restart can drop posts.** The "seed on first run" heuristic (`len(seen) <= len(new_entries)`) treats that poll as seeding, so new posts that arrived during a restart are not sent.

**Hygiene and security**
- **Anyone can use the bot.** Command handlers have no access control, so any Telegram user can trigger `/all` and spend Groq and API quota.
- **Startup code runs at import time** (threads, scheduler, polling), so the module cannot be imported for tests.
- **Leftovers and missing files:** the `websocket` import is unused, versions are not pinned, and there is no `.gitignore` or `.env.example`.
- **There is no Railway config in the repo,** so the start command and replica count exist only in the Railway dashboard.
