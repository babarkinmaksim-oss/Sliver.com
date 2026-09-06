"""
Доска объявлений — backend на FastAPI, всё в одном файле.

Стек:
- FastAPI (веб-сервер и API)
- Turso / libsql (база данных постов и реакций)
- Cloudflare R2 через boto3 (хранилище фото/видео, S3-совместимое)

Переменные окружения (задаются в Render → Environment):
    ADMIN_PASSWORD          пароль для входа в админку
    TURSO_DATABASE_URL      напр. libsql://board-db-xxx.turso.io
    TURSO_AUTH_TOKEN        токен доступа к Turso
    R2_ACCOUNT_ID           Account ID в Cloudflare
    R2_ACCESS_KEY_ID        ключ доступа R2
    R2_SECRET_ACCESS_KEY    секретный ключ R2
    R2_BUCKET_NAME          имя бакета, напр. board-media
    R2_PUBLIC_URL           публичный URL бакета, напр. https://pub-xxxx.r2.dev
    PORT                    порт (Render подставляет сам)
"""

import os
import time
import uuid
import mimetypes
from typing import Optional

import boto3
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

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 МБ

# ---------------------------------------------------------------------------
# Клиенты: база данных (Turso) и файловое хранилище (Cloudflare R2)
# ---------------------------------------------------------------------------

db = libsql_client.create_client_sync(
    url=TURSO_DATABASE_URL,
    auth_token=TURSO_AUTH_TOKEN,
)

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
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


def upload_to_r2(content: bytes, content_type: str, original_name: str) -> str:
    ext = (original_name.rsplit(".", 1)[-1] if "." in original_name else "bin").lower()
    key = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"
    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=content,
        ContentType=content_type or "application/octet-stream",
    )
    return f"{R2_PUBLIC_URL}/{key}"


def delete_from_r2(url: Optional[str]):
    if not url or not url.startswith(R2_PUBLIC_URL):
        return
    key = url[len(R2_PUBLIC_URL) + 1:]
    try:
        s3.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
    except Exception as e:
        print(f"Не удалось удалить файл из R2: {e}")


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
        media_url = upload_to_r2(content, guessed_type, media.filename or "file")

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
        delete_from_r2(result.rows[0][0])

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
# Отдача фронтенда (папка public/) — сначала API-роуты выше, потом статика
# ---------------------------------------------------------------------------

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")


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
