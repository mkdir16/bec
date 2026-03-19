from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from dotenv import load_dotenv
import os

load_dotenv()

# Берём URL из .env и меняем схему на синхронную
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Supabase даёт postgresql+asyncpg://... — заменяем на обычный postgresql://
DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
DATABASE_URL = DATABASE_URL.replace("asyncpg://", "postgresql://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

# Dependency для роутов FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
