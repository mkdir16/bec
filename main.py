from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import aiofiles
import hashlib
import hmac
import json
import base64
import os
import uuid

from database import get_db, engine, Base
from models import User, Subject, Question, Option, Result, Subscription
from auth import verify_telegram_init_data

app = FastAPI(title="UniQuiz API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")
PAYME_SECRET_KEY = os.getenv("PAYME_SECRET_KEY", "")
SUBSCRIPTION_PRICE = 20000  # сум
SUBSCRIPTION_DAYS = 30


# ── Старт ────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    print("✅ База данных готова")


# ── Получить текущего юзера ──────────────────────────────────────────────

def get_current_user(
    x_init_data: str = Header(..., alias="X-Init-Data"),
    db: Session = Depends(get_db)
) -> User:
    tg_user = verify_telegram_init_data(x_init_data)
    if not tg_user:
        raise HTTPException(status_code=401, detail="Неверная подпись Telegram")

    tg_id = tg_user["id"]
    user = db.query(User).filter(User.tg_id == tg_id).first()

    if not user:
        # Новый юзер — определяем роль
        admin_id = os.getenv("ADMIN_TG_ID", "0")
        role = "admin" if str(tg_id) == admin_id else "student"

        user = User(
            tg_id=tg_id,
            name=tg_user.get("first_name", ""),
            role=role,
            subscription_active=(role in ["admin", "teacher"])
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Проверяем не истекла ли подписка
        if (user.subscription_expires and
                user.subscription_expires < datetime.utcnow() and
                user.role == "student"):
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
    """Проверяет что у студента есть активная подписка"""
    if user.role in ["admin", "teacher"]:
        return user
    if not user.subscription_active:
        raise HTTPException(status_code=402, detail="Требуется подписка")
    return user


# ── ПУБЛИЧНЫЕ РОУТЫ ──────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "UniQuiz API 🎉"}


@app.get("/me")
def get_me(user: User = Depends(get_current_user)):
    """Получить данные текущего юзера — вызывается при старте приложения"""
    return {
        "id": user.id,
        "tg_id": user.tg_id,
        "name": user.name,
        "role": user.role,
        "subscription_active": user.subscription_active,
        "subscription_expires": user.subscription_expires.isoformat() if user.subscription_expires else None,
    }


@app.get("/subjects")
def get_subjects(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    subjects = db.query(Subject).all()
    return [{"id": s.id, "title": s.title, "emoji": s.emoji} for s in subjects]


@app.get("/questions/{subject_id}")
def get_questions(
    subject_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscription)   # ← требует подписку
):
    questions = db.query(Question).filter(Question.subject_id == subject_id).all()
    output = []
    for q in questions:
        options = (
            db.query(Option)
            .filter(Option.question_id == q.id)
            .order_by(Option.order_index)
            .all()
        )
        output.append({
            "id": q.id,
            "text": q.text,
            "image_url": q.image_url,
            "options": [{"id": o.id, "text": o.text} for o in options]
        })
    return output


class SubmitResultRequest(BaseModel):
    subject_id: int
    answers: dict


@app.post("/results")
def submit_result(
    payload: SubmitResultRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_subscription)
):
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

    return {
        "score": score,
        "total": len(questions),
        "percentage": round(score / len(questions) * 100) if questions else 0
    }


