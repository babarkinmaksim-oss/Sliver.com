"""
Доска объявлений — backend на FastAPI, всё в одном файле.

Стек:
- FastAPI (веб-сервер и API)
- Turso / libsql (база данных постов и реакций)
- Локальное хранилище файлов (папка uploads/ на диске сервера)

⚠️ На бесплатном тарифе Render диск не постоянный: файлы могут пропасть
при перезапуске/передеплое сервиса. Посты и реакции (в Turso) при этом
не пострадают — пропадут только сами картинки/видео. Если это станет
проблемой, можно подключить внешнее хранилище (Cloudflare R2, Backblaze B2 и т.п.)

Переменные окружения (задаются в Render → Environment):
    ADMIN_PASSWORD          пароль для входа в админку
    TURSO_DATABASE_URL      напр. libsql://board-db-xxx.turso.io
    TURSO_AUTH_TOKEN        токен доступа к Turso
    PORT                    порт (Render подставляет сам)
"""

import os
import time
import uuid
import mimetypes
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import libsql_client

# ---------------------------------------------------------------------------
# Настройка окружения
# ---------------------------------------------------------------------------

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 МБ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Клиент базы данных (Turso)
# ---------------------------------------------------------------------------

db = libsql_client.create_client_sync(
    url=TURSO_DATABASE_URL,
    auth_token=TURSO_AUTH_TOKEN,
)


def init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            text TEXT,
            media_type TEXT,
            media_url TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS reactions (
            post_id TEXT NOT NULL,
            visitor_id TEXT NOT NULL,
            emoji TEXT NOT NULL,
            PRIMARY KEY (post_id, visitor_id, emoji)
        )
    """)


def save_file_locally(content: bytes, original_name: str) -> str:
    """Сохраняет файл в папку uploads/ и возвращает относительный URL для доступа."""
    ext = (original_name.rsplit(".", 1)[-1] if "." in original_name else "bin").lower()
    filename = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(content)
    return f"/uploads/{filename}"


def delete_file_locally(url: Optional[str]):
    if not url or not url.startswith("/uploads/"):
        return
    filename = url[len("/uploads/"):]
    filepath = os.path.join(UPLOADS_DIR, filename)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        print(f"Не удалось удалить файл: {e}")


# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginBody(BaseModel):
    password: str


class ReactBody(BaseModel):
    emoji: str
    visitorId: str


def require_admin(x_admin_password: Optional[str] = Header(None)):
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверный пароль")


@app.get("/api/ping")
def ping():
    return {"ok": True, "time": int(time.time() * 1000)}


@app.post("/api/admin/login")
def admin_login(body: LoginBody):
    if body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверный пароль")
    return {"ok": True}


@app.get("/api/posts")
def get_posts(x_visitor_id: Optional[str] = Header(None)):
    visitor_id = x_visitor_id or ""

    posts_result = db.execute("SELECT * FROM posts ORDER BY created_at DESC")
    reactions_result = db.execute("SELECT * FROM reactions")

    reaction_counts: dict[str, dict[str, int]] = {}
    my_reactions: dict[str, list[str]] = {}

    for row in reactions_result.rows:
        post_id, r_visitor_id, emoji = row[0], row[1], row[2]
        reaction_counts.setdefault(post_id, {})
        reaction_counts[post_id][emoji] = reaction_counts[post_id].get(emoji, 0) + 1
        if r_visitor_id == visitor_id:
            my_reactions.setdefault(post_id, [])
            my_reactions[post_id].append(emoji)

    posts = []
    for row in posts_result.rows:
        post_id, text, media_type, media_url, created_at = row[0], row[1], row[2], row[3], row[4]
        posts.append({
            "id": post_id,
            "text": text,
            "media": {"type": media_type, "url": media_url} if media_url else None,
            "createdAt": created_at,
            "reactions": reaction_counts.get(post_id, {}),
        })

    return {"posts": posts, "myReactions": my_reactions}


@app.post("/api/posts")
async def create_post(
    text: str = Form(""),
    media: Optional[UploadFile] = File(None),
    x_admin_password: Optional[str] = Header(None),
):
    require_admin(x_admin_password)

    media_type = None
    media_url = None

    if media is not None:
        content = await media.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="Файл слишком большой (макс. 100 МБ)")

        guessed_type = media.content_type or mimetypes.guess_type(media.filename or "")[0] or ""
        media_type = "video" if guessed_type.startswith("video") else "image"
        media_url = save_file_locally(content, media.filename or "file")

    if not text and not media_url:
        raise HTTPException(status_code=400, detail="Нужен текст или медиа")

    post_id = uuid.uuid4().hex[:12]
    created_at = int(time.time() * 1000)

    db.execute(
        "INSERT INTO posts (id, text, media_type, media_url, created_at) VALUES (?, ?, ?, ?, ?)",
        [post_id, text, media_type, media_url, created_at],
    )

    return {
        "id": post_id,
        "text": text,
        "media": {"type": media_type, "url": media_url} if media_url else None,
        "createdAt": created_at,
        "reactions": {},
    }


@app.delete("/api/posts/{post_id}")
def delete_post(post_id: str, x_admin_password: Optional[str] = Header(None)):
    require_admin(x_admin_password)

    result = db.execute("SELECT media_url FROM posts WHERE id = ?", [post_id])
    if result.rows:
        delete_file_locally(result.rows[0][0])

    db.execute("DELETE FROM posts WHERE id = ?", [post_id])
    db.execute("DELETE FROM reactions WHERE post_id = ?", [post_id])

    return {"ok": True}


@app.post("/api/posts/{post_id}/react")
def react_to_post(post_id: str, body: ReactBody):
    if not body.emoji or not body.visitorId:
        raise HTTPException(status_code=400, detail="Нужны emoji и visitorId")

    existing = db.execute(
        "SELECT 1 FROM reactions WHERE post_id = ? AND visitor_id = ? AND emoji = ?",
        [post_id, body.visitorId, body.emoji],
    )

    if existing.rows:
        db.execute(
            "DELETE FROM reactions WHERE post_id = ? AND visitor_id = ? AND emoji = ?",
            [post_id, body.visitorId, body.emoji],
        )
        return {"ok": True, "action": "removed"}
    else:
        db.execute(
            "INSERT INTO reactions (post_id, visitor_id, emoji) VALUES (?, ?, ?)",
            [post_id, body.visitorId, body.emoji],
        )
        return {"ok": True, "action": "added"}


# ---------------------------------------------------------------------------
# Отдача загруженных файлов и фронтенда
# ---------------------------------------------------------------------------

PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(PUBLIC_DIR, "index.html"))


app.mount("/", StaticFiles(directory=PUBLIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
