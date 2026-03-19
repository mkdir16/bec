from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
import aiofiles
import os
import uuid

from database import get_db, engine, Base
from models import User, Subject, Question, Option, Result
from auth import verify_telegram_init_data

app = FastAPI(title="Quiz App API")

# CORS — разрешаем фронтенду обращаться к серверу
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене укажи конкретный домен
    allow_methods=["*"],
    allow_headers=["*"],
)

# Папка для загружаемых картинок
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ─── Запуск: создаём таблицы в БД ──────────────────────────────────────────

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ База данных готова")


# ─── Вспомогательная функция: получить текущего юзера ──────────────────────

async def get_current_user(
    x_init_data: str = Header(..., alias="X-Init-Data"),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Фронтенд должен передавать заголовок X-Init-Data = window.Telegram.WebApp.initData
    """
    tg_user = verify_telegram_init_data(x_init_data)
    if not tg_user:
        raise HTTPException(status_code=401, detail="Неверная подпись Telegram")

    tg_id = tg_user["id"]

    # Ищем юзера в БД, если нет — создаём
    result = await db.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            tg_id=tg_id,
            name=tg_user.get("first_name", ""),
            is_admin=(str(tg_id) == os.getenv("ADMIN_TG_ID", "0"))
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user


# ─── ПУБЛИЧНЫЕ РОУТЫ ────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "Quiz API работает 🎉"}


@app.get("/subjects")
async def get_subjects(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Получить список всех предметов"""
    result = await db.execute(select(Subject))
    subjects = result.scalars().all()
    return [{"id": s.id, "title": s.title, "emoji": s.emoji} for s in subjects]


@app.get("/questions/{subject_id}")
async def get_questions(
    subject_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Получить вопросы по предмету (без правильного ответа — его скрываем!)"""
    result = await db.execute(
        select(Question).where(Question.subject_id == subject_id)
    )
    questions = result.scalars().all()

    output = []
    for q in questions:
        opts_result = await db.execute(
            select(Option)
            .where(Option.question_id == q.id)
            .order_by(Option.order_index)
        )
        options = opts_result.scalars().all()
        output.append({
            "id": q.id,
            "text": q.text,
            "image_url": q.image_url,
            "options": [{"id": o.id, "text": o.text} for o in options]
            # НЕ возвращаем correct_option_id — студент не должен видеть!
        })
    return output


class SubmitResultRequest(BaseModel):
    subject_id: int
    answers: dict  # {question_id: chosen_option_index}


@app.post("/results")
async def submit_result(
    payload: SubmitResultRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Принять ответы студента, посчитать баллы и сохранить"""
    # Загружаем вопросы
    result = await db.execute(
        select(Question).where(Question.subject_id == payload.subject_id)
    )
    questions = result.scalars().all()

    score = 0
    for q in questions:
        chosen = payload.answers.get(str(q.id))
        if chosen is not None and int(chosen) == q.correct_option_id:
            score += 1

    # Сохраняем результат
    res = Result(
        user_id=user.id,
        subject_id=payload.subject_id,
        score=score,
        total=len(questions)
    )
    db.add(res)
    await db.commit()

    return {
        "score": score,
        "total": len(questions),
        "percentage": round(score / len(questions) * 100) if questions else 0
    }


# ─── АДМИНСКИЕ РОУТЫ ────────────────────────────────────────────────────────

def require_admin(user: User = Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Только для администраторов")
    return user


@app.post("/admin/subjects")
async def create_subject(
    title: str,
    emoji: str = "📚",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin)
):
    """Создать новый предмет"""
    subject = Subject(title=title, emoji=emoji)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return {"id": subject.id, "title": subject.title}


class AddQuestionRequest(BaseModel):
    subject_id: int
    text: str
    options: list[str]        # ["Вариант А", "Вариант Б", "Вариант В", "Вариант Г"]
    correct_index: int         # 0, 1, 2 или 3
    image_url: Optional[str] = None


@app.post("/admin/questions")
async def add_question(
    payload: AddQuestionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin)
):
    """Добавить новый вопрос"""
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Нужно минимум 2 варианта ответа")

    q = Question(
        subject_id=payload.subject_id,
        text=payload.text,
        image_url=payload.image_url,
        correct_option_id=payload.correct_index
    )
    db.add(q)
    await db.flush()  # получаем q.id

    for i, opt_text in enumerate(payload.options):
        opt = Option(question_id=q.id, text=opt_text, order_index=i)
        db.add(opt)

    await db.commit()
    return {"id": q.id, "message": "Вопрос добавлен ✅"}


@app.post("/admin/upload-image")
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(require_admin)
):
    """Загрузить картинку к вопросу"""
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Только картинки!")

    ext = file.filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    path = f"uploads/{filename}"

    async with aiofiles.open(path, "wb") as f:
        content = await file.read()
        await f.write(content)

    return {"image_url": f"/uploads/{filename}"}


@app.get("/admin/results")
async def get_all_results(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin)
):
    """Посмотреть результаты всех студентов"""
    results = await db.execute(
        select(Result, User, Subject)
        .join(User, Result.user_id == User.id)
        .join(Subject, Result.subject_id == Subject.id)
        .order_by(Result.created_at.desc())
    )
    rows = results.all()
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