@app.get("/my-results")
def my_results(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """История результатов текущего студента"""
    results = (
        db.query(Result, Subject)
        .join(Subject, Result.subject_id == Subject.id)
        .filter(Result.user_id == user.id)
        .order_by(Result.created_at.desc())
        .all()
    )
    return [
        {
            "subject": r.Subject.title,
            "emoji": r.Subject.emoji,
            "score": r.Result.score,
            "total": r.Result.total,
            "percentage": round(r.Result.score / r.Result.total * 100) if r.Result.total else 0,
            "date": r.Result.created_at.isoformat() if r.Result.created_at else None
        }
        for r in results
    ]


# ── ОПЛАТА PAYME ─────────────────────────────────────────────────────────

@app.post("/payment/create")
def create_payment(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Создать запись об оплате и вернуть ссылку на Payme"""
    if user.subscription_active and user.subscription_expires and \
       user.subscription_expires > datetime.utcnow():
        return {"message": "Подписка уже активна", "expires": user.subscription_expires.isoformat()}

    # Создаём запись подписки
    sub = Subscription(user_id=user.id, status="pending", amount=SUBSCRIPTION_PRICE)
    db.add(sub)
    db.commit()
    db.refresh(sub)

    # Формируем ссылку на Payme
    # Параметры: merchant_id, amount (в тийинах = сум * 100), order_id
    amount_tiyin = SUBSCRIPTION_PRICE * 100
    params = f"m={PAYME_MERCHANT_ID};ac.order_id={sub.id};a={amount_tiyin}"
    encoded = base64.b64encode(params.encode()).decode()
    payme_url = f"https://checkout.paycom.uz/{encoded}"

    return {
        "payment_id": sub.id,
        "amount": SUBSCRIPTION_PRICE,
        "payme_url": payme_url
    }


@app.post("/payment/payme-webhook")
async def payme_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Webhook от Payme — вызывается когда студент оплатил.
    Payme отправляет JSON-RPC запросы на этот URL.
    """
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})

    # Проверяем авторизацию от Payme
    auth = request.headers.get("Authorization", "")
    if auth:
        try:
            decoded = base64.b64decode(auth.split(" ")[1]).decode()
            _, key = decoded.split(":")
            if key != PAYME_SECRET_KEY:
                return {"error": {"code": -32504, "message": "Forbidden"}}
        except Exception:
            pass

    if method == "CheckPerformTransaction":
        order_id = params.get("account", {}).get("order_id")
        sub = db.query(Subscription).filter(Subscription.id == order_id).first()
        if not sub:
            return {"result": {"allow": False}}
        return {"result": {"allow": True}}

    elif method == "CreateTransaction":
        order_id = params.get("account", {}).get("order_id")
        sub = db.query(Subscription).filter(Subscription.id == order_id).first()
        if not sub:
            return {"error": {"code": -31050, "message": "Order not found"}}
        return {"result": {"create_time": int(datetime.utcnow().timestamp() * 1000), "transaction": str(sub.id), "state": 1}}

    elif method == "PerformTransaction":
        order_id = params.get("account", {}).get("order_id")
        sub = db.query(Subscription).filter(Subscription.id == order_id).first()
        if not sub:
            return {"error": {"code": -31050, "message": "Order not found"}}

        # Активируем подписку!
        sub.status = "paid"
        sub.paid_at = datetime.utcnow()

        user = db.query(User).filter(User.id == sub.user_id).first()
        if user:
            user.subscription_active = True
            user.subscription_expires = datetime.utcnow() + timedelta(days=SUBSCRIPTION_DAYS)

        db.commit()
        return {"result": {"transaction": str(sub.id), "perform_time": int(datetime.utcnow().timestamp() * 1000), "state": 2}}

    elif method == "CancelTransaction":
        order_id = params.get("account", {}).get("order_id")
        sub = db.query(Subscription).filter(Subscription.id == order_id).first()
        if sub:
            sub.status = "cancelled"
            db.commit()
        return {"result": {"transaction": str(sub.id) if sub else "0", "cancel_time": int(datetime.utcnow().timestamp() * 1000), "state": -1}}

    elif method == "CheckTransaction":
        order_id = params.get("account", {}).get("order_id")
        sub = db.query(Subscription).filter(Subscription.id == order_id).first()
        if not sub:
            return {"error": {"code": -31003, "message": "Transaction not found"}}
        state = 2 if sub.status == "paid" else (-1 if sub.status == "cancelled" else 1)
        return {"result": {"create_time": int(sub.created_at.timestamp() * 1000), "perform_time": int(sub.paid_at.timestamp() * 1000) if sub.paid_at else 0, "cancel_time": 0, "transaction": str(sub.id), "state": state, "reason": None}}

    return {"result": None}


# ── ПРЕПОДАВАТЕЛЬ / АДМИН РОУТЫ ──────────────────────────────────────────

@app.post("/admin/subjects")
def create_subject(
    title: str,
    emoji: str = "📚",
    db: Session = Depends(get_db),
    user: User = Depends(require_teacher)
):
    subject = Subject(title=title, emoji=emoji)
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return {"id": subject.id, "title": subject.title}


class AddQuestionRequest(BaseModel):
    subject_id: int
    text: str
    options: list[str]
    correct_index: int
    image_url: Optional[str] = None


@app.post("/admin/questions")
def add_question(
    payload: AddQuestionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_teacher)
):
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Нужно минимум 2 варианта")

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
    return {"id": q.id, "message": "Вопрос добавлен ✅"}


@app.post("/admin/upload-image")
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(require_teacher)
):
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
def get_all_results(
    db: Session = Depends(get_db),
    user: User = Depends(require_teacher)
):
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


@app.get("/admin/users")
def get_all_users(
    db: Session = Depends(get_db),
    user: User = Depends(require_admin)
):
    """Список всех пользователей (только для админа)"""
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [
        {
            "id": u.id,
            "tg_id": u.tg_id,
            "name": u.name,
            "role": u.role,
            "subscription_active": u.subscription_active,
            "subscription_expires": u.subscription_expires.isoformat() if u.subscription_expires else None,
        }
        for u in users
    ]


@app.post("/admin/set-role")
def set_role(
    tg_id: int,
    role: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin)
):
    """Назначить роль пользователю (admin/teacher/student)"""
    if role not in ["admin", "teacher", "student"]:
        raise HTTPException(status_code=400, detail="Роль должна быть: admin, teacher, student")

    target = db.query(User).filter(User.tg_id == tg_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target.role = role
    if role in ["admin", "teacher"]:
        target.subscription_active = True
    db.commit()
    return {"message": f"Роль {role} назначена ✅"}


@app.post("/admin/activate-subscription")
def activate_subscription(
    tg_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin)
):
    """Вручную активировать подписку студенту"""
    target = db.query(User).filter(User.tg_id == tg_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target.subscription_active = True
    target.subscription_expires = datetime.utcnow() + timedelta(days=days)
    db.commit()
    return {"message": f"Подписка активирована на {days} дней ✅"}
