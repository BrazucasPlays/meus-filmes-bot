import os
import threading
import time
import asyncio
from flask import Flask

from telegram.ext import Application, MessageHandler, filters
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, db

# =========================
# ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN não definido")

# =========================
# FIREBASE
# =========================
cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred, {
    "databaseURL": FIREBASE_DB_URL
})

# =========================
# TELEGRAM BOT
# =========================
async def handle_message(update, context):
    msg = update.message
    chat_id = str(msg.chat.id)

    if GROUP_ID and chat_id != GROUP_ID:
        return

    if msg.video or msg.document:
        ref = db.reference("movies").push()
        ref.set({
            "title": msg.caption or "Filme sem título",
            "createdAt": int(time.time() * 1000)
        })

        await msg.reply_text("✅ Filme salvo no Firebase!")

async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    print("🤖 Bot online 24h...")
    await app.run_polling(stop_signals=None)

def start_bot():
    asyncio.run(run_bot())

# =========================
# FLASK (PORTA PARA O RENDER)
# =========================
server = Flask(__name__)

@server.route("/")
def home():
    return "🤖 Bot online 24h"

if __name__ == "__main__":
    threading.Thread(target=start_bot, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    server.run(host="0.0.0.0", port=port)
