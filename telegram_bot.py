import os
import tempfile
import time
import urllib.parse

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

import firebase_admin
from firebase_admin import credentials, db, storage

# =========================
# CONFIGURAÇÃO
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID")  # opcional

if not BOT_TOKEN:
    raise RuntimeError("Defina TELEGRAM_BOT_TOKEN")
if not FIREBASE_DB_URL:
    raise RuntimeError("Defina FIREBASE_DB_URL")
if not FIREBASE_STORAGE_BUCKET:
    raise RuntimeError("Defina FIREBASE_STORAGE_BUCKET")

# =========================
# FIREBASE INIT
# =========================

if not firebase_admin._apps:
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
# MEMÓRIA TEMPORÁRIA
# =========================

pending_movies = {}

# =========================
# HELPERS
# =========================

def _check_chat(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)
    except:
        return False


def extract_field(text: str, label: str):
    idx = text.lower().find(label.lower())
    if idx == -1:
        return None
    rest = text[idx + len(label):]
    for sep in ["\n", "🎙", "📆", "⭐", "Sinopse:", "SINOPSE:"]:
        pos = rest.find(sep)
        if pos != -1:
            rest = rest[:pos]
    return rest.strip(" -:\n\r\t")


def parse_metadata(text: str):
    norm = (
        text.replace("🎬", "\n")
        .replace("🎙", "\n")
        .replace("📆", "\n")
        .replace("⭐️", "\n")
        .replace("⭐", "\n")
    )

    data = {
        "title": extract_field(norm, "Título:") or extract_field(norm, "Titulo:"),
        "director": extract_field(norm, "Diretor:"),
        "audio": extract_field(norm, "Áudio:") or extract_field(norm, "Audio:"),
        "year": extract_field(norm, "Lançamento:") or extract_field(norm, "Ano:"),
        "genres": extract_field(norm, "Gêneros:") or extract_field(norm, "Generos:"),
        "synopsis": None,
    }

    idx = norm.lower().find("sinopse:")
    if idx != -1:
        data["synopsis"] = norm[idx + len("sinopse:"):].strip()

    return data


def build_download_url(blob):
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"

# =========================
# HANDLERS
# =========================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    pending_movies[chat_id] = {
        "poster_file_id": photo.file_id,
        "created_at": time.time(),
    }

    await update.message.reply_text(
        "✅ Capa recebida.\nAgora envie o TEXTO do filme e depois o VÍDEO."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_chat(update):
        return

    text = update.message.text.lower()
    if "título" not in text and "titulo" not in text:
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id, {})
    pending["metadata_text"] = update.message.text
    pending_movies[chat_id] = pending

    await update.message.reply_text(
        "📝 Texto recebido.\nAgora envie o ARQUIVO DE VÍDEO (mp4 ou mkv)."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_chat(update):
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)

    if not pending:
        await update.message.reply_text(
            "⚠️ Envie primeiro: CAPA → TEXTO → VÍDEO"
        )
        return

    video = update.message.video or update.message.document
    pending["video_file_id"] = video.file_id
    pending_movies[chat_id] = pending

    await update.message.reply_text("📥 Salvando no Firebase...")
    await save_movie(context, chat_id)
    await update.message.reply_text("✅ Filme salvo! Já aparece no app.")


async def save_movie(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    pending = pending_movies.get(chat_id)
    if not pending:
        return

    metadata = parse_metadata(pending.get("metadata_text", ""))
    metadata["title"] = metadata.get("title") or "Filme sem título"

    ref = db.reference("movies").push()
    movie_id = ref.key

    # POSTER
    poster_file = await context.bot.get_file(pending["poster_file_id"])
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        await poster_file.download_to_drive(f.name)
        blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        blob.upload_from_filename(f.name)
    poster_url = build_download_url(blob)

    # VIDEO
    video_file = await context.bot.get_file(pending["video_file_id"])
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        await video_file.download_to_drive(f.name)
        blob = bucket.blob(f"movies/{movie_id}/video.mp4")
        blob.upload_from_filename(f.name)
    video_url = build_download_url(blob)

    ref.set({
        **metadata,
        "posterUrl": poster_url,
        "videoUrl": video_url,
        "createdAt": int(time.time() * 1000),
    })

    pending_movies.pop(chat_id, None)

# =========================
# MAIN
# =========================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    print("🤖 BOT ONLINE 24H - escutando mensagens")
    app.run_polling()

if __name__ == "__main__":
    main()

