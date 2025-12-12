import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import re
import unicodedata

# Inicializa Firebase
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "filme"

def save_movie(movie: dict):
    """
    movie = {
      "title": str,
      "year": int | None,
      "synopsis": str,
      "tags": [str],
      "videoUrl": str,
      "posterUrl": str | None
    }
    """
    title = movie.get("title") or "Sem título"
    year = movie.get("year")
    doc_id = slugify(f"{title}-{year}" if year else title)

    movie["createdAt"] = datetime.utcnow()

    db.collection("movies").document(doc_id).set(movie, merge=True)
    print(f"[Firestore] Filme salvo: {title} ({year})")
