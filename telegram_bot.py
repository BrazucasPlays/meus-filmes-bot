import os
import time
import tempfile
import urllib.parse
import json

from flask import Flask, request
from dotenv import load_dotenv

from telegram.ext import (
    Updater, 
    MessageHandler, 
    CallbackContext, 
    filters
)
from telegram import Update, File, Bot

import firebase_admin
from firebase_admin import credentials, db, storage

# ======================================================
# ENV & INIT
# ======================================================
load_dotenv()

# ... (Variáveis de ambiente iguais) ...
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID") 
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") 
# ... (Verificação de variáveis igual) ...

# Inicialização ÚNICA do Firebase
# ... (Bloco de inicialização do Firebase igual) ...
if not firebase_admin._apps:
    try:
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
# FLASK & MEMÓRIA TEMPORÁRIA
# ======================================================
app_flask = Flask(__name__)
pending_movies = {} 

@app_flask.route("/")
def home():
    return "🤖 Bot online (Webhook mode)", 200

# ======================================================
# HELPERS (Síncronos)
# ======================================================
# ... (build_download_url, check_chat, parse_metadata iguais) ...
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
# HANDLERS (Síncronos - PTB 13.x)
# ======================================================
# Nota: get_file.download_to_drive é SÍNCRONO no PTB 13.x
def handle_photo(update: Update, context: CallbackContext):
    if not check_chat(update): return
    chat_id = update.effective_chat.id
    text = update.message.caption 
    photo = update.message.photo[-1] if update.message.photo else None
    document_image = update.message.document if update.message.document and update.message.document.mime_type.startswith('image') else None
    if not photo and not document_image: return 
    if "título" not in text.lower(): return
    poster_file_id = photo.file_id if photo else (document_image.file_id if document_image else None)
    if not poster_file_id:
        update.message.reply_text("⚠️ Falha ao obter o ID da imagem. Tente enviar a imagem diretamente.")
        return
    metadata = parse_metadata(text)
    pending_movies[chat_id] = {"poster_file_id": poster_file_id, "metadata": metadata, "created_at": time.time()}
    update.message.reply_text("✅ Capa e Metadados recebidos. Agora envie o **VÍDEO** do filme.")


def handle_video(update: Update, context: CallbackContext):
    if not check_chat(update): return
    chat_id = update.effective_chat.id
    pending = pending_movies.get(chat_id)
    if not pending or "metadata" not in pending:
        update.message.reply_text("⚠️ Ordem incorreta. Envie: **Capa + Texto** primeiro → **Vídeo**.")
        return
    file = update.message.video or update.message.document 
    if not file or (update.message.document and not update.message.document.mime_type.startswith('video')):
        update.message.reply_text("⚠️ Mensagem não contém um arquivo de vídeo válido.")
        return
    
    file_id = file.file_id
    update.message.reply_text("📥 Salvando no Firebase... (Isto pode levar tempo)")
    movie_ref = movies_ref.push()
    movie_id = movie_ref.key

    # --- UPLOAD POSTER ---
    poster_url = ""
    try:
        # get_file é síncrono no PTB 13.x
        poster_file: File = context.bot.get_file(pending["poster_file_id"]) 
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            poster_file.download(custom_path=tmp.name) # Síncrono
            poster_blob = bucket.blob(f"movies/{movie_id}/poster.jpg") 
            poster_blob.upload_from_filename(tmp.name)
            poster_url = build_download_url(poster_blob)
    except Exception as e:
        print(f"❌ Erro ao salvar poster no Storage: {e}")
        update.message.reply_text("❌ Falha crítica ao salvar a capa.")
        pending_movies.pop(chat_id, None) 
        return

    # --- UPLOAD VIDEO ---
    video_url = ""
    try:
        video_file: File = context.bot.get_file(file_id)
        ext = "." + file.file_name.split(".")[-1] if file.file_name and "." in file.file_name else ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            video_file.download(custom_path=tmp.name) # Síncrono
            video_blob = bucket.blob(f"movies/{movie_id}/video{ext}") 
            video_blob.upload_from_filename(tmp.name)
            video_url = build_download_url(video_blob)
    except Exception as e:
        print(f"❌ Erro ao salvar vídeo no Storage: {e}")
        update.message.reply_text("❌ Falha crítica ao salvar o vídeo.")
        pending_movies.pop(chat_id, None)
        return

    # SALVAR NO REALTIME DATABASE
    data = pending["metadata"]
    movie_ref.set({**data, "posterUrl": poster_url, "videoUrl": video_url, "createdAt": int(time.time() * 1000)})
    pending_movies.pop(chat_id, None)
    update.message.reply_text("✅ Filme salvo no Firebase!")
# ======================================================


# ======================================================
# INICIALIZAÇÃO E DISPATCHER (PTB 13.x)
# ======================================================

# No PTB 13.x, usamos Bot e Dispatcher (síncronos)
bot = Bot(token=BOT_TOKEN)
dispatcher = Updater(bot=bot).dispatcher

dispatcher.add_handler(MessageHandler(filters.caption & ~filters.command, handle_photo)) 
dispatcher.add_handler(MessageHandler(filters.video | filters.document.video, handle_video))


# ======================================================
# WEBSERVICE HANDLER (POST) - SIMPLES E SÍNCRONO
# ======================================================

@app_flask.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Recebe o update e o passa diretamente para o dispatcher."""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        
        # 🚨 CORREÇÃO CRÍTICA: Processa o update usando o dispatcher síncrono.
        # Isso evita qualquer problema de Application/asyncio.
        dispatcher.process_update(update)
        
        return "OK", 200
    return "Method Not Allowed", 405


# ======================================================
# CONFIGURAÇÃO DE WEBSERVICE (Startup) - SÍNCRONA
# ======================================================

def setup_webhook():
    """Configura o Webhook no Telegram na inicialização (Síncrono)."""
    try:
        full_webhook_url = f"{WEBHOOK_URL}/telegram-webhook"
        print(f"🔗 Tentando configurar Webhook para: {full_webhook_url}")
        
        # No PTB 13.x, set_webhook é uma chamada de API síncrona
        bot.set_webhook(url=full_webhook_url, drop_pending_updates=True)
        
        print("✅ Webhook configurado com sucesso. Bot está pronto!")

    except Exception as e:
        print(f"❌ ERRO CRÍTICO no setup do Webhook: {e}. Verifique o BOT_TOKEN e RENDER_EXTERNAL_URL.")

# Executa o setup do webhook na inicialização do módulo
print("🤖 Iniciando Bot em modo Webhook...")
setup_webhook()
