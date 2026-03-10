import os
import uuid
import json
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Set

import bcrypt
import jwt
import aiosqlite
from fastapi import (
    FastAPI, HTTPException, UploadFile, File,
    WebSocket, WebSocketDisconnect, Form, Request
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
INSTRUCTOR_CODE = os.getenv("INSTRUCTOR_CODE", "instructor2024")
UPLOAD_DIR = Path("uploads")
DB_PATH = os.getenv("DB_PATH", "social_reader.db")

UPLOAD_DIR.mkdir(exist_ok=True)

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ─── WebSocket Manager ────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[int, Set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, article_id: int):
        await ws.accept()
        self.rooms.setdefault(article_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, article_id: int):
        self.rooms.get(article_id, set()).discard(ws)

    async def broadcast(self, message: dict, article_id: int, exclude: WebSocket = None):
        room = self.rooms.get(article_id, set())
        dead = set()
        for ws in room:
            if ws is exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        room -= dead


manager = ConnectionManager()


# ─── Database ─────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                uploaded_by INTEGER REFERENCES users(id),
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS annotations (
                id TEXT PRIMARY KEY,
                article_id INTEGER REFERENCES articles(id),
                user_id INTEGER REFERENCES users(id),
                type TEXT NOT NULL,
                page INTEGER NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Social Reader", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Auth Utilities ───────────────────────────────────────────────────────────

def create_token(user_id: int, username: str, display_name: str, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "display_name": display_name,
        "role": role,
        "exp": datetime.utcnow() + timedelta(days=14),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_token(auth[7:])


async def require_instructor(request: Request) -> dict:
    user = await get_current_user(request)
    if user["role"] != "instructor":
        raise HTTPException(status_code=403, detail="Instructor only")
    return user


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    display_name: str
    password: str
    instructor_code: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class AnnotationCreate(BaseModel):
    article_id: int
    type: str  # highlight | underline | note
    page: int
    data: dict


# ─── Auth Routes ──────────────────────────────────────────────────────────────

@app.post("/api/register")
async def register(req: RegisterRequest):
    role = "student"
    if req.instructor_code:
        if req.instructor_code != INSTRUCTOR_CODE:
            raise HTTPException(400, "Invalid instructor code")
        role = "instructor"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute(
                "INSERT INTO users (username, display_name, password_hash, role) VALUES (?, ?, ?, ?)",
                (req.username.strip().lower(), req.display_name.strip(), hash_password(req.password), role),
            )
            await db.commit()
            user_id = cur.lastrowid
        except aiosqlite.IntegrityError:
            raise HTTPException(400, "Username already taken")

    token = create_token(user_id, req.username.strip().lower(), req.display_name.strip(), role)
    return {
        "token": token,
        "user": {"id": user_id, "username": req.username, "display_name": req.display_name.strip(), "role": role},
    }


@app.post("/api/login")
async def login(req: LoginRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (req.username.strip().lower(),))
        row = await cursor.fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        user = dict(row)

    token = create_token(user["id"], user["username"], user["display_name"], user["role"])
    return {
        "token": token,
        "user": {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"]},
    }


@app.get("/api/me")
async def me(request: Request):
    return await get_current_user(request)


# ─── Article Routes ───────────────────────────────────────────────────────────

@app.get("/api/articles")
async def list_articles(request: Request):
    await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT a.id, a.title, a.created_at, u.display_name AS uploaded_by_name
            FROM articles a JOIN users u ON a.uploaded_by = u.id
            ORDER BY a.created_at DESC
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


@app.post("/api/articles")
async def upload_article(request: Request, title: str = Form(...), file: UploadFile = File(...)):
    user = await require_instructor(request)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are allowed")

    filename = f"{uuid.uuid4()}.pdf"
    file_path = UPLOAD_DIR / filename
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO articles (title, filename, uploaded_by) VALUES (?, ?, ?)",
            (title.strip(), filename, int(user["sub"])),
        )
        await db.commit()
        article_id = cur.lastrowid

    return {"id": article_id, "title": title.strip(), "filename": filename}


@app.delete("/api/articles/{article_id}")
async def delete_article(article_id: int, request: Request):
    await require_instructor(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT filename FROM articles WHERE id = ?", (article_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        await db.execute("DELETE FROM annotations WHERE article_id = ?", (article_id,))
        await db.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        await db.commit()

    pdf_path = UPLOAD_DIR / row["filename"]
    if pdf_path.exists():
        pdf_path.unlink()
    return {"ok": True}


@app.get("/api/articles/{article_id}/pdf")
async def get_pdf(article_id: int, request: Request):
    await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT filename FROM articles WHERE id = ?", (article_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Article not found")

    pdf_path = UPLOAD_DIR / row["filename"]
    if not pdf_path.exists():
        raise HTTPException(404, "PDF file not found")
    return FileResponse(str(pdf_path), media_type="application/pdf")


# ─── Annotation Routes ────────────────────────────────────────────────────────

@app.get("/api/annotations/{article_id}")
async def get_annotations(article_id: int, request: Request):
    await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT a.id, a.article_id, a.user_id, a.type, a.page, a.data, a.created_at,
                   u.display_name, u.username
            FROM annotations a JOIN users u ON a.user_id = u.id
            WHERE a.article_id = ?
            ORDER BY a.page ASC, a.created_at ASC
        """, (article_id,))
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            result.append(d)
        return result


@app.post("/api/annotations")
async def create_annotation(ann: AnnotationCreate, request: Request):
    user = await get_current_user(request)
    ann_id = str(uuid.uuid4())
    user_id = int(user["sub"])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO annotations (id, article_id, user_id, type, page, data) VALUES (?, ?, ?, ?, ?, ?)",
            (ann_id, ann.article_id, user_id, ann.type, ann.page, json.dumps(ann.data)),
        )
        await db.commit()
        cursor = await db.execute("""
            SELECT a.id, a.article_id, a.user_id, a.type, a.page, a.data, a.created_at,
                   u.display_name, u.username
            FROM annotations a JOIN users u ON a.user_id = u.id
            WHERE a.id = ?
        """, (ann_id,))
        row = await cursor.fetchone()
        result = dict(row)
        result["data"] = json.loads(result["data"])

    await manager.broadcast({"event": "annotation_added", "annotation": result}, ann.article_id)
    return result


@app.delete("/api/annotations/{ann_id}")
async def delete_annotation(ann_id: str, request: Request):
    user = await get_current_user(request)
    user_id = int(user["sub"])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT article_id, user_id FROM annotations WHERE id = ?", (ann_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Annotation not found")
        if row["user_id"] != user_id and user["role"] != "instructor":
            raise HTTPException(403, "Not allowed")
        article_id = row["article_id"]
        await db.execute("DELETE FROM annotations WHERE id = ?", (ann_id,))
        await db.commit()

    await manager.broadcast({"event": "annotation_deleted", "annotation_id": ann_id}, article_id)
    return {"ok": True}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/{article_id}")
async def websocket_endpoint(ws: WebSocket, article_id: int, token: str = ""):
    try:
        decode_token(token)
    except HTTPException:
        await ws.close(code=4001)
        return

    await manager.connect(ws, article_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws, article_id)


# ─── Static Files ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/articles")
async def articles_page():
    return FileResponse("static/articles.html")


@app.get("/reader/{article_id}")
async def reader_page(article_id: int):
    return FileResponse("static/reader.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
