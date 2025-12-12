import os
import tempfile
import time
import re
import urllib.parse

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Updater, MessageHandler, Filters, CallbackContext

import firebase_admin
from firebase_admin import credentials, db, storage

# --------------------
# Carregar variáveis de ambiente
# --------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID")  # pode ser vazio

if not BOT_TOKEN:
    raise RuntimeError("Defina TELEGRAM_BOT_TOKEN no .env")

if not FIREBASE_DB_URL:
    raise RuntimeError("Defina FIREBASE_DB_URL no .env")

if not FIREBASE_STORAGE_BUCKET:
    raise RuntimeError("Defina FIREBASE_STORAGE_BUCKET no .env")

# --------------------
# Inicializar Firebase Admin
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
# Memória temporária de filmes por chat
# --------------------
# Estrutura:
# pending_movies[chat_id] = {
#   "created_at": timestamp,
#   "poster_file_id": str,
#   "metadata_text": str,
#   "video_file_id": str,
#   "video_is_document": bool
# }
pending_movies = {}


# --------------------
# Funções auxiliares para parsing
# --------------------
def extract_field(text: str, label: str):
    """Pega o campo depois de 'Título:' / 'Diretor:' etc."""
    idx = text.lower().find(label.lower())
    if idx == -1:
        return None
    rest = text[idx + len(label) :]

    # corta em algum separador comum
    for sep in ["\n", "🎙", "📆", "⭐", "Sinopse:", "SINOPSE:"]:
        pos = rest.find(sep)
        if pos != -1:
            rest = rest[:pos]
    return rest.strip(" -:|\n\r\t")


def parse_metadata(text: str):
    """Recebe o texto completo e devolve um dicionário com os campos."""
    data = {}

    # normalizar emojis em quebras de linha pra facilitar
    norm = (
        text.replace("🎬", "\n")
        .replace("🎙", "\n")
        .replace("📆", "\n")
        .replace("⭐️", "\n")
        .replace("⭐", "\n")
    )

    data["title"] = extract_field(norm, "Título:") or extract_field(
        norm, "Titulo:"
    )
    data["director"] = extract_field(norm, "Diretor:") or extract_field(
        norm, "Director:"
    )
    data["audio"] = extract_field(norm, "Áudio:") or extract_field(
        norm, "Audio:"
    )
    data["year"] = extract_field(norm, "Lançamento:") or extract_field(
        norm, "Ano:"
    )
    data["genres"] = extract_field(norm, "Gêneros:") or extract_field(
        norm, "Generos:"
    )

    # Sinopse: pega tudo depois de "Sinopse:"
    sinopse_idx = norm.lower().find("sinopse:")
    if sinopse_idx != -1:
        data["synopsis"] = norm[sinopse_idx + len("sinopse:") :].strip()
    else:
        data["synopsis"] = None

    return data


def build_download_url(blob):
    """Gera URL pública usando o endpoint padrão do Firebase Storage."""
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"


# --------------------
# Lógica do BOT
# --------------------
def _check_chat(update: Update):
    chat_id = update.effective_chat.id
    if ALLOWED_CHAT_ID:
        try:
            allowed = int(ALLOWED_CHAT_ID)
        except ValueError:
            allowed = ALLOWED_CHAT_ID
        return chat_id == allowed
    return True  # aceita qualquer chat se não configurar


def handle_photo(update: Update, context: CallbackContext):
    if not _check_chat(update):
        return

    chat_id = update.effective_chat.id
    message = update.effective_message

    photo = message.photo[-1]  # melhor qualidade
    file_id = photo.file_id

    pending = pending_movies.get(chat_id, {})
    pending["poster_file_id"] = file_id
    pending["created_at"] = time.time()
    pending_movies[chat_id] = pending

    message.reply_text("✅ Capa recebida. Agora envie o texto do filme e depois o vídeo.")


