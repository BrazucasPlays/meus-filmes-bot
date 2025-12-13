import os
import time
import tempfile
import urllib.parse

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

import firebase_admin
from firebase_admin import credentials, db, storage


# =========================
# VARIÁVEIS DE AMBIENTE
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_GROUP_ID")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN não definido")
if not FIREBASE_DB_URL:
    raise RuntimeError("FIREBASE_DB_URL não definido")
if not FIREBASE_STORAGE_BUCKET:
    raise RuntimeError("FIREBASE_STORAGE_BUCKET não definido")


# =========================
# FIREBASE INIT
# =========================
cred = credentials.Certificate("firebase-key.json")

firebase_admin.initialize_app(
    cred,
    {
        "databaseURL": FIREBASE_DB_URL,
        "storageBucket": FIREBASE_STORAGE_BUCKET,
    }
)

bucket = storage.bucket()


# =========================
# MEMÓRIA TEMPORÁRIA
# =========================
pending_movies = {}


# =========================
# HELPERS
# =========================
def check_chat(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)
    except Exception:
        return False


def build_public_url(blob):
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"


# =========================
# HANDLERS
# =========================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    pending_movies[chat_id] = {
        "poster": photo.file_id,
        "created": time.time(),
    }

    await update.message.reply_text("✅ Capa recebida. Envie agora o TEXTO do filme.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    if "título" not in text.lower() and "titulo" not in text.lower():
        return

    pending = pending_movies.get(chat_id)
    if not pending:
        await update.message.reply_text("⚠️ Envie primeiro a CAPA do filme.")
        return

    pending["text"] = text
    pending_movies[chat_id] = pending

    await update.message.reply_text("📝 Texto recebido. Agora envie o VÍDEO.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)

    if not pending:
        await update.message.reply_text("⚠️ Ordem correta: CAPA → TEXTO → VÍDEO.")
        return

    file = update.message.video or update.message.document
    if not file:
        return

    bot = context.bot

    await update.message.reply_text("📥 Salvando filme no Firebase...")

    # Criar registro
    movie_ref = db.reference("movies").push()
    movie_id = movie_ref.key

    # CAPA
    poster_file = await bot.get_file(pending["poster"])
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await poster_file.download_to_drive(tmp.name)
        poster_blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        poster_blob.upload_from_filename(tmp.name)

    poster_url = build_public_url(poster_blob)

    # VIDEO
    video_file = await bot.get_file(file.file_id)
    ext = ".mp4"
    if video_file.file_path and "." in video_file.file_path:
        ext = "." + video_file.file_path.split(".")[-1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        await video_file.download_to_drive(tmp.name)
        video_blob = bucket.blob(f"movies/{movie_id}/video{ext}")
        video_blob.upload_from_filename(tmp.name)

    video_url = build_public_url(video_blob)

    # SALVAR DB
    movie_ref.set({
        "title": pending["text"],
        "posterUrl": poster_url,
        "videoUrl": video_url,
        "createdAt": int(time.time() * 1000),
    })

    pending_movies.pop(chat_id, None)

    await update.message.reply_text("✅ Filme salvo! Já aparece no app.")


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    print("🤖 Bot online 24h")
    app.run_polling()


if __name__ == "__main__":
    main()
