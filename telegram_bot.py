import os
import tempfile
import time
import urllib.parse
import asyncio

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

# --------------------
# ENV
# --------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID")

if not BOT_TOKEN:
    raise RuntimeError("Defina TELEGRAM_BOT_TOKEN no Render (Environment Variables)")

# --------------------
# FIREBASE
# --------------------
cred = credentials.Certificate("firebase-key.json")

firebase_admin.initialize_app(
    cred,
    {
        "databaseURL": FIREBASE_DB_URL,
        "storageBucket": FIREBASE_STORAGE_BUCKET,
    },
)

bucket = storage.bucket()

# --------------------
# MEMÓRIA
# --------------------
pending_movies = {}

# --------------------
# HELPERS
# --------------------
def build_download_url(blob):
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"

def check_chat(update: Update):
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)

# --------------------
# HANDLERS
# --------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    pending_movies[chat_id] = {
        "poster_file_id": photo.file_id,
        "created_at": time.time(),
    }

    await update.message.reply_text("✅ Capa recebida. Envie agora o TEXTO do filme.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    text = update.message.text.lower()
    if "título" not in text and "titulo" not in text:
        return

    chat_id = update.effective_chat.id
    pending_movies.setdefault(chat_id, {})["metadata"] = update.message.text

    await update.message.reply_text("📝 Texto recebido. Agora envie o VÍDEO.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)

    if not pending:
        await update.message.reply_text("⚠️ Envie CAPA → TEXTO → VÍDEO")
        return

    file = update.message.video or update.message.document
    tg_file = await file.get_file()

    await update.message.reply_text("⬆️ Enviando para Firebase...")

    movie_ref = db.reference("movies").push()
    movie_id = movie_ref.key

    # POSTER
    poster = await context.bot.get_file(pending["poster_file_id"])
    with tempfile.NamedTemporaryFile(suffix=".jpg") as p:
        await poster.download_to_drive(p.name)
        poster_blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        poster_blob.upload_from_filename(p.name)

    # VIDEO
    with tempfile.NamedTemporaryFile(suffix=".mp4") as v:
        await tg_file.download_to_drive(v.name)
        video_blob = bucket.blob(f"movies/{movie_id}/video.mp4")
        video_blob.upload_from_filename(v.name)

    movie_ref.set({
        "title": pending["metadata"],
        "posterUrl": build_download_url(poster_blob),
        "videoUrl": build_download_url(video_blob),
        "createdAt": int(time.time() * 1000),
    })

    pending_movies.pop(chat_id, None)

    await update.message.reply_text("🎬 Filme salvo com sucesso!")

# --------------------
# MAIN
# --------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    print("🤖 Bot online 24h")
    app.run_polling()

if __name__ == "__main__":
    main()