def handle_text(update: Update, context: CallbackContext):
    if not _check_chat(update):
        return

    chat_id = update.effective_chat.id
    message = update.effective_message
    text = message.text or ""

    # só nos interessa se tiver "Título" e "Sinopse"
    if "título" not in text.lower() and "titulo" not in text.lower():
        return

    pending = pending_movies.get(chat_id, {})
    pending["metadata_text"] = text
    pending["created_at"] = time.time()
    pending_movies[chat_id] = pending

    message.reply_text("📝 Informações do filme recebidas. Agora envie o arquivo de vídeo (mp4, mkv, etc.).")


def handle_video_or_document(update: Update, context: CallbackContext):
    if not _check_chat(update):
        return

    chat_id = update.effective_chat.id
    message = update.effective_message

    video = message.video
    document = message.document

    file_obj = None
    is_document = False

    if video:
        file_obj = video
        is_document = False
    elif document and document.mime_type.startswith("video/"):
        file_obj = document
        is_document = True
    else:
        return  # não é vídeo

    pending = pending_movies.get(chat_id)
    if not pending:
        message.reply_text(
            "⚠️ Primeiro envie a CAPA (imagem) e o TEXTO com Título/Diretor/Sinopse, depois o vídeo."
        )
        return

    pending["video_file_id"] = file_obj.file_id
    pending["video_is_document"] = is_document
    pending_movies[chat_id] = pending

    message.reply_text("📥 Recebi o vídeo, estou salvando no Firebase...")

    try:
        save_movie_to_firebase(context, chat_id)
        message.reply_text("✅ Filme salvo no Firebase! Ele já deve aparecer no app em alguns instantes.")
    except Exception as e:
        message.reply_text(f"❌ Erro ao salvar filme: {e}")


def save_movie_to_firebase(context: CallbackContext, chat_id):
    pending = pending_movies.get(chat_id)
    if not pending:
        raise RuntimeError("Nada pendente para este chat.")

    if not (
        pending.get("poster_file_id")
        and pending.get("metadata_text")
        and pending.get("video_file_id")
    ):
        raise RuntimeError("Capa, texto ou vídeo faltando. Envie na ordem: capa -> texto -> vídeo.")

    metadata = parse_metadata(pending["metadata_text"])

    # se mesmo assim não tiver título, tenta pegar do nome do vídeo (caption ou file_name)
    title = metadata.get("title")
    if not title:
        metadata["title"] = "Filme sem título"

    # criar id no Realtime Database
    movies_ref = db.reference("movies")
    new_movie_ref = movies_ref.push()
    movie_id = new_movie_ref.key

    # 1) baixar e subir CAPA
    bot = context.bot

    poster_file = bot.get_file(pending["poster_file_id"])
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_poster:
        poster_file.download(custom_path=tmp_poster.name)
        poster_blob = bucket.blob(f"movies/{movie_id}/poster.jpg")
        poster_blob.upload_from_filename(tmp_poster.name)
    poster_url = build_download_url(poster_blob)

    # 2) baixar e subir VÍDEO
    video_file = bot.get_file(pending["video_file_id"])
    # tenta manter extensão
    ext = ".mp4"
    if pending.get("video_is_document") and video_file.file_path:
        # file_path geralmente contém a extensão
        parts = video_file.file_path.split(".")
        if len(parts) > 1:
            ext = "." + parts[-1]

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_video:
        video_file.download(custom_path=tmp_video.name)
        video_blob = bucket.blob(f"movies/{movie_id}/video{ext}")
        video_blob.upload_from_filename(tmp_video.name)
    video_url = build_download_url(video_blob)

    # 3) salvar metadados no Realtime Database
    now_ms = int(time.time() * 1000)

    new_movie_ref.set(
        {
            "title": metadata.get("title"),
            "director": metadata.get("director"),
            "audio": metadata.get("audio"),
            "year": metadata.get("year"),
            "genres": metadata.get("genres"),
            "synopsis": metadata.get("synopsis"),
            "posterUrl": poster_url,
            "videoUrl": video_url,
            "createdAt": now_ms,
        }
    )

    # limpar pendente
    pending_movies.pop(chat_id, None)


def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.photo & ~Filters.command, handle_photo))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    dp.add_handler(
        MessageHandler(
            (Filters.video | Filters.document.video) & ~Filters.command,
            handle_video_or_document,
        )
    )

    print("Bot rodando... CTRL+C para parar.")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
