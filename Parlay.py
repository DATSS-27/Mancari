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

# === KONFIGURASI DASAR ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
API_HOST = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}
CONCURRENT_PREDICTIONS = 5
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === BACA LEAGUES DARI FILE JSON ===
def load_league_ids():
    try:
        with open("leagues.json", "r", encoding="utf-8") as f:
            leagues = json.load(f)
        league_ids = [int(l["id"]) for l in leagues if "id" in l]
        logger.info(f"📚 Ditemukan {len(league_ids)} liga dari leagues.json")
        return league_ids
    except Exception as e:
        logger.error(f"Gagal membaca leagues.json: {e}")
        return []

# === UTILITAS WAKTU ===
def utc_to_local_str(utc_str):
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone(ZoneInfo("Asia/Makassar"))
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return utc_str

# === FETCH DATA ===
async def fetch_json(session, url, params=None):
    for _ in range(3):
        try:
            async with session.get(url, params=params) as res:
                if res.status == 200:
                    return await res.json()
                await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Gagal fetch {url}: {e}")
            await asyncio.sleep(2)
    return None

async def fetch_fixtures(session, league_ids):
    """Ambil pertandingan hari ini & besok, filter berdasarkan leagues.json"""
    tz = ZoneInfo("Asia/Makassar")
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    fixtures = []

    for d in [today, tomorrow]:
        params = {
            "date": d.strftime("%Y-%m-%d"),
            "status": "NS",
            "timezone": "Asia/Makassar",
        }
        data = await fetch_json(session, f"{API_HOST}/fixtures", params)
        if not data or not data.get("response"):
            continue

        for f in data["response"]:
            lid = f.get("league", {}).get("id")
            if lid in league_ids:
                fixtures.append(f)

    logger.info(f"✅ Total fixture ditemukan: {len(fixtures)}")
    return fixtures

async def fetch_prediction_for_fixture(session, fixture_id):
    data = await fetch_json(session, f"{API_HOST}/predictions", {"fixture": fixture_id})
    if data and data.get("response"):
        return data["response"][0]
    return None

# === BUILD FILE EXCEL ===
def build_predictions_excel(predictions):
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
    msg = await context.bot.send_message(chat_id, "🔍 Mengambil data pertandingan...")

    league_ids = load_league_ids()
    if not league_ids:
        await msg.edit_text("❌ Gagal memuat leagues.json — pastikan file tersedia di root proyek.")
        return

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        fixtures = await fetch_fixtures(session, league_ids)
        if not fixtures:
            await msg.edit_text("⚠️ Tidak ada pertandingan ditemukan untuk hari ini atau besok.")
            return

        sem = asyncio.Semaphore(CONCURRENT_PREDICTIONS)

        async def sem_fetch(fid):
            async with sem:
                return await fetch_prediction_for_fixture(session, fid)

        raw_predictions = await asyncio.gather(*[
            sem_fetch(f["fixture"]["id"]) for f in fixtures
        ])

    predictions = []
    for idx, raw in enumerate(raw_predictions):
        if not raw:
            continue
        f = fixtures[idx]
        fixture_obj = f.get("fixture", {})
        teams_obj = f.get("teams", {})

        def calc_strength(stats, side):
            played = stats.get("played", {}).get(side, 0)
            wins = stats.get("wins", {}).get(side, 0)
            return round((wins / played) * 100, 1) if played else "-"

        home_stats = raw.get("teams", {}).get("home", {}).get("league", {}).get("fixtures", {}) or {}
        away_stats = raw.get("teams", {}).get("away", {}).get("league", {}).get("fixtures", {}) or {}

        predictions.append({
            "date": utc_to_local_str(fixture_obj.get("date")),
            "league": raw.get("league", {}).get("name", ""),
            "teams": {
                "home": teams_obj.get("home", {}).get("name", ""),
                "away": teams_obj.get("away", {}).get("name", "")
            },
            "advice": raw.get("predictions", {}).get("advice", ""),
            "home_last5": raw.get("teams", {}).get("home", {}).get("last_5", {}),
            "away_last5": raw.get("teams", {}).get("away", {}).get("last_5", {}),
            "home_form": (raw.get("teams", {}).get("home", {}).get("league", {}).get("form") or "")[-5:],
            "away_form": (raw.get("teams", {}).get("away", {}).get("league", {}).get("form") or "")[-5:],
            "strength": {
                "home": calc_strength(home_stats, "home"),
                "away": calc_strength(away_stats, "away")
            }
        })

    if not predictions:
        await msg.edit_text("⚠️ Tidak ada prediksi tersedia dari API untuk liga yang dipilih.")
        return

    file_stream = build_predictions_excel(predictions)
    await context.bot.send_document(
        chat_id=chat_id,
        document=InputFile(file_stream, filename="predictions.xlsx"),
        caption=f"📊 Prediksi {len(predictions)} pertandingan dari leagues.json"
    )
    await msg.delete()

# === MAIN (WEBHOOK UNTUK RAILWAY) ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("get", get_handler))

    logger.info("🚀 Bot Telegram berjalan dalam mode webhook (Railway)")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
