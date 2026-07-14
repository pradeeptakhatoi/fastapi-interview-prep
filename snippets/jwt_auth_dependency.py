"""
JWT Bearer auth dependency with access + refresh token pattern.
Requires: pip install python-jose[cryptography] passlib[bcrypt]
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

SECRET_KEY = "change-me-in-production-use-secrets-module"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

app = FastAPI()


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    sub: str
    scopes: list[str] = []


class User(BaseModel):
    id: int
    username: str
    scopes: list[str] = []


# --- Token creation ---

def create_token(data: dict, expires_delta: timedelta) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_token_pair(user_id: str, scopes: list[str] = []) -> TokenPair:
    access = create_token(
        {"sub": user_id, "scopes": scopes, "type": "access"},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh = create_token(
        {"sub": user_id, "type": "refresh"},
        timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    return TokenPair(access_token=access, refresh_token=refresh)


# --- Auth dependency ---

async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            raise credentials_exc
        sub: str = payload.get("sub")
        if sub is None:
            raise credentials_exc
        token_data = TokenData(sub=sub, scopes=payload.get("scopes", []))
    except JWTError:
        raise credentials_exc

    # In production: fetch user from DB here
    user = User(id=int(token_data.sub), username=f"user_{token_data.sub}", scopes=token_data.scopes)
    return user


def require_scope(scope: str):
    async def _check(user: User = Depends(get_current_user)) -> User:
        if scope not in user.scopes:
            raise HTTPException(status_code=403, detail=f"Scope '{scope}' required")
        return user
    return _check


CurrentUser = Annotated[User, Depends(get_current_user)]


# --- Routes ---

@app.post("/auth/token", response_model=TokenPair)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    # Validate credentials against DB here
    # For demo: accept any username/password
    return create_token_pair(user_id="1", scopes=["read", "write"])


@app.post("/auth/refresh", response_model=TokenPair)
async def refresh(refresh_token: str) -> TokenPair:
    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise ValueError("not a refresh token")
        sub = payload["sub"]
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return create_token_pair(user_id=sub)


@app.get("/users/me")
async def me(user: CurrentUser) -> User:
    return user


@app.delete("/admin/users/{user_id}", dependencies=[Depends(require_scope("admin"))])
async def delete_user(user_id: int) -> dict:
    return {"deleted": user_id}
