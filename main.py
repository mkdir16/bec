from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import hashlib
import aiofiles
import os
import uuid
import json
import random
import string
import re
from contextlib import asynccontextmanager

from database import get_db, engine, Base
from models import User, Subject, Question, Option, Result, UserAchievement, Duel
from auth import create_token, verify_token


# ====================== HASH ======================
def hash_password(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()


# ====================== LIFESPAN (ИСПРАВЛЕН) ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup - создаём таблицы
    Base.metadata.create_all(bind=engine)
    
    # Создаём отдельную сессию для проверки/создания админа
    from database import SessionLocal
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            db.add(User(
                username="admin",
                password_hash=hash_password("admin123"),
                full_name="Администратор",
                role="admin",
                subscription_active=True,
                is_trial=False
            ))
            db.commit()
            print("✅ Админ создан: admin / admin123")
        print("✅ База данных готова")
    finally:
        db.close()
    
    yield
    # Shutdown - ничего не делаем


# ====================== FASTAPI APP ======================
app = FastAPI(title="UniQuiz API", lifespan=lifespan)

# ── CORS ── ОБЯЗАТЕЛЬНО САМЫМ ПЕРВЫМ!
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://fron-alpha.vercel.app",
        "https://www.fron-alpha.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Папка для локальных загрузок
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

SUBSCRIPTION_DAYS = 30
FREE_TRIAL_DAYS = 3

# ── SUPABASE STORAGE ──────────────────────────────────────────────────────
import httpx as _httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_BUCKET = "images"


async def upload_to_storage(file: UploadFile, folder: str = "questions") -> str:
    """Загружает файл в Supabase Storage или локально"""
    content_bytes = await file.read()
    ext = file.filename.split(".")[-1].lower()
    filename = f"{folder}/{uuid.uuid4()}.{ext}"

    if SUPABASE_URL and SUPABASE_KEY:
        upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": file.content_type or "image/jpeg",
            "x-upsert": "true"
        }
        async with _httpx.AsyncClient() as client:
            resp = await client.post(upload_url, content=content_bytes, headers=headers)
            if resp.status_code not in (200, 201):
                raise HTTPException(status_code=500, detail=f"Ошибка загрузки: {resp.text}")
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    else:
        local_filename = f"uploads/{uuid.uuid4()}.{ext}"
        async with aiofiles.open(local_filename, "wb") as f:
            await f.write(content_bytes)
        return f"/{local_filename}"


