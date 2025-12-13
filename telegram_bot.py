import os
import time
import tempfile
import urllib.parse
import asyncio 
import json # Adicionado para desserialização manual

from flask import Flask, request
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
    raise RuntimeError("Variáveis de ambiente incompletas. Verifique BOT_TOKEN, FIREBASE_DB_URL, FIREBASE_STORAGE_BUCKET, TELEGRAM_GROUP_ID e RENDER_EXTERNAL_URL.")

# Inicialização ÚNICA do Firebase
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
# FLASK (Ponto de entrada do Gunicorn e Keep-Alive)
# ======================================================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "🤖 Bot online (Webhook mode)", 200

# ======================================================
# MEMÓRIA TEMPORÁRIA
# ======================================================
pending_movies = {} 

# ======================================================
# HELPERS (Inalterados)
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
# HANDLERS (Mantenha inalterados)
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
# INICIALIZAÇÃO DE APLICAÇÃO PTB (GLOBAL) - Revertida para simplificação
# ======================================================

application = ApplicationBuilder().token(BOT_TOKEN).build()
application.add_handler(MessageHandler(filters.Caption, handle_photo)) 
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))


# ======================================================
# WEBSERVICE HANDLER (POST) - CORREÇÃO MANUAL DE DESSERIALIZAÇÃO
# ======================================================

@app_flask.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Recebe o Update do Telegram, desserializa manualmente e processa."""
    try:
        update_data = request.get_data()
        
        if not update_data:
            return "OK", 200

        # Desserializa os dados manualmente.
        # update_dict = json.loads(update_data) # Desnecessário, mas o .get_data() é suficiente
        
        # Cria um novo Event Loop e o seta para esta requisição
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 🚨 CORREÇÃO CHAVE: Usamos o método handle_update que é recomendado para Webhook PTB 20+.
        # Ele aceita o JSON do Telegram e cuida da desserialização e inicialização da Application.
        loop.run_until_complete(
            application.update_queue.put(update_data)
        )
        
        # Como estamos rodando em Gunicorn, não usamos application.process_update(update_data)
        # nem application.run_polling/run_webhook. A melhor forma é usar a queue.
        # No entanto, a queue só é processada pelo Updater/Runner.

        # Como estamos forçando o processamento dentro de um worker Gunicorn, 
        # a sintaxe mais estável é a anterior, mas vamos tentar a queue com a nova estrutura.
        # ---
        # VOLTANDO AO PADRÃO PTB P/ FLASK APÓS MAIS TESTES EM AMBIENTES SEMELHANTES:

        # 1. Desserialização manual (mais seguro)
        update_json = request.json
        if update_json is None:
            return "OK", 200

        # 2. Cria o objeto Update
        update = Update.de_json(update_json, application.bot)

        # 3. Processa no loop
        loop.run_until_complete(
            application.process_update(update)
        )

        return "OK", 200

    except Exception as e:
        print(f"❌ Erro ao processar webhook: {e}")
        return "Internal Server Error", 500


# ======================================================
# CONFIGURAÇÃO DE WEBSERVICE (Startup)
# ======================================================

def setup_webhook():
    """Configura o Webhook no Telegram na inicialização."""
    try:
        full_webhook_url = f"{WEBHOOK_URL}/telegram-webhook"
        print(f"🔗 Tentando configurar Webhook para: {full_webhook_url}")
        
        async def set_hook():
            # Usa a Application GLOBAL aqui
            await application.bot.set_webhook(url=full_webhook_url, drop_pending_updates=True)
            print("✅ Webhook configurado com sucesso. Bot está pronto!")
        
        loop = asyncio.new_event_loop()
        loop.run_until_complete(set_hook())

    except Exception as e:
        print(f"❌ ERRO CRÍTICO no setup do Webhook: {e}. Verifique o BOT_TOKEN e RENDER_EXTERNAL_URL.")

# Executa o setup do webhook na inicialização do módulo (Gunicorn)
print("🤖 Iniciando Bot em modo Webhook...")
setup_webhook()
