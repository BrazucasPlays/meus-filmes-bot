import os
import time
import tempfile
import urllib.parse

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

import firebase_admin
from firebase_admin import credentials, db, storage

# =========================
# ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID")

if not BOT_TOKEN:
    raise RuntimeError("Defina TELEGRAM_BOT_TOKEN")

if not FIREBASE_DB_URL:
    raise RuntimeError("Defina FIREBASE_DB_URL")

if not FIREBASE_STORAGE_BUCKET:
    raise RuntimeError("Defina FIREBASE_STORAGE_BUCKET")

# =========================
# FIREBASE INIT
# =========================
cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(
    cred,
    {
        "databaseURL": FIREBASE_DB_URL,
        "storageBucket": FIREBASE_STORAGE_BUCKET,
    },
)

bucket = storage.bucket(FIREBASE_STORAGE_BUCKET)

# =========================
# CACHE TEMP
# =========================
pending_movies = {}

# =========================
# HELPERS
# =========================
def allowed_chat(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)
    except Exception:
        return False


def build_download_url(blob):
    path = urllib.parse.quote(blob.name, safe="")
    return (
        f"https://firebasestorage.googleapis.com/v0/b/"
        f"{bucket.name}/o/{path}?alt=media"
    )


# =========================
# HANDLERS
# =========================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    pending_movies[chat_id] = {
        "poster_file_id": photo.file_id,
        "created_at": time.time(),
    }

    await update.message.reply_text(
        "✅ Capa recebida. Agora envie o TEXTO do filme e depois o VÍDEO."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return

    text = update.message.text or ""
    if "título" not in text.lower() and "titulo" not in text.lower():
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)
    if not pending:
        return

    pending["metadata_text"] = text
    pending_movies[chat_id] = pending

    await update.message.reply_text(
        "📝 Texto recebido. Agora envie o ARQUIVO DE VÍDEO."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed_chat(update):
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)

    if not pending:
        await update.message.reply_text(
            "⚠️ Envie CAPA → TEXTO → VÍDEO nessa ordem."
        )
        return

    file_obj = update.message.video or update.message.document
    if not file_obj:
        return

    pending["video_file_id"] = file_obj.file_id
    pending_movies[chat_id] = pending

    await update.message.reply_text("📥 Salvando filme no Firebase...")

    try:
        await save_movie(context, chat_id)
        await update.message.reply_text(
            "✅ Filme salvo! Já deve aparecer no app."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {e}")


# =========================
# SAVE MOVIE
# =========================
async def save_movie(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    pending = pending_movies.pop(chat_id)

    movies_ref = db.reference("movies")
    new_ref = movies_ref.push()
    movie_id = new_ref.key

    bot = context.bot

    # POSTER
    poster_file = await bot.get_file(pending["poster_file_id"])
    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        await poster_file.download_to_drive(tmp.name)
        blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        blob.upload_from_filename(tmp.name)
    poster_url = build_download_url(blob)

    # VIDEO
    video_file = await bot.get_file(pending["video_file_id"])
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        await video_file.download_to_drive(tmp.name)
        vblob = bucket.blob(f"movies/{movie_id}/video.mp4")
        vblob.upload_from_filename(tmp.name)
    video_url = build_download_url(vblob)

    new_ref.set(
        {
            "title": "Filme",
            "posterUrl": poster_url,
            "videoUrl": video_url,
            "createdAt": int(time.time() * 1000),
        }
    )


# =========================
# MAIN
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )

    print("🤖 Bot online 24h")
    app.run_polling()


if __name__ == "__main__":
    main()
