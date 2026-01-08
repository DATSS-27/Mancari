import os
import json
import asyncio
import aiohttp
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule

# ======================================================
# CONFIG (ENV)
# ======================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
FOOTBALL_API_KEY = os.environ["API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

API_HOST = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": FOOTBALL_API_KEY
}

CONCURRENT_PREDICTIONS = 3
STATE_FILE = "state.json"

# ======================================================
# GLOBAL STATE
# ======================================================
SELECTED_LEAGUE = {}
FIXTURE_IDS = []

# ======================================================
# STATE FILE HANDLER (PALING AMAN UNTUK RAILWAY FREE)
# ======================================================
def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "league": SELECTED_LEAGUE,
                "fixtures": FIXTURE_IDS,
            },
            f
        )

def load_state():
    global SELECTED_LEAGUE, FIXTURE_IDS
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            SELECTED_LEAGUE = data.get("league", {})
            FIXTURE_IDS = data.get("fixtures", [])
    except FileNotFoundError:
        SELECTED_LEAGUE = {}
        FIXTURE_IDS = []

def reset_state():
    global SELECTED_LEAGUE, FIXTURE_IDS
    SELECTED_LEAGUE = {}
    FIXTURE_IDS = []
    save_state()  # overwrite file with empty state

# ======================================================
# UTILITIES
# ======================================================
async def fetch_json(session, endpoint, params=None):
    async with session.get(endpoint, params=params) as resp:
        if resp.status != 200:
            return None
        return await resp.json()

async def fetch_prediction_for_fixture(session, fixture_id):
    data = await fetch_json(
        session,
        f"{API_HOST}/predictions",
        {"fixture": fixture_id}
    )
    if data and data.get("response"):
        return data["response"][0]
    return None

# ======================================================
# EXCEL BUILDER (COLOR GRADIENT)
# ======================================================
def build_predictions_excel(predictions):
    wb = Workbook()
    ws = wb.active
    ws.title = "Predictions"

    ws.append([
        "Tanggal", "Liga", "Home", "Away", "Saran",
        "Form Home", "Form Away",
        "Att Home", "Att Away", "Δ Att",
        "Def Home", "Def Away", "Δ Def",
        "Strength Home", "Strength Away", "Δ Strength"
    ])

    for p in predictions:
        att_h = p["home_last5"].get("att", 0)
        att_a = p["away_last5"].get("att", 0)
        def_h = p["home_last5"].get("def", 0)
        def_a = p["away_last5"].get("def", 0)
        str_h = p["strength"]["home"]
        str_a = p["strength"]["away"]

        ws.append([
            p["date"],
            p["league"],
            p["teams"]["home"],
            p["teams"]["away"],
            p["advice"],
            p["home_form"],
            p["away_form"],
            att_h, att_a, att_h - att_a,
            def_h, def_a, def_h - def_a,
            str_h, str_a, str_h - str_a
        ])

    max_row = ws.max_row

    rule = ColorScaleRule(
        start_type="num", start_value=-100, start_color="FF8B0000",
        mid_type="num", mid_value=0, mid_color="FFD9D9D9",
        end_type="num", end_value=100, end_color="FF006400",
    )

    ws.conditional_formatting.add(f"J2:J{max_row}", rule)
    ws.conditional_formatting.add(f"M2:M{max_row}", rule)
    ws.conditional_formatting.add(f"P2:P{max_row}", rule)

    ws.column_dimensions["J"].hidden = True
    ws.column_dimensions["M"].hidden = True
    ws.column_dimensions["P"].hidden = True

    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream

