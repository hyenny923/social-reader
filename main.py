import os
import uuid
import json
import shutil
import random
import string
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Set

import bcrypt
import jwt
import aiosqlite
try:
    import asyncpg
except ImportError:
    asyncpg = None
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
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")

neon_pool = None

UPLOAD_DIR.mkdir(exist_ok=True)

def generate_join_code() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                join_code TEXT UNIQUE NOT NULL,
                instructor_id INTEGER REFERENCES users(id),
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS class_members (
                class_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (class_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS annotation_comments (
                id TEXT PRIMARY KEY,
                annotation_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migration: add class_id to articles
        try:
            await db.execute("ALTER TABLE articles ADD COLUMN class_id INTEGER")
        except Exception:
            pass
        await db.commit()


async def init_neon():
    global neon_pool
    if not asyncpg or not NEON_DATABASE_URL:
        print("NEON_DATABASE_URL not set or asyncpg unavailable, logging disabled")
        return
    try:
        # Strip sslmode from URL and pass ssl='require' explicitly for asyncpg compatibility
        import re
        clean_url = re.sub(r'[?&]sslmode=\w+', '', NEON_DATABASE_URL)
        neon_pool = await asyncpg.create_pool(clean_url, min_size=1, max_size=5, ssl='require')
        async with neon_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS web_logs (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT,
                    user_id INTEGER,
                    username TEXT,
                    display_name TEXT,
                    event_type TEXT NOT NULL,
                    article_id INTEGER,
                    article_title TEXT,
                    class_id INTEGER,
                    page INTEGER,
                    metadata JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        print("NeonDB connected ✓")
    except Exception as e:
        import traceback
        print(f"NeonDB connection failed (logging disabled): {e}")
        traceback.print_exc()
        neon_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_neon()
    yield
    if neon_pool:
        await neon_pool.close()


app = FastAPI(title="Social Reader", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# static 파일 CDN 캐시 방지
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(NoCacheStaticMiddleware)


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

class CreateClassRequest(BaseModel):
    name: str
    description: Optional[str] = ''

class JoinClassRequest(BaseModel):
    join_code: str

class CreateCommentRequest(BaseModel):
    annotation_id: str
    text: str

class LogEventRequest(BaseModel):
    event_type: str
    session_id: Optional[str] = None
    article_id: Optional[int] = None
    article_title: Optional[str] = None
    class_id: Optional[int] = None
    page: Optional[int] = None
    metadata: Optional[dict] = None


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
async def list_articles(request: Request, class_id: Optional[int] = None):
    await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if class_id is not None:
            cursor = await db.execute("""
                SELECT a.id, a.title, a.created_at, u.display_name AS uploaded_by_name
                FROM articles a JOIN users u ON a.uploaded_by = u.id
                WHERE a.class_id = ?
                ORDER BY a.created_at DESC
            """, (class_id,))
        else:
            cursor = await db.execute("""
                SELECT a.id, a.title, a.created_at, u.display_name AS uploaded_by_name
                FROM articles a JOIN users u ON a.uploaded_by = u.id
                ORDER BY a.created_at DESC
            """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


@app.post("/api/articles")
async def upload_article(
    request: Request,
    title: str = Form(...),
    file: UploadFile = File(...),
    class_id: Optional[int] = Form(None),
):
    user = await require_instructor(request)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are allowed")

    filename = f"{uuid.uuid4()}.pdf"
    file_path = UPLOAD_DIR / filename
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO articles (title, filename, uploaded_by, class_id) VALUES (?, ?, ?, ?)",
            (title.strip(), filename, int(user["sub"]), class_id),
        )
        await db.commit()
        article_id = cur.lastrowid

    return {"id": article_id, "title": title.strip(), "filename": filename, "class_id": class_id}


@app.patch("/api/articles/{article_id}")
async def update_article_title(article_id: int, request: Request):
    await require_instructor(request)
    body = await request.json()
    new_title = body.get("title", "").strip()
    if not new_title:
        raise HTTPException(400, "Title cannot be empty")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id FROM articles WHERE id = ?", (article_id,))
        if not await cursor.fetchone():
            raise HTTPException(404, "Article not found")
        await db.execute("UPDATE articles SET title = ? WHERE id = ?", (new_title, article_id))
        await db.commit()
    return {"id": article_id, "title": new_title}


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


@app.get("/api/articles/{article_id}/textlayer/{page_num}")
async def get_textlayer(article_id: int, page_num: int, request: Request):
    """PyMuPDF로 텍스트 레이어 추출 – 인코딩이 깨진 PDF 대응"""
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

    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        if page_num < 1 or page_num > len(doc):
            raise HTTPException(400, "Invalid page number")
        page = doc[page_num - 1]
        pw, ph = page.rect.width, page.rect.height
        words = page.get_text("words")  # (x0,y0,x1,y1, text, block,line,word)
        doc.close()
        result = [
            {"x": w[0]/pw, "y": w[1]/ph, "w": (w[2]-w[0])/pw, "h": (w[3]-w[1])/ph, "t": w[4]}
            for w in words if w[4].strip()
        ]
        return {"words": result}
    except HTTPException:
        raise
    except Exception as e:
        return {"words": [], "error": str(e)}


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


# ─── Class Routes ────────────────────────────────────────────────────────────

@app.get("/api/classes/{class_id}/info")
async def get_class_info(class_id: int, request: Request):
    await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM classes WHERE id = ?", (class_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Class not found")
        return dict(row)


@app.get("/api/classes")
async def list_classes(request: Request):
    user = await get_current_user(request)
    user_id = int(user["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user["role"] == "instructor":
            cursor = await db.execute(
                "SELECT * FROM classes WHERE instructor_id = ? ORDER BY created_at DESC", (user_id,)
            )
        else:
            cursor = await db.execute("""
                SELECT c.* FROM classes c
                JOIN class_members cm ON c.id = cm.class_id
                WHERE cm.user_id = ? ORDER BY c.created_at DESC
            """, (user_id,))
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            cur2 = await db.execute("SELECT COUNT(*) as cnt FROM class_members WHERE class_id = ?", (d["id"],))
            cnt = await cur2.fetchone()
            d["member_count"] = cnt["cnt"] if cnt else 0
            cur3 = await db.execute("SELECT COUNT(*) as cnt FROM articles WHERE class_id = ?", (d["id"],))
            cnt3 = await cur3.fetchone()
            d["article_count"] = cnt3["cnt"] if cnt3 else 0
            result.append(d)
        return result


@app.post("/api/classes")
async def create_class(req: CreateClassRequest, request: Request):
    user = await require_instructor(request)
    user_id = int(user["sub"])
    join_code = generate_join_code()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "INSERT INTO classes (name, description, join_code, instructor_id) VALUES (?, ?, ?, ?)",
            (req.name.strip(), (req.description or '').strip(), join_code, user_id),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM classes WHERE id = ?", (cur.lastrowid,))
        row = await cursor.fetchone()
        d = dict(row)
        d["member_count"] = 0
        return d


@app.delete("/api/classes/{class_id}")
async def delete_class(class_id: int, request: Request):
    user = await require_instructor(request)
    user_id = int(user["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM classes WHERE id = ? AND instructor_id = ?", (class_id, user_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(404, "Class not found")
        await db.execute("DELETE FROM class_members WHERE class_id = ?", (class_id,))
        await db.execute("DELETE FROM classes WHERE id = ?", (class_id,))
        await db.commit()
    return {"ok": True}


@app.post("/api/classes/join")
async def join_class(req: JoinClassRequest, request: Request):
    user = await get_current_user(request)
    user_id = int(user["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM classes WHERE join_code = ?", (req.join_code.strip().upper(),)
        )
        cls = await cursor.fetchone()
        if not cls:
            raise HTTPException(404, "잘못된 참여 코드입니다")
        try:
            await db.execute(
                "INSERT INTO class_members (class_id, user_id) VALUES (?, ?)", (cls["id"], user_id)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # already member
        return dict(cls)


@app.get("/api/classes/{class_id}/members")
async def get_class_members(class_id: int, request: Request):
    user = await require_instructor(request)
    user_id = int(user["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM classes WHERE id = ? AND instructor_id = ?", (class_id, user_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(403, "Not your class")
        cursor = await db.execute("""
            SELECT u.id, u.display_name, u.username,
                   COUNT(a.id) as annotation_count,
                   SUM(CASE WHEN a.type='highlight' THEN 1 ELSE 0 END) as highlight_count,
                   SUM(CASE WHEN a.type='underline' THEN 1 ELSE 0 END) as underline_count,
                   SUM(CASE WHEN a.type='note'      THEN 1 ELSE 0 END) as note_count
            FROM class_members cm
            JOIN users u ON cm.user_id = u.id
            LEFT JOIN annotations a ON a.user_id = u.id
            WHERE cm.class_id = ?
            GROUP BY u.id ORDER BY u.display_name
        """, (class_id,))
        rows = await cursor.fetchall()
        students = []
        for r in rows:
            d = dict(r)
            cur2 = await db.execute("""
                SELECT a.id, a.type, a.page, a.data, a.created_at,
                       ar.title as article_title, ar.id as article_id
                FROM annotations a JOIN articles ar ON a.article_id = ar.id
                WHERE a.user_id = ? ORDER BY ar.title, a.page, a.created_at
            """, (d["id"],))
            anns = await cur2.fetchall()
            ann_list = []
            for an in anns:
                ad = dict(an)
                ad["data"] = json.loads(ad["data"])
                ann_list.append(ad)
            d["annotations"] = ann_list
            students.append(d)
        return students


# ─── My Annotations ───────────────────────────────────────────────────────────

@app.get("/api/my-annotations")
async def get_my_annotations(request: Request, article_id: Optional[int] = None):
    user = await get_current_user(request)
    user_id = int(user["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if article_id:
            cursor = await db.execute("""
                SELECT a.id, a.type, a.page, a.data, a.created_at,
                       ar.title as article_title, ar.id as article_id
                FROM annotations a JOIN articles ar ON a.article_id = ar.id
                WHERE a.user_id = ? AND a.article_id = ?
                ORDER BY a.page ASC, a.created_at ASC
            """, (user_id, article_id))
        else:
            cursor = await db.execute("""
                SELECT a.id, a.type, a.page, a.data, a.created_at,
                       ar.title as article_title, ar.id as article_id
                FROM annotations a JOIN articles ar ON a.article_id = ar.id
                WHERE a.user_id = ?
                ORDER BY ar.title ASC, a.page ASC, a.created_at ASC
            """, (user_id,))
        rows = await cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            result.append(d)
        return result


# ─── Comment Routes ───────────────────────────────────────────────────────────

@app.get("/api/comments/{annotation_id}")
async def get_comments(annotation_id: str, request: Request):
    await get_current_user(request)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT c.id, c.annotation_id, c.text, c.created_at,
                   u.display_name, u.username, u.id as user_id
            FROM annotation_comments c JOIN users u ON c.user_id = u.id
            WHERE c.annotation_id = ? ORDER BY c.created_at ASC
        """, (annotation_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


@app.post("/api/comments")
async def create_comment(req: CreateCommentRequest, request: Request):
    user = await get_current_user(request)
    user_id = int(user["sub"])
    comment_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT article_id FROM annotations WHERE id = ?", (req.annotation_id,)
        )
        ann = await cursor.fetchone()
        if not ann:
            raise HTTPException(404, "Annotation not found")
        await db.execute(
            "INSERT INTO annotation_comments (id, annotation_id, user_id, text) VALUES (?, ?, ?, ?)",
            (comment_id, req.annotation_id, user_id, req.text.strip()),
        )
        await db.commit()
        cursor = await db.execute("""
            SELECT c.id, c.annotation_id, c.text, c.created_at,
                   u.display_name, u.username, u.id as user_id
            FROM annotation_comments c JOIN users u ON c.user_id = u.id
            WHERE c.id = ?
        """, (comment_id,))
        result = dict(await cursor.fetchone())
    await manager.broadcast(
        {"event": "comment_added", "comment": result}, ann["article_id"]
    )
    return result


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: str, request: Request):
    user = await get_current_user(request)
    user_id = int(user["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id FROM annotation_comments WHERE id = ?", (comment_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Comment not found")
        if row["user_id"] != user_id and user["role"] != "instructor":
            raise HTTPException(403, "Not allowed")
        await db.execute("DELETE FROM annotation_comments WHERE id = ?", (comment_id,))
        await db.commit()
    return {"ok": True}


# ─── Event Logging ────────────────────────────────────────────────────────────

@app.post("/api/log")
async def log_event(req: LogEventRequest, request: Request):
    user = await get_current_user(request)
    if not neon_pool:
        return {"ok": True}
    try:
        async with neon_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO web_logs
                    (session_id, user_id, username, display_name, event_type,
                     article_id, article_title, class_id, page, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
                req.session_id, int(user["sub"]), user["username"], user["display_name"],
                req.event_type, req.article_id, req.article_title,
                req.class_id, req.page,
                json.dumps(req.metadata) if req.metadata else None,
            )
    except Exception as e:
        print(f"Log write failed: {e}")
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


@app.get("/class/{class_id}")
async def class_page(class_id: int):
    return FileResponse("static/class.html")


@app.get("/reader/{article_id}")
async def reader_page(article_id: int):
    return FileResponse("static/reader.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
