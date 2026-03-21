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

from database import get_db, engine, Base
from models import User, Subject, Question, Option, Result
from auth import create_token, verify_token

app = FastAPI(title="UniQuiz API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

SUBSCRIPTION_DAYS = 30
FREE_TRIAL_DAYS = 3


def hash_password(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
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


def get_current_user(
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db)
) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Неверный формат токена")
    payload = verify_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Токен недействителен")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
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
    }


# ── ПУБЛИЧНЫЕ РОУТЫ ──────────────────────────────────────────────────────

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
    full_name: str        # Имя студента
    username: str         # Придуманный логин
    password: str         # Пароль


@app.post("/register")
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """Самостоятельная регистрация студента — получает 3 дня бесплатно"""

    # Проверяем что логин не занят
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Этот логин уже занят, придумай другой")

    # Проверяем минимальную длину
    if len(payload.username) < 3:
        raise HTTPException(status_code=400, detail="Логин должен быть минимум 3 символа")
    if len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть минимум 4 символа")
    if len(payload.full_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Введи своё имя")

    # Создаём студента с триалом
    new_user = User(
        username=payload.username.lower().strip(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
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


# ── ПРЕДМЕТЫ И ВОПРОСЫ ───────────────────────────────────────────────────

@app.get("/subjects")
def get_subjects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [{"id": s.id, "title": s.title, "emoji": s.emoji, "time_limit": s.time_limit} for s in db.query(Subject).all()]


@app.get("/questions/{subject_id}")
def get_questions(subject_id: int, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    output = []
    for q in questions:
        options = db.query(Option).filter(Option.question_id == q.id).order_by(Option.order_index).all()
        output.append({
            "id": q.id, "text": q.text, "image_url": q.image_url,
            "correct_option_id": q.correct_option_id,
            "options": [{"id": o.id, "text": o.text} for o in options]
        })
    return output


class SubmitResultRequest(BaseModel):
    subject_id: int
    answers: dict


@app.post("/results")
def submit_result(payload: SubmitResultRequest, db: Session = Depends(get_db), user: User = Depends(require_subscription)):
    questions = db.query(Question).filter(Question.subject_id == payload.subject_id).all()
    score = sum(1 for q in questions if payload.answers.get(str(q.id)) is not None and int(payload.answers[str(q.id)]) == q.correct_option_id)
    db.add(Result(user_id=user.id, subject_id=payload.subject_id, score=score, total=len(questions)))
    db.commit()
    return {"score": score, "total": len(questions), "percentage": round(score / len(questions) * 100) if questions else 0}


@app.get("/my-results")
def my_results(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Result, Subject).join(Subject, Result.subject_id == Subject.id).filter(Result.user_id == user.id).order_by(Result.created_at.desc()).all()
    return [{"subject": r.Subject.title, "emoji": r.Subject.emoji, "score": r.Result.score, "total": r.Result.total, "percentage": round(r.Result.score / r.Result.total * 100) if r.Result.total else 0, "date": r.Result.created_at.isoformat() if r.Result.created_at else None} for r in rows]


# ── АДМИН: ПРЕДМЕТЫ ──────────────────────────────────────────────────────

@app.post("/admin/subjects")
def create_subject(title: str, emoji: str = "📚", time_limit: int = 60, db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    s = Subject(title=title, emoji=emoji, time_limit=time_limit)
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "title": s.title, "time_limit": s.time_limit}


# ── АДМИН: ВОПРОСЫ ───────────────────────────────────────────────────────

class AddQuestionRequest(BaseModel):
    subject_id: int
    text: str
    options: list[str]
    correct_index: int
    image_url: Optional[str] = None


@app.post("/admin/questions")
def add_question(payload: AddQuestionRequest, db: Session = Depends(get_db), user: User = Depends(require_teacher)):
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Нужно минимум 2 варианта")
    q = Question(subject_id=payload.subject_id, text=payload.text, image_url=payload.image_url, correct_option_id=payload.correct_index)
    db.add(q); db.flush()
    for i, t in enumerate(payload.options):
        db.add(Option(question_id=q.id, text=t, order_index=i))
    db.commit()
    return {"id": q.id, "message": "Вопрос добавлен ✅"}


@app.post("/admin/upload-image")
async def upload_image(file: UploadFile = File(...), user: User = Depends(require_teacher)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Только картинки!")
    filename = f"{uuid.uuid4()}.{file.filename.split('.')[-1]}"
    async with aiofiles.open(f"uploads/{filename}", "wb") as f:
        f.write(await file.read())
    return {"image_url": f"/uploads/{filename}"}


@app.post("/admin/import-questions")
async def import_questions(
    subject_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_teacher)
):
    """Импорт вопросов из Excel файла"""
    import openpyxl
    import tempfile

    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Только Excel файлы (.xlsx)")

    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Предмет не найден")

    # Сохраняем файл временно
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
            # Пропускаем пустые строки
            if not row[0]:
                continue

            q_text = str(row[0]).strip() if row[0] else ""
            opt_a = str(row[1]).strip() if row[1] else ""
            opt_b = str(row[2]).strip() if row[2] else ""
            opt_c = str(row[3]).strip() if row[3] else ""
            opt_d = str(row[4]).strip() if row[4] else ""
            opt_e = str(row[5]).strip() if row[5] else ""
            correct_letter = str(row[6]).strip().upper() if row[6] else ""

            # Проверяем обязательные поля
            if not q_text:
                errors.append(f"Строка {row_num}: пустой вопрос")
                continue
            if not opt_a or not opt_b:
                errors.append(f"Строка {row_num}: нужно минимум 2 варианта (A и Б)")
                continue
            if correct_letter not in letter_to_index:
                errors.append(f"Строка {row_num}: неверный правильный ответ '{correct_letter}' (нужно A/Б/В/Г/Д)")
                continue

            # Собираем варианты
            options = [opt_a, opt_b]
            if opt_c: options.append(opt_c)
            if opt_d: options.append(opt_d)
            if opt_e: options.append(opt_e)

            correct_index = letter_to_index[correct_letter]
            if correct_index >= len(options):
                errors.append(f"Строка {row_num}: правильный ответ '{correct_letter}' — варианта нет")
                continue

            # Добавляем вопрос
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

        result = {"added": added, "errors": errors[:10]}  # максимум 10 ошибок
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