# ======================================================
# COMMAND: /jadwal
# ======================================================
async def jadwal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_local = datetime.now(ZoneInfo("Asia/Makassar"))
    today = now_local.strftime("%Y-%m-%d")

    params = {
        "date": today,
        "status": "NS",
        "timezone": "Asia/Makassar",
    }

    r = requests.get(
        f"{API_HOST}/fixtures",
        headers=HEADERS,
        params=params,
        timeout=20
    )

    data = r.json()
    fixtures = data.get("response", [])

    if not fixtures:
        await update.message.reply_text(
            f"⚠️ Tidak ada pertandingan tanggal {today}."
        )
        return

    context.bot_data["fixtures"] = fixtures

    leagues = {}
    for f in fixtures:
        league = f["league"]
        league_id = league["id"]
        league_name = league["name"]
        country = league.get("country", "")

        leagues[league_id] = (
            f"{league_name} ({country})"
            if country else league_name
        )

    context.bot_data["leagues"] = leagues

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"league:{lid}")]
        for lid, name in sorted(leagues.items(), key=lambda x: x[1])
    ]

    await update.message.reply_text(
        f"⚽ Pilih liga (tanggal {today}):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
# ======================================================
# CALLBACK: LEAGUE SELECTED
# ======================================================
async def league_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SELECTED_LEAGUE, FIXTURE_IDS

    query = update.callback_query
    await query.answer()

    league_id = int(query.data.split(":")[1])

    fixtures = context.bot_data.get("fixtures", [])
    leagues = context.bot_data.get("leagues", {})

    FIXTURE_IDS = [
        f["fixture"]["id"]
        for f in fixtures
        if f["league"]["id"] == league_id
    ][:15]

    if not FIXTURE_IDS:
        await query.edit_message_text("Tidak ada fixture di liga ini.")
        return

    SELECTED_LEAGUE = {
        "id": league_id,
        "name": leagues.get(league_id, "")
    }

    save_state()

    await query.edit_message_text(
        f"✅ Liga dipilih: *{SELECTED_LEAGUE['name']}*\n"
        f"📌 Total pertandingan: *{len(FIXTURE_IDS)}*",
        parse_mode="Markdown"
    )

# ======================================================
# COMMAND: /prediksi
# ======================================================
async def prediksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FIXTURE_IDS:
        await update.message.reply_text("❌ Pilih liga dulu dengan /jadwal")
        return

    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id, "🔍 Mengambil prediksi...")

    sem = asyncio.Semaphore(CONCURRENT_PREDICTIONS)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async def sem_fetch(fid):
            async with sem:
                return await fetch_prediction_for_fixture(session, fid)

        raw_predictions = await asyncio.gather(
            *[sem_fetch(fid) for fid in FIXTURE_IDS]
        )

    predictions = []

    def calc_strength(stats, side):
        played = stats.get("played", {}).get(side, 0)
        wins = stats.get("wins", {}).get(side, 0)
        return round((wins / played) * 100, 1) if played else 0

    for raw in raw_predictions:
        if not raw:
            continue

        teams = raw.get("teams", {})
        league = raw.get("league", {})

        home_stats = teams.get("home", {}).get("league", {}).get("fixtures", {}) or {}
        away_stats = teams.get("away", {}).get("league", {}).get("fixtures", {}) or {}

        predictions.append({
            "date": datetime.now().strftime("%d-%m-%Y"),
            "league": league.get("name", ""),
            "teams": {
                "home": teams.get("home", {}).get("name", ""),
                "away": teams.get("away", {}).get("name", "")
            },
            "advice": raw.get("predictions", {}).get("advice", ""),
            "home_last5": teams.get("home", {}).get("last_5", {}),
            "away_last5": teams.get("away", {}).get("last_5", {}),
            "home_form": (teams.get("home", {}).get("league", {}).get("form") or "")[-5:],
            "away_form": (teams.get("away", {}).get("league", {}).get("form") or "")[-5:],
            "strength": {
                "home": calc_strength(home_stats, "home"),
                "away": calc_strength(away_stats, "away")
            }
        })

    if not predictions:
        await msg.edit_text("⚠️ Tidak ada prediksi tersedia.")
        return

    excel = build_predictions_excel(predictions)

    await context.bot.send_document(
        chat_id=chat_id,
        document=InputFile(excel, filename="predictions.xlsx"),
        caption=f"📊 Prediksi {len(predictions)} pertandingan\n🏆 {SELECTED_LEAGUE['name']}"
    )

    await msg.delete()

    # ✅ PALING PENTING: RESET FILE & STATE
    reset_state()

# ======================================================
# MAIN (WEBHOOK)
# ======================================================
def main():
    load_state()

    PORT = int(os.environ.get("PORT", 8080))
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("jadwal", jadwal))
    app.add_handler(CallbackQueryHandler(league_selected, pattern="^league:"))
    app.add_handler(CommandHandler("prediksi", prediksi))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()





