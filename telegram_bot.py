import os
import time
import tempfile
import urllib.parse
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
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
    raise RuntimeError("TELEGRAM_BOT_TOKEN não definido")

# --------------------
# Firebase
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

pending_movies = {}

# --------------------
# Helpers
# --------------------
def build_download_url(blob):
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"


def check_chat(update: Update):
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)


# --------------------
# Handlers
# --------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    pending_movies[chat_id] = {
        "poster": photo.file_id,
        "time": time.time(),
    }

    await update.message.reply_text("✅ Capa recebida. Envie agora o TEXTO do filme.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    if "título" not in text.lower() and "titulo" not in text.lower():
        return

    pending_movies.setdefault(chat_id, {})["text"] = text
    await update.message.reply_text("📝 Texto recebido. Agora envie o VÍDEO.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    data = pending_movies.get(chat_id)

    if not data or "poster" not in data or "text" not in data:
        await update.message.reply_text("⚠️ Envie: capa → texto → vídeo.")
        return

    file = update.message.video or update.message.document
    tg_file = await context.bot.get_file(file.file_id)

    movie_ref = db.reference("movies").push()
    movie_id = movie_ref.key

    # Poster
    poster_file = await context.bot.get_file(data["poster"])
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await poster_file.download_to_drive(tmp.name)
        blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        blob.upload_from_filename(tmp.name)
    poster_url = build_download_url(blob)

    # Video
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        vblob = bucket.blob(f"movies/{movie_id}/video.mp4")
        vblob.upload_from_filename(tmp.name)
    video_url = build_download_url(vblob)

    movie_ref.set(
        {
            "title": "Filme",
            "posterUrl": poster_url,
            "videoUrl": video_url,
            "createdAt": int(time.time() * 1000),
        }
    )

    pending_movies.pop(chat_id, None)
    await update.message.reply_text("🎉 Filme salvo no Firebase com sucesso!")


# --------------------
# MAIN
# --------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )

    print("🤖 Bot online 24h...")
    app.run_polling(drop_pending_updates=True)



if __name__ == "__main__":
    main()

