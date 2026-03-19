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
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    results = relationship("Result", back_populates="user")


class Subject(Base):
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)   # "Математика", "Физика"
    emoji = Column(String(10), default="📚")

    questions = relationship("Question", back_populates="subject")


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    text = Column(Text, nullable=False)
    image_url = Column(String(500), nullable=True)   # ссылка на картинку (опционально)
    correct_option_id = Column(Integer, nullable=False)  # номер правильного ответа (0-3)

    subject = relationship("Subject", back_populates="questions")
    options = relationship("Option", back_populates="question", cascade="all, delete")


class Option(Base):
    __tablename__ = "options"

    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    text = Column(String(500), nullable=False)
    order_index = Column(Integer, default=0)  # 0, 1, 2, 3

    question = relationship("Question", back_populates="options")


class Result(Base):
    __tablename__ = "results"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    score = Column(Integer, default=0)        # сколько правильных
    total = Column(Integer, default=0)        # всего вопросов
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="results")
