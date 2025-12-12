import os
import re
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

import firebase_admin
from firebase_admin import credentials, firestore, storage

# ============== CONFIGURAÇÕES ==============

# SEU TOKEN AQUI
BOT_TOKEN = "7588918300:AAHN3EOIvGIrvifXO-Qj-BFWPOY9FeJEPkA"

# Caminho do JSON da Service Account
SERVICE_ACCOUNT_PATH = "serviceAccountKey.json"

# NOME DO SEU BUCKET DO STORAGE (ex: meus-filmes-drive.appspot.com)
STORAGE_BUCKET = "SEU_ID_DO_PROJETO.appspot.com"

# ===========================================

# Inicializa Firebase (Firestore e Storage)
try:
    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_app = firebase_admin.initialize_app(cred, {
        'storageBucket': STORAGE_BUCKET
    })
    db = firestore.client()
    bucket = storage.bucket()
    print("✅ Firebase (Firestore e Storage) inicializado com sucesso!")
except Exception as e:
    print(f"❌ Erro ao inicializar o Firebase: {e}")
    exit()

# Expressão regular (Regex) para o NOVO FORMATO (mais robusto)
REGEX_FILME = re.compile(
    r"Título:\s*(?P<titulo>.+?)\n"
    r"Ano:\s*(?P<ano>\d{4})\n"
    # Usando Classificação no lugar de Áudio
    r"Classificação:\s*(?P<classificacao>.+?)\n"
    r"Gêneros:\s*(?P<genero>.+?)\n"
    r"Sinopse:\s*(?P<sinopse>[\s\S]+)",
    re.IGNORECASE
)

# --- FUNÇÕES DE UPLOAD PARA O FIREBASE STORAGE ---


async def upload_telegram_file_to_firebase(file_id: str, destination_path: str, mime_type: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Faz download de um arquivo do Telegram e faz upload para o Firebase Storage."""

    telegram_file = await context.bot.get_file(file_id)
    file_bytes = await telegram_file.download_as_bytes()

    blob = bucket.blob(destination_path)

    # Faz o upload dos bytes
    blob.upload_from_string(
        data=file_bytes,
        content_type=mime_type
    )

    # Retorna o caminho no formato gs:// para salvar no Firestore
    return f"gs://{bucket.name}/{destination_path}"


# --- HANDLER PRINCIPAL (Agora mais robusto) ---

async def handle_video_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id

    # Precisa de vídeo E texto (para os metadados) E foto (para a capa)
    if not (message and message.caption and message.video and message.photo):
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Mensagem ignorada. Precisa de VÍDEO, FOTO DE CAPA e LEGENDA COMPLETA.")
        return

    # 1. Extrair Metadados usando Regex
    match = REGEX_FILME.search(message.caption)
    if not match:
        await context.bot.send_message(chat_id=chat_id, text="❌ Formato de metadados incorreto. Use: Título:, Ano:, Classificação:, Gêneros:, Sinopse:")
        return

    metadados = match.groupdict()
    titulo_formatado = metadados['titulo'].strip().replace(" ", "_").lower()

    await context.bot.send_message(chat_id=chat_id, text=f"🔍 Metadados de '{metadados['titulo']}' detectados. Iniciando upload...")

    try:
        # 2. Upload da Capa (Foto)
        capa_file_id = message.photo[-1].file_id  # A foto de maior resolução
        capa_path_storage = f"capas/{titulo_formatado}_{message.photo[-1].file_unique_id}.jpg"
        capa_url = await upload_telegram_file_to_firebase(
            capa_file_id, capa_path_storage, 'image/jpeg', context
        )
        await context.bot.send_message(chat_id=chat_id, text="🖼️ Capa enviada para o Storage.")

        # 3. Upload do Vídeo
        video_file_id = message.video.file_id
        video_mime_type = message.video.mime_type or "video/mp4"
        extension = os.path.splitext(message.video.file_name or "video.mp4")[1]

        video_path_storage = f"videos/{titulo_formatado}_{message.video.file_unique_id}{extension}"
        video_url = await upload_telegram_file_to_firebase(
            video_file_id, video_path_storage, video_mime_type, context
        )
        await context.bot.send_message(chat_id=chat_id, text="🎥 Vídeo enviado para o Storage.")

        # 4. Salvar no Firestore
        filme_data = {
            'titulo': metadados['titulo'].strip(),
            'ano': metadados['ano'].strip(),
            'classificacao': metadados['classificacao'].strip(),
            'genero': metadados['genero'].strip(),
            'sinopse': metadados['sinopse'].strip(),
            'capaUrl': capa_url,
            'videoUrl': video_url,
            'timestamp': firestore.SERVER_TIMESTAMP,
        }

        doc_ref = db.collection('filmes').add(filme_data)

        await context.bot.send_message(chat_id=chat_id, text=f"🎉 Filme **'{metadados['titulo'].strip()}'** catalogado no **Firestore** com sucesso! (ID: {doc_ref[1].id})")

    except Exception as e:
        logger.exception("Erro ao processar filme no Firebase")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Erro final ao salvar: {e}")


def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Filtra mensagens que contêm VÍDEO e TEXTO (LEGENDAS)
    # E esperamos que contenham uma foto para a capa
    application.add_handler(MessageHandler(
        filters.VIDEO & filters.CAPTION, handle_video_post))

    print("Bot rodando... Aguardando postagens de filme no grupo.")
    application.run_polling()


if __name__ == "__main__":
    main()
