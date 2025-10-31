import os
import json
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes
from openpyxl import Workbook
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
API_HOST = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
CACHE_FILE = "cache_fixtures.json"
CONCURRENT_PREDICTIONS = 5
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === UTILITAS CACHE ===
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    tz = ZoneInfo("Asia/Makassar")
    today = datetime.now(tz).date()
    expired = [key for key in data.keys() if datetime.strptime(key, "%Y-%m-%d").date() < today]

    if expired:
        for key in expired:
            del data[key]
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"🧹 Cache lama dihapus: {', '.join(expired)}")

    return data

def save_cache(data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_today_key():
    tz = ZoneInfo("Asia/Makassar")
    return datetime.now(tz).strftime("%Y-%m-%d")

# === FETCH DATA ===
async def fetch_json(session, url, params=None):
    for _ in range(3):
        try:
            async with session.get(url, params=params) as res:
                if res.status == 200:
                    return await res.json()
                await asyncio.sleep(2)
        except Exception:
            await asyncio.sleep(2)
    return None

async def fetch_fixtures_for_league(session, league_id):
    tz = ZoneInfo("Asia/Makassar")
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    fixtures = []

    for d in [today, tomorrow]:
        params = {
            "date": d.strftime("%Y-%m-%d"),
            "status": "NS",
            "timezone": "Asia/Makassar"
        }
        data = await fetch_json(session, f"{API_HOST}/fixtures", params)
        if not data or not data.get("response"):
            continue

        for fixture in data["response"]:
            try:
                if fixture.get("league", {}).get("id") == league_id:
                    fixtures.append(fixture)
            except Exception:
                continue
    return fixtures

async def fetch_prediction_for_fixture(session, fixture_id):
    return await fetch_json(session, f"{API_HOST}/predictions", {"fixture": fixture_id})

def load_league_map():
    if not os.path.exists("leagues.json"):
        return {}
    with open("leagues.json", "r") as f:
        return json.load(f)

def utc_to_local_str(utc_str):
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone(ZoneInfo("Asia/Makassar"))
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return utc_str

def build_predictions_excel(predictions):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Tanggal", "Liga", "Home", "Away", "Saran", "Form Home", "Form Away",
        "Att Home", "Att Away", "Def Home", "Def Away",
        "Strength Home", "Strength Away"
    ])
    for p in predictions:
        ws.append([
            p.get("date", ""),
            p.get("league", ""),
            p.get("teams", {}).get("home", ""),
            p.get("teams", {}).get("away", ""),
            p.get("advice", ""),
            p.get("home_form", ""),
            p.get("away_form", ""),
            p.get("home_last5", {}).get("att", ""),
            p.get("away_last5", {}).get("att", ""),
            p.get("home_last5", {}).get("def", ""),
            p.get("away_last5", {}).get("def", ""),
            p.get("strength", {}).get("home", ""),
            p.get("strength", {}).get("away", "")
        ])
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream

# === COMMAND HANDLER ===
async def get_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id, "⏳ Mengecek cache data hari ini...")
    today_key = get_today_key()
    cache = load_cache()

    if today_key in cache and "predictions" in cache[today_key]:
        await msg.edit_text("📂 Mengambil data dari cache lokal...")
        predictions = cache[today_key]["predictions"]
    else:
        await msg.edit_text("🔍 Mengambil data dari API...")
        league_map = load_league_map()
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            sem = asyncio.Semaphore(CONCURRENT_PREDICTIONS)
            tasks = [fetch_fixtures_for_league(session, lid) for lid in league_map.keys()]
            all_results = await asyncio.gather(*tasks)

            fixtures = []
            seen = set()
            for lst in all_results:
                for f in lst or []:
                    fid = f.get("fixture", {}).get("id")
                    if fid and fid not in seen:
                        seen.add(fid)
                        fixtures.append(f)

            async def sem_fetch(fid):
                async with sem:
                    return await fetch_prediction_for_fixture(session, fid)
            raw_predictions = await asyncio.gather(*[sem_fetch(f["fixture"]["id"]) for f in fixtures])

        predictions = []
        for idx, raw in enumerate(raw_predictions):
            if not raw or not raw.get("response"):
                continue
            resp = raw["response"][0]
            fixture_obj = fixtures[idx].get("fixture", {})
            teams_obj = fixtures[idx].get("teams", {})

            def calc_strength(stats, side):
                played = stats.get("played", {}).get(side, 0)
                wins = stats.get("wins", {}).get(side, 0)
                return round((wins / played) * 100, 1) if played else "-"

            home_stats = resp.get("teams", {}).get("home", {}).get("league", {}).get("fixtures", {}) or {}
            away_stats = resp.get("teams", {}).get("away", {}).get("league", {}).get("fixtures", {}) or {}

            predictions.append({
                "date": utc_to_local_str(fixture_obj.get("date")),
                "league": resp.get("league", {}).get("name", ""),
                "teams": {
                    "home": teams_obj.get("home", {}).get("name", ""),
                    "away": teams_obj.get("away", {}).get("name", "")
                },
                "advice": resp.get("predictions", {}).get("advice", ""),
                "home_last5": resp.get("teams", {}).get("home", {}).get("last_5", {}),
                "away_last5": resp.get("teams", {}).get("away", {}).get("last_5", {}),
                "home_form": (resp.get("teams", {}).get("home", {}).get("league", {}).get("form") or "")[-5:],
                "away_form": (resp.get("teams", {}).get("away", {}).get("league", {}).get("form") or "")[-5:],
                "comparison": resp.get("comparison", {}),
                "strength": {
                    "home": calc_strength(home_stats, "home"),
                    "away": calc_strength(away_stats, "away")
                }
            })
        cache[today_key] = {"fixtures": [f["fixture"]["id"] for f in fixtures], "predictions": predictions}
        save_cache(cache)
        await msg.edit_text("💾 Data disimpan ke cache lokal!")

    file_stream = build_predictions_excel(predictions)
    await context.bot.send_document(chat_id=chat_id, document=InputFile(file_stream, filename="predictions.xlsx"))
    await msg.delete()

# === MAIN (WEBHOOK MODE) ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("get", get_handler))

    # Jalankan webhook server
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"{BOT_TOKEN}",
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()


