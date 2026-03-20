from sqlalchemy import (
    Column, Integer, String, Boolean,
    ForeignKey, Text, DateTime, func
)
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False, index=True)
    name = Column(String(200))
    phone = Column(String(20), nullable=True)

    # Роли: student / teacher / admin
    role = Column(String(20), default="student")

    # Подписка
    subscription_active = Column(Boolean, default=False)
    subscription_expires = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    results = relationship("Result", back_populates="user")
    subscriptions = relationship("Subscription", back_populates="user")


class Subject(Base):
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    emoji = Column(String(10), default="📚")

    questions = relationship("Question", back_populates="subject")


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    text = Column(Text, nullable=False)
    image_url = Column(String(500), nullable=True)
    correct_option_id = Column(Integer, nullable=False)

    subject = relationship("Subject", back_populates="questions")
    options = relationship("Option", back_populates="question", cascade="all, delete")


class Option(Base):
    __tablename__ = "options"

    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    text = Column(String(500), nullable=False)
    order_index = Column(Integer, default=0)

    question = relationship("Question", back_populates="options")


class Result(Base):
    __tablename__ = "results"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    score = Column(Integer, default=0)
    total = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="results")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Статус: pending / paid / cancelled
    status = Column(String(20), default="pending")

    amount = Column(Integer, default=20000)       # сум
    payment_id = Column(String(200), nullable=True)  # ID из Payme
    created_at = Column(DateTime, server_default=func.now())
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscriptions")