# ====================== AUTH DEPENDENCIES ======================
def get_current_user(
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db)
) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Неверный формат токена")
    payload = verify_token(authorization[7:])
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=401, detail="Токен недействителен")
    try:
        user_id = int(payload["sub"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Неверный токен")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    
    if user.subscription_expires and user.subscription_expires < datetime.utcnow() and user.role == "student":
        user.subscription_active = False
        db.commit()
    return user


def require_teacher(user: User = Depends(get_current_user)):
    if user.role not in ["teacher", "admin"]:
        raise HTTPException(status_code=403, detail="Только для преподавателей")
    return user


def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Только для администраторов")
    return user


def require_subscription(user: User = Depends(get_current_user)):
    if user.role in ["admin", "teacher"]:
        return user
    if not user.subscription_active:
        raise HTTPException(status_code=402, detail="Требуется подписка")
    return user


def user_to_dict(user: User) -> dict:
    days_left = None
    if user.subscription_expires and user.role == "student":
        delta = user.subscription_expires - datetime.utcnow()
        days_left = max(0, delta.days)
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "subscription_active": user.subscription_active,
        "subscription_expires": user.subscription_expires.isoformat() if user.subscription_expires else None,
        "days_left": days_left,
        "is_trial": user.is_trial,
        "phone": user.phone,
        "lang": user.lang or "ru",
    }


# ====================== PUBLIC ROUTES ======================
@app.get("/")
@app.head("/")
def root():
    return {"status": "UniQuiz API 🎉"}


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or user.password_hash != hash_password(payload.password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return {"token": create_token(user.id, user.role), "user": user_to_dict(user)}


class RegisterRequest(BaseModel):
    full_name: str
    username: str
    password: str
    phone: str
    lang: str = "ru"


@app.post("/register")
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    phone_clean = re.sub(r'[^0-9]', '', payload.phone or "")
    if len(phone_clean) < 9:
        raise HTTPException(status_code=400, detail="Введи корректный номер телефона")
    
    if phone_clean.startswith("998"):
        phone_clean = "+" + phone_clean
    elif phone_clean.startswith("8") and len(phone_clean) == 11:
        phone_clean = "+7" + phone_clean[1:]
    else:
        phone_clean = "+998" + phone_clean.lstrip("0")

    existing_phone = db.query(User).filter(User.phone == phone_clean).first()
    if existing_phone:
        if existing_phone.is_trial and not existing_phone.subscription_active:
            raise HTTPException(status_code=400, detail="С этого номера уже использовался пробный период. Для продолжения оплати подписку — @mkdir16")
        else:
            raise HTTPException(status_code=400, detail="Этот номер телефона уже зарегистрирован")

    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Этот логин уже занят, придумай другой")

    if len(payload.username) < 3:
        raise HTTPException(status_code=400, detail="Логин должен быть минимум 3 символа")
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть минимум 4 символа")
    if len(payload.full_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Введи своё имя")

    new_user = User(
        username=payload.username.lower().strip(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        phone=phone_clean,
        lang=payload.lang,
        role="student",
        subscription_active=True,
        subscription_expires=datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS),
        is_trial=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    token = create_token(new_user.id, new_user.role)
    return {"token": token, "user": user_to_dict(new_user)}


@app.get("/me")
def get_me(user: User = Depends(get_current_user)):
    return user_to_dict(user)


@app.post("/me/lang")
def update_lang(lang: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if lang not in ["ru", "uz", "en"]:
        raise HTTPException(status_code=400, detail="Язык: ru, uz, en")
    user.lang = lang
    db.commit()
    return {"lang": lang}


# ── ПРЕДМЕТЫ (ГЛАВНЫЙ ИСПРАВЛЕННЫЙ РОУТ) ──
@app.get("/subjects")
def get_subjects(db: Session = Depends(get_db)):
    subjects = db.query(Subject).all()
    result = []
    for s in subjects:
        total = db.query(Question).filter(Question.subject_id == s.id).count()
        result.append({
            "id": s.id,
            "title": s.title,
            "emoji": s.emoji,
            "time_limit": s.time_limit,
            "question_count": s.question_count or 30,
            "total_questions": total,
            "lang": s.lang or "all"
        })
    return result


# ====================== ОСТАЛЬНЫЕ РОУТЫ ======================

@app.get("/questions/{subject_id}")
def get_questions(
    subject_id: int,
    limit: int = 30,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscription)
):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    if len(questions) > limit:
        questions = random.sample(questions, limit)
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({
            "id": q.id, "text": q.text, "image_url": q.image_url,
            "correct_option_id": q.correct_option_id,
            "options": [{"id": o.id, "text": o.text, "image_url": o.image_url} for o in options]
        })
    return output


@app.get("/questions-all/{subject_id}")
def get_all_questions_student(subject_id: int, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({
            "id": q.id, "text": q.text, "image_url": q.image_url,
            "correct_option_id": q.correct_option_id,
            "options": [{"id": o.id, "text": o.text, "image_url": o.image_url} for o in options]
        })
    return output


@app.get("/knowledge/{subject_id}")
def get_knowledge(
    subject_id: int,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    total = db.query(Question).filter(Question.subject_id == subject_id).count()
    questions = (
        db.query(Question)
        .filter(Question.subject_id == subject_id)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({
            "id": q.id, "text": q.text,
            "correct_option_id": q.correct_option_id,
            "options": [{"id": o.id, "text": o.text, "image_url": o.image_url} for o in options]
        })
    return {"total": total, "page": page, "per_page": per_page, "questions": output}


class SubmitResultRequest(BaseModel):
    subject_id: int
    answers: dict


@app.post("/results")
def submit_result(payload: SubmitResultRequest, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    questions = db.query(Question).filter(Question.subject_id == payload.subject_id).all()
    score = sum(1 for q in questions if payload.answers.get(str(q.id)) is not None and int(payload.answers[str(q.id)]) == q.correct_option_id)
    db.add(Result(user_id=user.id, subject_id=payload.subject_id, score=score, total=len(questions)))
    db.commit()
    new_achievements = check_and_award(user.id, db)
    return {
        "score": score,
        "total": len(questions),
        "percentage": round(score / len(questions) * 100) if questions else 0,
        "new_achievements": new_achievements
    }


@app.get("/rating/{subject_id}")
def get_rating(subject_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(Result, User, Subject)
        .join(User, Result.user_id == User.id)
        .join(Subject, Result.subject_id == Subject.id)
        .filter(Result.subject_id == subject_id)
        .order_by(Result.created_at.desc())
        .all()
    )
    best = {}
    for r in rows:
        name = r.User.full_name or r.User.username
        pct = round(r.Result.score / r.Result.total * 100) if r.Result.total else 0
        if name not in best or pct > best[name]["pct"]:
            best[name] = {
                "name": name,
                "score": r.Result.score,
                "total": r.Result.total,
                "pct": pct
            }
    return sorted(best.values(), key=lambda x: x["pct"], reverse=True)


@app.get("/rating")
def get_rating_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(Result, User, Subject)
        .join(User, Result.user_id == User.id)
        .join(Subject, Result.subject_id == Subject.id)
        .order_by(Result.created_at.desc())
        .all()
    )
    result = []
    for r in rows:
        result.append({
            "student": r.User.full_name or r.User.username,
            "username": r.User.username,
            "subject": r.Subject.title,
            "subject_id": r.Subject.id,
            "score": r.Result.score,
            "total": r.Result.total,
            "date": r.Result.created_at.isoformat() if r.Result.created_at else None
        })
    return result


@app.get("/my-progress/{subject_id}")
def my_progress(subject_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    results = (
        db.query(Result)
        .filter(Result.user_id == user.id, Result.subject_id == subject_id)
        .order_by(Result.created_at.desc())
        .all()
    )
    if not results:
        return {"attempts": 0, "best_score": 0, "best_pct": 0, "last_score": 0, "last_pct": 0, "total": 0}

    best = max(results, key=lambda r: r.score / r.total if r.total else 0)
    last = results[0]
    return {
        "attempts": len(results),
        "best_score": best.score,
        "best_total": best.total,
        "best_pct": round(best.score / best.total * 100) if best.total else 0,
        "last_score": last.score,
        "last_total": last.total,
        "last_pct": round(last.score / last.total * 100) if last.total else 0,
    }


@app.get("/my-results")
def my_results(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Result, Subject).join(Subject, Result.subject_id == Subject.id).filter(Result.user_id == user.id).order_by(Result.created_at.desc()).all()
    return [{"subject": r.Subject.title, "emoji": r.Subject.emoji, "score": r.Result.score, "total": r.Result.total, "percentage": round(r.Result.score / r.Result.total * 100) if r.Result.total else 0, "date": r.Result.created_at.isoformat() if r.Result.created_at else None} for r in rows]


# ── АДМИН: ПРЕДМЕТЫ ──────────────────────────────────────────────────────

@app.post("/admin/subjects")
def create_subject(title: str, emoji: str = "📚", time_limit: int = 60, question_count: int = 30, lang: str = "all", db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    s = Subject(title=title, emoji=emoji, time_limit=time_limit, question_count=question_count, lang=lang)
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "title": s.title, "time_limit": s.time_limit, "question_count": s.question_count}


# ── АДМИН: ВОПРОСЫ ───────────────────────────────────────────────────────

class OptionInput(BaseModel):
    text: Optional[str] = None
    image_url: Optional[str] = None

class AddQuestionRequest(BaseModel):
    subject_id: int
    text: str
    options: list[OptionInput]
    correct_index: int
    image_url: Optional[str] = None


@app.post("/admin/questions")
def add_question(payload: AddQuestionRequest, db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Нужно минимум 2 варианта")
    q = Question(subject_id=payload.subject_id, text=payload.text, image_url=payload.image_url, correct_option_id=payload.correct_index)
    db.add(q); db.flush()
    for i, opt in enumerate(payload.options):
        if isinstance(opt, str):
            db.add(Option(question_id=q.id, text=opt, order_index=i))
        else:
            db.add(Option(question_id=q.id, text=opt.text, image_url=opt.image_url, order_index=i))
    db.commit()
    return {"id": q.id, "message": "Вопрос добавлен ✅"}


@app.post("/admin/upload-image")
async def upload_image(file: UploadFile = File(...), user: User = Depends(require_teacher)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Только картинки!")
    image_url = await upload_to_storage(file, folder="uniquiz/questions")
    return {"image_url": image_url}


@app.post("/admin/upload-option-image")
async def upload_option_image(file: UploadFile = File(...), user: User = Depends(require_teacher)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Только картинки!")
    image_url = await upload_to_storage(file, folder="uniquiz/options")
    return {"image_url": image_url}


@app.post("/admin/import-questions")
async def import_questions(
    subject_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_teacher)
):
    import openpyxl
    import tempfile

    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Только Excel файлы (.xlsx)")

    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Предмет не найден")

    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        wb = openpyxl.load_workbook(tmp_path)
        ws = wb.active

        letter_to_index = {"A": 0, "Б": 1, "В": 2, "Г": 3, "Д": 4}
        added = 0
        errors = []

        for row_num, row in enumerate(ws.iter_rows(min_row=6, values_only=True), start=6):
            if not row[0]:
                continue

            q_text = str(row[0]).strip() if row[0] else ""
            opt_a = str(row[1]).strip() if row[1] else ""
            opt_b = str(row[2]).strip() if row[2] else ""
            opt_c = str(row[3]).strip() if row[3] else ""
            opt_d = str(row[4]).strip() if row[4] else ""
            opt_e = str(row[5]).strip() if row[5] else ""
            correct_letter = str(row[6]).strip().upper() if row[6] else ""

            if not q_text:
                errors.append(f"Строка {row_num}: пустой вопрос")
                continue
            if not opt_a or not opt_b:
                errors.append(f"Строка {row_num}: нужно минимум 2 варианта (A и Б)")
                continue
            if correct_letter not in letter_to_index:
                errors.append(f"Строка {row_num}: неверный правильный ответ '{correct_letter}' (нужно A/Б/В/Г/Д)")
                continue

            options = [opt_a, opt_b]
            if opt_c: options.append(opt_c)
            if opt_d: options.append(opt_d)
            if opt_e: options.append(opt_e)

            correct_index = letter_to_index[correct_letter]
            if correct_index >= len(options):
                errors.append(f"Строка {row_num}: правильный ответ '{correct_letter}' — варианта нет")
                continue

            q = Question(
                subject_id=subject_id,
                text=q_text,
                correct_option_id=correct_index
            )
            db.add(q)
            db.flush()

            for i, opt_text in enumerate(options):
                db.add(Option(question_id=q.id, text=opt_text, order_index=i))

            added += 1

        db.commit()

        result = {"added": added, "errors": errors[:10]}
        if errors:
            result["message"] = f"Добавлено {added} вопросов, пропущено {len(errors)} строк с ошибками"
        else:
            result["message"] = f"Успешно добавлено {added} вопросов ✅"

        return result

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Ошибка чтения файла: {str(e)}")
    finally:
        import os as _os
        _os.unlink(tmp_path)


@app.get("/admin/questions/{subject_id}")
def get_all_questions_admin(subject_id: int, db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({
            "id": q.id, "text": q.text, "image_url": q.image_url,
            "correct_option_id": q.correct_option_id,
            "options": [{"id": o.id, "text": o.text, "image_url": o.image_url} for o in options]
        })
    return output


@app.delete("/admin/questions/{question_id}")
def delete_question(question_id: int, db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    db.delete(q); db.commit()
    return {"message": "Вопрос удалён ✅"}


@app.put("/admin/questions/{question_id}")
def edit_question(question_id: int, payload: AddQuestionRequest, db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    q.text = payload.text
    q.correct_option_id = payload.correct_index
    if payload.image_url is not None:
        q.image_url = payload.image_url
    db.query(Option).filter(Option.question_id == question_id).delete()
    for i, opt in enumerate(payload.options):
        if isinstance(opt, str):
            db.add(Option(question_id=q.id, text=opt, order_index=i))
        else:
            db.add(Option(question_id=q.id, text=opt.text, image_url=getattr(opt, 'image_url', None), order_index=i))
    db.commit()
    return {"message": "Вопрос обновлён ✅"}


@app.get("/admin/results")
def get_all_results(db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    rows = db.query(Result, User, Subject).join(User, Result.user_id == User.id).join(Subject, Result.subject_id == Subject.id).order_by(Result.created_at.desc()).all()
    return [{"student": r.User.full_name or r.User.username, "username": r.User.username, "subject": r.Subject.title, "score": r.Result.score, "total": r.Result.total, "date": r.Result.created_at.isoformat() if r.Result.created_at else None} for r in rows]


# ── АДМИН: ПОЛЬЗОВАТЕЛИ ──────────────────────────────────────────────────

@app.get("/admin/users")
def get_all_users(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [{
        "id": u.id, "username": u.username, "full_name": u.full_name,
        "role": u.role, "subscription_active": u.subscription_active,
        "subscription_expires": u.subscription_expires.isoformat() if u.subscription_expires else None,
        "is_trial": u.is_trial,
        "phone": u.phone,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in users]


class CreateUserRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "student"


@app.post("/admin/users")
def create_user(payload: CreateUserRequest, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Такой логин уже существует")
    if payload.role not in ["student", "teacher", "admin"]:
        raise HTTPException(status_code=400, detail="Роль: student, teacher, admin")
    is_student = payload.role == "student"
    new_user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
        subscription_active=True,
        subscription_expires=datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS) if is_student else None,
        is_trial=is_student
    )
    db.add(new_user); db.commit()
    return {"message": f"Пользователь {payload.username} создан ✅"}


@app.delete("/admin/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Не найден")
    if target.username == "admin":
        raise HTTPException(status_code=400, detail="Нельзя удалить главного админа")
    db.delete(target); db.commit()
    return {"message": "Удалён ✅"}


@app.post("/admin/activate-subscription")
def activate_subscription(user_id: int, days: int = 30, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Не найден")
    target.subscription_active = True
    target.subscription_expires = datetime.utcnow() + timedelta(days=days)
    target.is_trial = False
    db.commit()
    return {"message": f"Подписка активирована на {days} дней ✅"}


@app.post("/admin/set-role")
def set_role(user_id: int, role: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if role not in ["admin", "teacher", "student"]:
        raise HTTPException(status_code=400, detail="Роль: admin, teacher, student")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Не найден")
    target.role = role
    if role in ["admin", "teacher"]:
        target.subscription_active = True
        target.is_trial = False
    db.commit()
    return {"message": f"Роль {role} назначена ✅"}


# ── ДОСТИЖЕНИЯ ────────────────────────────────────────────────────────────

ACHIEVEMENTS = {
    "first_test":   {"id": "first_test",   "title": "Первый шаг",    "desc": "Сдал первый тест",             "emoji": "🎯"},
    "perfect":      {"id": "perfect",      "title": "Отличник",      "desc": "100% правильных ответов",      "emoji": "💯"},
    "streak_3":     {"id": "streak_3",     "title": "На волне",      "desc": "3 теста подряд выше 80%",      "emoji": "🔥"},
    "speed_run":    {"id": "speed_run",    "title": "Молния",        "desc": "Закончил тест за 50% времени", "emoji": "⚡"},
    "all_subjects": {"id": "all_subjects", "title": "Всезнайка",     "desc": "Прошёл все предметы",          "emoji": "🏆"},
    "duel_win":     {"id": "duel_win",     "title": "Победитель",    "desc": "Выиграл дуэль",                "emoji": "⚔️"},
    "duel_3":       {"id": "duel_3",       "title": "Дуэлянт",       "desc": "Выиграл 3 дуэли",              "emoji": "🥊"},
}


@app.get("/my-achievements")
def get_my_achievements(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    earned = db.query(UserAchievement).filter(UserAchievement.user_id == user.id).all()
    earned_ids = {a.achievement_id for a in earned}
    result = []
    for ach_id, ach in ACHIEVEMENTS.items():
        result.append({**ach, "earned": ach_id in earned_ids,
                       "earned_at": next((a.earned_at.isoformat() for a in earned if a.achievement_id == ach_id), None)})
    return result


def check_and_award(user_id: int, db: Session, extra: dict = {}):
    results = db.query(Result).filter(Result.user_id == user_id).all()
    earned = {a.achievement_id for a in db.query(UserAchievement).filter(UserAchievement.user_id == user_id).all()}
    new_achievements = []

    def award(ach_id):
        if ach_id not in earned:
            db.add(UserAchievement(user_id=user_id, achievement_id=ach_id))
            new_achievements.append(ACHIEVEMENTS[ach_id])
            earned.add(ach_id)

    if len(results) >= 1: award("first_test")

    last = results[-1] if results else None
    if last and last.total > 0 and last.score == last.total: award("perfect")

    if extra.get("speed_bonus"): award("speed_run")

    if len(results) >= 3:
        last3 = sorted(results, key=lambda r: r.created_at)[-3:]
        if all(r.total > 0 and r.score / r.total >= 0.8 for r in last3):
            award("streak_3")

    subjects_done = {r.subject_id for r in results}
    total_subjects = db.query(Subject).count()
    if total_subjects > 0 and len(subjects_done) >= total_subjects:
        award("all_subjects")

    duel_wins = db.query(Duel).filter(Duel.winner_id == user_id).count()
    if duel_wins >= 1: award("duel_win")
    if duel_wins >= 3: award("duel_3")

    db.commit()
    return new_achievements


# ── ДУЭЛИ ─────────────────────────────────────────────────────────────────

def gen_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


@app.post("/duel/create")
def create_duel(subject_id: int, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    if len(questions) < 10:
        raise HTTPException(status_code=400, detail="Нужно минимум 10 вопросов в предмете")
    selected = random.sample(questions, min(10, len(questions)))
    code = gen_code()
    while db.query(Duel).filter(Duel.code == code).first():
        code = gen_code()
    duel = Duel(
        subject_id=subject_id,
        challenger_id=user.id,
        status="waiting",
        question_ids=json.dumps([q.id for q in selected]),
        code=code
    )
    db.add(duel); db.commit(); db.refresh(duel)
    return {"duel_id": duel.id, "code": code, "subject_id": subject_id}


@app.post("/duel/join/{code}")
def join_duel(code: str, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    duel = db.query(Duel).filter(Duel.code == code.upper(), Duel.status == "waiting").first()
    if not duel:
        raise HTTPException(status_code=404, detail="Дуэль не найдена или уже началась")
    if duel.challenger_id == user.id:
        raise HTTPException(status_code=400, detail="Нельзя присоединиться к своей дуэли")
    duel.opponent_id = user.id
    duel.status = "active"
    db.commit()
    return {"duel_id": duel.id, "subject_id": duel.subject_id}


@app.get("/duel/{duel_id}/questions")
def get_duel_questions(duel_id: int, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel:
        raise HTTPException(status_code=404, detail="Дуэль не найдена")
    if user.id not in [duel.challenger_id, duel.opponent_id]:
        raise HTTPException(status_code=403, detail="Ты не участник этой дуэли")
    q_ids = json.loads(duel.question_ids)
    questions = db.query(Question).filter(Question.id.in_(q_ids)).all()
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({"id": q.id, "text": q.text, "image_url": q.image_url,
                       "correct_option_id": q.correct_option_id,
                       "options": [{"id": o.id, "text": o.text, "image_url": o.image_url} for o in options]})
    return output


@app.post("/duel/{duel_id}/submit")
def submit_duel(duel_id: int, answers: dict, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel or duel.status != "active":
        raise HTTPException(status_code=400, detail="Дуэль не активна")
    is_challenger = user.id == duel.challenger_id
    if not is_challenger and user.id != duel.opponent_id:
        raise HTTPException(status_code=403, detail="Ты не участник")

    q_ids = json.loads(duel.question_ids)
    questions = db.query(Question).filter(Question.id.in_(q_ids)).all()
    score = sum(1 for q in questions if answers.get(str(q.id)) is not None and int(answers[str(q.id)]) == q.correct_option_id)

    if is_challenger:
        duel.challenger_score = score
        duel.challenger_finished = True
    else:
        duel.opponent_score = score
        duel.opponent_finished = True

    if duel.challenger_finished and duel.opponent_finished:
        duel.status = "finished"
        if duel.challenger_score > duel.opponent_score:
            duel.winner_id = duel.challenger_id
        elif duel.opponent_score > duel.challenger_score:
            duel.winner_id = duel.opponent_id
        check_and_award(duel.challenger_id, db)
        check_and_award(duel.opponent_id, db)

    db.commit()
    return {"score": score, "total": len(questions), "finished": duel.challenger_finished and duel.opponent_finished,
            "duel_status": duel.status}


@app.get("/duel/{duel_id}/status")
def duel_status(duel_id: int, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel:
        raise HTTPException(status_code=404, detail="Не найдена")
    is_challenger = user.id == duel.challenger_id
    my_score = duel.challenger_score if is_challenger else duel.opponent_score
    opp_score = duel.opponent_score if is_challenger else duel.challenger_score
    opp_finished = duel.opponent_finished if is_challenger else duel.challenger_finished

    opp_id = duel.opponent_id if is_challenger else duel.challenger_id
    opp = db.query(User).filter(User.id == opp_id).first() if opp_id else None

    return {
        "status": duel.status,
        "my_score": my_score,
        "opp_score": opp_score,
        "opp_name": (opp.full_name or opp.username) if opp else None,
        "opp_finished": opp_finished,
        "winner_id": duel.winner_id,
        "i_won": duel.winner_id == user.id,
        "draw": duel.status == "finished" and duel.winner_id is None,
        "code": duel.code
    }
