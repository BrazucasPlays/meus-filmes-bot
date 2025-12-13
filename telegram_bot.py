import os
import asyncio
import threading
import time
import tempfile
import urllib.parse

from flask import Flask

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

import firebase_admin
from firebase_admin import credentials, db, storage

# ======================================================
# ENV
# ======================================================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN não definido")

if not FIREBASE_DB_URL:
    raise RuntimeError("FIREBASE_DB_URL não definido")

if not FIREBASE_STORAGE_BUCKET:
    raise RuntimeError("FIREBASE_STORAGE_BUCKET não definido")

# ======================================================
# FIREBASE INIT (APENAS UMA VEZ)
# ======================================================
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-key.json")
    firebase_admin.initialize_app(
        cred,
        {
            "databaseURL": FIREBASE_DB_URL,
            "storageBucket": FIREBASE_STORAGE_BUCKET,
        },
    )

bucket = storage.bucket()
movies_ref = db.reference("movies")

# ======================================================
# FLASK (OBRIGATÓRIO PARA RENDER FREE)
# ======================================================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "🤖 Bot online 24h", 200

# ======================================================
# MEMÓRIA TEMPORÁRIA
# ======================================================
pending_movies = {}

# ======================================================
# HELPERS
# ======================================================
def build_download_url(blob):
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"


def check_chat(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)


def parse_metadata(text: str):
    def get(label):
        for line in text.splitlines():
            if label.lower() in line.lower():
                return line.split(":", 1)[-1].strip()
        return None

    return {
        "title": get("Título") or "Sem título",
        "director": get("Diretor"),
        "audio": get("Áudio"),
        "year": get("Lançamento"),
        "genres": get("Gêneros"),
        "synopsis": text.split("Sinopse:", 1)[-1].strip()
        if "Sinopse:" in text
        else None,
    }

# ======================================================
# HANDLERS
# ======================================================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    pending_movies[chat_id] = {
        "poster_file_id": photo.file_id,
        "created_at": time.time(),
    }

    await update.message.reply_text("✅ Capa recebida. Agora envie o texto do filme.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    if "título" not in text.lower():
        return

    pending = pending_movies.get(chat_id)
    if not pending:
        return

    pending["metadata"] = parse_metadata(text)

    await update.message.reply_text("📝 Texto recebido. Agora envie o vídeo.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)

    if not pending:
        await update.message.reply_text(
            "⚠️ Envie primeiro: capa → texto → vídeo."
        )
        return

    file = update.message.video or update.message.document
    file_id = file.file_id

    await update.message.reply_text("📥 Salvando no Firebase...")

    # ID do filme
    movie_ref = movies_ref.push()
    movie_id = movie_ref.key

    # POSTER
    poster_file = await context.bot.get_file(pending["poster_file_id"])
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await poster_file.download_to_drive(tmp.name)
        poster_blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        poster_blob.upload_from_filename(tmp.name)

    # VIDEO
    video_file = await context.bot.get_file(file_id)
    ext = ".mp4"
    if file.file_name and "." in file.file_name:
        ext = "." + file.file_name.split(".")[-1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        await video_file.download_to_drive(tmp.name)
        video_blob = bucket.blob(f"movies/{movie_id}/video{ext}")
        video_blob.upload_from_filename(tmp.name)

    # DATABASE
    data = pending["metadata"]
    movie_ref.set(
        {
            **data,
            "posterUrl": build_download_url(poster_blob),
            "videoUrl": build_download_url(video_blob),
            "createdAt": int(time.time() * 1000),
        }
    )

    pending_movies.pop(chat_id, None)

    await update.message.reply_text("✅ Filme salvo no Firebase!")

# ======================================================
# BOT LOOP (NÃO FECHA EVENT LOOP)
# ======================================================
async def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )

    print("🤖 Bot online 24h...")
    await app.initialize()
    await app.start()

    while True:
        await asyncio.sleep(3600)

# ======================================================
# THREAD BOT
# ======================================================
def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())

# ======================================================
# MAIN
# ======================================================
if __name__ == "__main__":
    threading.Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)
