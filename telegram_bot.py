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
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID") 

# Validação de variáveis de ambiente (Usa a validação mais completa)
if not all([BOT_TOKEN, FIREBASE_DB_URL, FIREBASE_STORAGE_BUCKET, ALLOWED_CHAT_ID]):
    raise RuntimeError("Variáveis de ambiente incompletas. Verifique BOT_TOKEN, FIREBASE_DB_URL, FIREBASE_STORAGE_BUCKET e TELEGRAM_GROUP_ID.")

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
pending_movies = {} 

# ======================================================
# HELPERS
# ======================================================
def build_download_url(blob):
    """Gera uma URL de acesso público para o arquivo no Firebase Storage."""
    path = urllib.parse.quote(blob.name, safe="")
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{path}?alt=media"


def check_chat(update: Update) -> bool:
    """Verifica se a mensagem vem do grupo permitido e imprime DEBUG."""
    chat_id_atual = str(update.effective_chat.id)
    
    # 🚨 DEBUG: Imprime o ID atual no log do Render
    print(f"DEBUG: Tentativa de chat ID: {chat_id_atual}")
    
    if chat_id_atual == str(ALLOWED_CHAT_ID):
        return True
    else:
        print(f"AVISO: Chat ID {chat_id_atual} BLOQUEADO. Esperado: {ALLOWED_CHAT_ID}")
        return False


def parse_metadata(text: str):
    """Extrai campos específicos do texto formatado do filme."""
    def get(label):
        for line in text.splitlines():
            if label.lower() in line.lower():
                # Retorna o texto após os dois pontos
                return line.split(":", 1)[-1].strip()
        return None

    # Tenta extrair a sinopse usando o separador "Sinopse:"
    synopsis = text.split("Sinopse:", 1)[-1].strip() if "Sinopse:" in text else None
    
    return {
        "title": get("Título") or "Sem título",
        "synopsis": synopsis,
        "director": get("Diretor"),
        "audio": get("Áudio"),
        "year": get("Lançamento"),
        "genres": get("Gêneros"),
    }

# ======================================================
# HANDLERS (LÓGICA AUTOMÁTICA)
# ======================================================

# Handler 1: Processa a imagem (foto ou documento) e a legenda (metadata)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update):
        return

    chat_id = update.effective_chat.id
    text = update.message.caption 

    # Tenta obter a foto de maior resolução ou o documento se for imagem
    photo = update.message.photo[-1] if update.message.photo else None
    document_image = update.message.document if update.message.document and update.message.document.mime_type.startswith('image') else None
    
    # Se não houver nenhum tipo de imagem (e sim apenas texto na legenda, por ex.), o bot para aqui
    if not photo and not document_image:
        return 

    # 🚨 REGRA DE NEGÓCIO: A legenda deve conter "Título" para ser metadata válida.
    if "título" not in text.lower():
        # Ignora, pois a mensagem não é um filme
        return

    # Tenta obter o file_id da imagem
    poster_file_id = photo.file_id if photo else (document_image.file_id if document_image else None)
    
    if not poster_file_id:
        await update.message.reply_text("⚠️ Falha ao obter o ID da imagem. Tente enviar a imagem diretamente.")
        return

    # Processa e armazena os metadados
    metadata = parse_metadata(text)

    pending_movies[chat_id] = {
        "poster_file_id": poster_file_id,
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

    # Verifica se a metadata já foi enviada (Etapa 1)
    if not pending or "metadata" not in pending:
        await update.message.reply_text(
            "⚠️ Ordem incorreta. Envie: **Capa + Texto** primeiro → **Vídeo**."
        )
        return

    file = update.message.video or update.message.document 
    file_id = file.file_id

    await update.message.reply_text("📥 Salvando no Firebase... (Isto pode levar tempo)")

    # 1. ID do filme no Realtime Database
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
        print(f"❌ Erro ao salvar poster no Storage: {e}")
        await update.message.reply_text("❌ Falha crítica ao salvar a capa.")
        pending_movies.pop(chat_id, None) 
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
        print(f"❌ Erro ao salvar vídeo no Storage: {e}")
        await update.message.reply_text("❌ Falha crítica ao salvar o vídeo.")
        pending_movies.pop(chat_id, None)
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
# BOT STARTER (Estabilidade no Render)
# ======================================================
def start_polling():
    """Configura e inicia o bot PTB em polling na thread separada."""
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handler 1: Filtro Relaxado: Aceita QUALQUER MENSAGEM com Legenda
    app.add_handler(
        MessageHandler(filters.ALL & filters.Caption, handle_photo) 
    )
    
    # Handler 2: Processa o Vídeo
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )

    print("🤖 Bot Telegram iniciando...")
    
    # run_polling é síncrono e BLOQUEIA esta thread.
    app.run_polling(drop_pending_updates=True, stop_signals=None) 

# ======================================================
# MAIN
# ======================================================
if __name__ == "__main__":
    # 1. Inicia o Bot em uma thread separada
    threading.Thread(target=start_polling, daemon=True).start()
    
    # 2. Inicia o Flask na thread principal para satisfazer o Render.
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)
