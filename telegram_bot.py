import os
import threading
import time
import tempfile
import urllib.parse
from flask import Flask

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

# ======================================================
# ENV & INIT
# ======================================================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID") # ID do grupo onde o bot deve monitorar

# Validação de variáveis de ambiente
if not all([BOT_TOKEN, FIREBASE_DB_URL, FIREBASE_STORAGE_BUCKET]):
    raise RuntimeError("Variáveis de ambiente incompletas.")

# Inicialização ÚNICA do Firebase
if not firebase_admin._apps:
    try:
        # Certifique-se que 'firebase-key.json' está na raiz do projeto
        cred = credentials.Certificate("firebase-key.json") 
        firebase_admin.initialize_app(
            cred,
            {
                "databaseURL": FIREBASE_DB_URL,
                "storageBucket": FIREBASE_STORAGE_BUCKET,
            },
        )
        print("✅ Firebase inicializado com sucesso.")
    except Exception as e:
        print(f"❌ Erro ao inicializar Firebase: {e}")
        raise

bucket = storage.bucket()
movies_ref = db.reference("movies") # Nó principal do Realtime Database

# ======================================================
# FLASK (Keep-Alive para Render Free)
# ======================================================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "🤖 Bot online 24h", 200

# ======================================================
# MEMÓRIA TEMPORÁRIA
# ======================================================
# Armazena o estado do filme (capa + metadata) por chat
pending_movies = {} 

# ======================================================
# HELPERS
# ======================================================
def build_download_url(blob):
    """Gera uma URL de acesso público para o arquivo no Firebase Storage."""
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"


def check_chat(update: Update) -> bool:
    """Verifica se a mensagem vem do grupo permitido."""
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)


def parse_metadata(text: str):
    """Extrai campos específicos do texto formatado do filme."""
    def get(label):
        for line in text.splitlines():
            # Procura por linhas que contenham o rótulo (ex: "Título:")
            if label.lower() in line.lower():
                # Retorna o texto após os dois pontos
                return line.split(":", 1)[-1].strip()
        return None

    # Tenta extrair a sinopse usando o separador "Sinopse:"
    synopsis = text.split("Sinopse:", 1)[-1].strip() if "Sinopse:" in text else None
    
    return {
        # Campos principais (necessários para o App Flutter)
        "title": get("Título") or "Sem título",
        "synopsis": synopsis,
        
        # Campos extras
        "director": get("Diretor"),
        "audio": get("Áudio"),
        "year": get("Lançamento"),
        "genres": get("Gêneros"),
    }

# ======================================================
# HANDLERS (LÓGICA AUTOMÁTICA)
# ======================================================

# Handler 1: Processa a foto e a legenda (metadata)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1] # Pega a foto de maior resolução
    text = update.message.caption # <--- PEGA A LEGENDA AQUI

    # 🚨 REGRA DE NEGÓCIO: A legenda deve existir e conter "Título" para ser válida.
    if not text or "título" not in text.lower():
        await update.message.reply_text(
            "⚠️ A Capa deve ser enviada **com a legenda** contendo 'Título:' e 'Sinopse:'."
        )
        return

    # Processa e armazena os metadados imediatamente
    metadata = parse_metadata(text)

    pending_movies[chat_id] = {
        "poster_file_id": photo.file_id,
        "metadata": metadata, 
        "created_at": time.time(),
    }

    await update.message.reply_text("✅ Capa e Metadados recebidos. Agora envie o **VÍDEO** do filme.")


# Handler 2: Processa o vídeo, faz uploads e salva no DB
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)

    # Verifica se a capa e a metadata já foram enviadas
    if not pending or "metadata" not in pending:
        await update.message.reply_text(
            "⚠️ Ordem incorreta. Envie: **Capa + Texto** primeiro → **Vídeo**."
        )
        return

    # O vídeo pode vir como 'video' ou 'document' (arquivo de vídeo)
    file = update.message.video or update.message.document 
    file_id = file.file_id

    await update.message.reply_text("📥 Salvando no Firebase... (Isto pode levar tempo)")

    # 1. ID do filme no Realtime Database (Gera a chave única)
    movie_ref = movies_ref.push()
    movie_id = movie_ref.key

    # --- UPLOAD PARA FIREBASE STORAGE ---

    # POSTER
    poster_url = ""
    try:
        poster_file = await context.bot.get_file(pending["poster_file_id"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            await poster_file.download_to_drive(tmp.name)
            poster_blob = bucket.blob(f"movies/{movie_id}/poster.jpg") 
            poster_blob.upload_from_filename(tmp.name)
            poster_url = build_download_url(poster_blob)
    except Exception as e:
        print(f"Erro ao salvar poster: {e}")
        await update.message.reply_text("❌ Falha crítica ao salvar a capa.")
        return

    # VIDEO
    video_url = ""
    try:
        video_file = await context.bot.get_file(file_id)
        # Tenta preservar a extensão original do arquivo
        ext = "." + file.file_name.split(".")[-1] if file.file_name and "." in file.file_name else ".mp4"

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            await video_file.download_to_drive(tmp.name)
            video_blob = bucket.blob(f"movies/{movie_id}/video{ext}") 
            video_blob.upload_from_filename(tmp.name)
            video_url = build_download_url(video_blob)
    except Exception as e:
        print(f"Erro ao salvar vídeo: {e}")
        await update.message.reply_text("❌ Falha crítica ao salvar o vídeo.")
        return

    # 2. SALVAR NO REALTIME DATABASE
    data = pending["metadata"]
    movie_ref.set(
        {
            **data,
            "posterUrl": poster_url, 
            "videoUrl": video_url, 
            "createdAt": int(time.time() * 1000),
        }
    )

    # Limpa a memória temporária
    pending_movies.pop(chat_id, None)

    await update.message.reply_text("✅ Filme salvo no Firebase!")

# ======================================================
# BOT STARTER (Corrigido para a estabilidade no Render)
# ======================================================
def start_polling():
    """Configura e inicia o bot PTB em polling."""
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers para o fluxo de 2 etapas
    app.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, handle_photo))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )

    print("🤖 Bot Telegram iniciando...")
    
    # run_polling é síncrono e BLOQUEIA esta thread, mantendo o bot vivo.
    app.run_polling(drop_pending_updates=True, stop_signals=None) 

# ======================================================
# MAIN
# ======================================================
if __name__ == "__main__":
    # 1. Inicia o Bot em uma thread separada para não bloquear a thread principal
    # que será usada pelo Flask.
    threading.Thread(target=start_polling, daemon=True).start()
    
    # 2. Inicia o Flask na thread principal para satisfazer o Render.
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)
