import os
import time
import tempfile
import urllib.parse
import asyncio 
import json

from flask import Flask # Mantido para cumprir a lista de dependências, mas não usado.
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
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") 

if not all([BOT_TOKEN, FIREBASE_DB_URL, FIREBASE_STORAGE_BUCKET, ALLOWED_CHAT_ID, WEBHOOK_URL]):
    # Não levanta erro, apenas imprime, pois o Polling não depende de todas as variáveis de Webhook.
    print("AVISO: Variáveis de ambiente incompletas (RENDER_EXTERNAL_URL não é necessário para Polling).")

# Inicialização ÚNICA do Firebase
if not firebase_admin._apps:
    try:
        # Certifique-se de que o firebase-key.json está na raiz do projeto
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
movies_ref = db.reference("movies") 

# ======================================================
# FLASK (Mantido APENAS para o Health Check do Render)
# Nota: Esta rota é inútil no Polling, mas o Render pode esperá-la.
# O comando de start será 'python telegram_bot.py' e ignorará esta seção.
# ======================================================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "🤖 Bot online (Polling mode)", 200

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
    chat_id_atual = str(update.effective_chat.id)
    print(f"DEBUG: Tentativa de chat ID: {chat_id_atual}")
    if chat_id_atual == str(ALLOWED_CHAT_ID):
        return True
    else:
        print(f"AVISO: Chat ID {chat_id_atual} BLOQUEADO. Esperado: {ALLOWED_CHAT_ID}")
        return False

def parse_metadata(text: str):
    def get(label):
        for line in text.splitlines():
            if label.lower() in line.lower():
                return line.split(":", 1)[-1].strip()
        return None
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


# ======================================================
# HANDLERS (Assíncronos - PTB 20.3)
# ======================================================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update): return
    chat_id = update.effective_chat.id
    text = update.message.caption 
    photo = update.message.photo[-1] if update.message.photo else None
    document_image = update.message.document if update.message.document and update.message.document.mime_type.startswith('image') else None
    if not photo and not document_image: return 
    if "título" not in text.lower(): return
    poster_file_id = photo.file_id if photo else (document_image.file_id if document_image else None)
    if not poster_file_id:
        await update.message.reply_text("⚠️ Falha ao obter o ID da imagem. Tente enviar a imagem diretamente.")
        return
    metadata = parse_metadata(text)
    pending_movies[chat_id] = {"poster_file_id": poster_file_id, "metadata": metadata, "created_at": time.time()}
    await update.message.reply_text("✅ Capa e Metadados recebidos. Agora envie o **VÍDEO** do filme.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_chat(update): return
    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)
    if not pending or "metadata" not in pending:
        await update.message.reply_text("⚠️ Ordem incorreta. Envie: **Capa + Texto** primeiro → **Vídeo**.")
        return
    file = update.message.video or update.message.document 
    if not file or (update.message.document and not update.message.document.mime_type.startswith('video')):
        await update.message.reply_text("⚠️ Mensagem não contém um arquivo de vídeo válido.")
        return
    file_id = file.file_id
    await update.message.reply_text("📥 Salvando no Firebase... (Isto pode levar tempo)")
    movie_ref = movies_ref.push()
    movie_id = movie_ref.key
    
    # --- UPLOAD POSTER ---
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
    
    # --- UPLOAD VIDEO ---
    video_url = ""
    try:
        video_file = await context.bot.get_file(file_id)
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
    movie_ref.set({**data, "posterUrl": poster_url, "videoUrl": video_url, "createdAt": int(time.time() * 1000)})
    pending_movies.pop(chat_id, None)
    await update.message.reply_text("✅ Filme salvo no Firebase!")
# ======================================================


# ======================================================
# INICIALIZAÇÃO E EXECUÇÃO (MODO POLLING)
# ======================================================

# 1. Constrói a Aplicação
application = ApplicationBuilder().token(BOT_TOKEN).build()
application.add_handler(MessageHandler(filters.Caption, handle_photo)) 
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))


def start_polling():
    """Inicia o bot em modo Polling."""
    print("🤖 Iniciando Bot em modo Polling...")
    # Limpa qualquer Webhook pendente
    asyncio.run(application.bot.delete_webhook()) 
    # Inicia o loop de polling, evitando qualquer conflito de servidor/asyncio.
    application.run_polling()
    
# Executa a função de inicialização
if __name__ == "__main__":
    start_polling()

# ======================================================
# FIM
# ======================================================
