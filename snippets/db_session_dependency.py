"""
Yield-based SQLAlchemy async session dependency.
Scopes one session per request, commits on success, rolls back on exception.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# --- Database setup ---

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/mydb")

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,       # health-check connections before use
    pool_recycle=3600,        # recycle connections after 1 hour
    echo=False,               # set True in development to log SQL
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,   # prevents lazy-load AttributeError after commit
    autoflush=False,          # explicit flush gives more control
)


# --- Models ---

class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    price: Mapped[float]


# --- Session dependency ---

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # AsyncSessionLocal()'s __aexit__ calls session.close()


# Type alias for cleaner route signatures
DbSession = Annotated[AsyncSession, Depends(get_db)]


# --- Lifespan ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify DB connectivity at startup
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    yield
    await engine.dispose()


# --- App and routes ---

app = FastAPI(lifespan=lifespan)


@app.post("/items", status_code=201)
async def create_item(name: str, price: float, db: DbSession) -> dict:
    item = Item(name=name, price=price)
    db.add(item)
    await db.flush()  # get the auto-generated ID before commit
    return {"id": item.id, "name": item.name, "price": item.price}


@app.get("/items/{item_id}")
async def get_item(item_id: int, db: DbSession) -> dict:
    item = await db.get(Item, item_id)
    if item is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Item not found")
    return {"id": item.id, "name": item.name, "price": item.price}
