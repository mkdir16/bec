import os
import uuid
import aiofiles
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel

# Импорты из ваших файлов
from database import get_db, engine, Base
from models import User, Subject, Question, Option, Result
from auth import verify_telegram_init_data

# --- MIDDLEWARE ДЛЯ ОЧИСТКИ URL ---
class NormalizeSlashesMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if "//" in path:
            parts = [p for p in path.split("/") if p]
            new_path = "/" + "/".join(parts) if parts else "/"
            if new_path != path:
                url = str(request.url.replace(path=new_path))
                return RedirectResponse(url=url, status_code=307)
        return await call_next(request)

app = FastAPI(title="UniQuiz API")

app.add_middleware(NormalizeSlashesMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Папка для загрузок
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    print("✅ База данных синхронизирована")

# --- СХЕМЫ ДАННЫХ (PYDANTIC) ---

class SubjectCreate(BaseModel):
    title: str
    emoji: str = "📚"

class AddQuestionRequest(BaseModel):
    subject_id: int
    text: str
    options: List[str]
    correct_index: int
    image_url: Optional[str] = None

class SubmitResultRequest(BaseModel):
    subject_id: int
    answers: dict

# --- ЗАВИСИМОСТИ (AUTH) ---

def get_current_user(
    x_init_data: str = Header(..., alias="X-Init-Data"),
    db: Session = Depends(get_db)
) -> User:
    tg_user = verify_telegram_init_data(x_init_data)
    if not tg_user:
        raise HTTPException(status_code=401, detail="Неверная авторизация Telegram")
    
    tg_id = tg_user["id"]
    user = db.query(User).filter(User.tg_id == tg_id).first()
    
    if not user:
        # Проверка на админа через переменную окружения
        is_admin = str(tg_id) == os.getenv("ADMIN_TG_ID", "0")
        user = User(
            tg_id=tg_id,
            name=tg_user.get("first_name", "Студент"),
            is_admin=is_admin
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

def require_admin(user: User = Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Доступ запрещен: вы не админ")
    return user

# --- РОУТЫ ПРИЛОЖЕНИЯ ---

@app.get("/")
def root():
    return {"status": "online", "message": "Quiz API is running"}

@app.get("/subjects")
def get_subjects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    subjects = db.query(Subject).all()
    return [{"id": s.id, "title": s.title, "emoji": s.emoji} for s in subjects]

@app.get("/questions/{subject_id}")
def get_questions(subject_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({
            "id": q.id,
            "text": q.text,
            "image_url": q.image_url,
            "options": [{"id": o.id, "text": o.text} for o in options]
        })
    return output

@app.post("/results")
def submit_result(payload: SubmitResultRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    questions = db.query(Question).filter(Question.subject_id == payload.subject_id).all()
    score = 0
    for q in questions:
        chosen = payload.answers.get(str(q.id))
        if chosen is not None and int(chosen) == q.correct_option_id:
            score += 1
    
    res = Result(
        user_id=user.id,
        subject_id=payload.subject_id,
        score=score,
        total=len(questions)
    )
    db.add(res)
    db.commit()
    return {"score": score, "total": len(questions)}

# --- АДМИН-ПАНЕЛЬ ---

@app.post("/admin/subjects")
def create_subject(
    payload: SubjectCreate,  # <--- ИСПРАВЛЕНО: Теперь принимает JSON Body
    db: Session = Depends(get_db),
    user: User = Depends(require_admin)
):
    subject = Subject(title=payload.title, emoji=payload.emoji)
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return {"id": subject.id, "title": subject.title, "emoji": subject.emoji}

@app.post("/admin/questions")
def add_question(payload: AddQuestionRequest, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Нужно минимум 2 варианта ответа")
    
    q = Question(
        subject_id=payload.subject_id,
        text=payload.text,
        image_url=payload.image_url,
        correct_option_id=payload.correct_index
    )
    db.add(q)
    db.flush() 
    
    for i, opt_text in enumerate(payload.options):
        db.add(Option(question_id=q.id, text=opt_text, order_index=i))
    
    db.commit()
    return {"id": q.id, "status": "success"}

@app.post("/admin/upload-image")
async def upload_image(file: UploadFile = File(...), user: User = Depends(require_admin)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Разрешены только изображения")
    
    ext = file.filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    path = f"uploads/{filename}"
    
    async with aiofiles.open(path, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)
        
    return {"image_url": f"/uploads/{filename}"}

@app.get("/admin/results")
def get_all_results(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = (
        db.query(Result, User, Subject)
        .join(User, Result.user_id == User.id)
        .join(Subject, Result.subject_id == Subject.id)
        .order_by(Result.created_at.desc())
        .all()
    )
    return [
        {
            "student": r.User.name,
            "tg_id": r.User.tg_id,
            "subject": r.Subject.title,
            "score": r.Result.score,
            "total": r.Result.total,
            "date": r.Result.created_at.isoformat() if r.Result.created_at else None
        }
        for r in rows
    ]
