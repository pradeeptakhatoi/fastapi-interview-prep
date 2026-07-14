# OAuth2PasswordBearer, JWT Implementation, Refresh Token Patterns

## Concept

FastAPI's `OAuth2PasswordBearer` is a dependency that:
1. Declares `securitySchemes: {bearerAuth: {type: http, scheme: bearer}}` in the OpenAPI schema
2. Extracts the `Authorization: Bearer <token>` header from the request
3. Returns the token string (does NOT validate it — that's your job)

It's a documentation + extraction helper, not a full auth system. JWT validation, user lookup, and scope enforcement happen in the dependency that uses `OAuth2PasswordBearer` as a sub-dependency.

**Token patterns:**
- **Access token**: short-lived (15min–1h), sent with every request, used for authorization
- **Refresh token**: long-lived (7–30 days), sent only to `/auth/refresh`, used to get new access tokens without re-authentication
- **Rotation**: on refresh, invalidate the old refresh token and issue a new one (prevents replay attacks)

JWT structure: `header.payload.signature` — base64url encoded, signed with HMAC-SHA256 (symmetric) or RS256 (asymmetric). FastAPI has no opinion on which — choose based on whether multiple services need to verify tokens independently.

---

## Interview Questions

### Q1: Why does `OAuth2PasswordBearer` not actually validate the JWT?

**Model answer:**

`OAuth2PasswordBearer` is a Starlette `SecurityBase` subclass. Its only job is to:
1. Add the OAuth2 password flow security scheme to the OpenAPI spec
2. Extract and return the Bearer token string from the request header

Token validation is application-specific: different algorithms (HS256, RS256), different payload structures, different error handling, different user stores. FastAPI doesn't impose any of this. `OAuth2PasswordBearer` is the extraction hook; you write the validation.

```python
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

# This just extracts the string — no validation
async def get_token(token: str = Depends(oauth2_scheme)) -> str:
    return token  # "eyJhbG..."

# This validates it
async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, ...)
    return await get_user_from_db(payload["sub"])
```

**Gotcha follow-up:** What happens if the `Authorization` header is missing?

`OAuth2PasswordBearer` raises `HTTPException(status_code=401, detail="Not authenticated")` automatically. The `auto_error=False` parameter makes it return `None` instead — useful for optional auth (public endpoints that show more data when authenticated).

---

### Q2: What's the correct pattern for refresh token rotation and why?

**Model answer:**

**Without rotation:** a stolen refresh token can be used indefinitely until it expires. If the refresh token is valid for 30 days and is compromised on day 1, the attacker has 29 days of access.

**With rotation (recommended):**
1. On `POST /auth/refresh` with a valid refresh token:
   - Issue new access token + new refresh token
   - Invalidate the old refresh token (store in DB or Redis blocklist)
2. If an old (invalidated) refresh token is used:
   - Revoke ALL refresh tokens for that user (token reuse detected → assumed compromise)

```python
# Redis-backed refresh token storage
async def create_refresh_token(user_id: str, redis: aioredis.Redis) -> str:
    token_id = str(uuid.uuid4())
    token = jwt.encode(
        {"sub": user_id, "jti": token_id, "type": "refresh", 
         "exp": datetime.now(UTC) + timedelta(days=7)},
        SECRET_KEY, algorithm="HS256"
    )
    # Store token_id in Redis with TTL
    await redis.setex(f"refresh:{user_id}:{token_id}", 7 * 86400, "valid")
    return token

async def rotate_refresh_token(old_token: str, redis: aioredis.Redis) -> tuple[str, str]:
    try:
        payload = jwt.decode(old_token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    
    user_id = payload["sub"]
    token_id = payload["jti"]
    key = f"refresh:{user_id}:{token_id}"
    
    existing = await redis.get(key)
    if not existing:
        # Token reuse detected — invalidate all user sessions
        await redis.delete(*await redis.keys(f"refresh:{user_id}:*"))
        raise HTTPException(status_code=401, detail="Refresh token reuse detected")
    
    # Invalidate old token
    await redis.delete(key)
    
    # Issue new pair
    new_refresh = await create_refresh_token(user_id, redis)
    new_access = create_access_token(user_id)
    return new_access, new_refresh
```

**Gotcha follow-up:** Why store token IDs (`jti`) instead of the full token?

The `jti` (JWT ID) is a compact identifier. Storing full JWT strings in Redis wastes space (a JWT is ~200-500 bytes vs a UUID at 36 bytes). You also shouldn't store sensitive token strings in a database that might be logged.

---

### Q3: When would you use RS256 instead of HS256 for JWT signing?

**Model answer:**

**HS256 (HMAC-SHA256):** symmetric — same secret key signs and verifies. Any service that can verify tokens can also forge them.

**RS256 (RSA-SHA256):** asymmetric — private key signs, public key verifies. Services can verify tokens without being able to create them.

**Use RS256 when:**
- Multiple microservices need to verify tokens but should NOT be able to issue them
- The private key lives in one auth service; each microservice has the public key
- Tokens need to be verified by external parties (customer integrations, third-party APIs)
- Tokens need to be inspectable without trust (public key infrastructure)

**Use HS256 when:**
- Single service issues and verifies tokens
- Simplicity and performance matter (HS256 is faster than RS256)
- The service can be trusted with the signing secret

In FastAPI:
```python
from cryptography.hazmat.primitives import serialization
from jose import jwt

# RS256 signing (auth service)
with open("private_key.pem", "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)

token = jwt.encode({"sub": "user123"}, private_key, algorithm="RS256")

# RS256 verification (any service with public key)
with open("public_key.pem", "rb") as f:
    public_key = f.read()

payload = jwt.decode(token, public_key, algorithms=["RS256"])
```

---

## Code: Complete Auth Flow with Scopes

```python
from datetime import datetime, timedelta, timezone
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm, SecurityScopes
from jose import JWTError, jwt
from pydantic import BaseModel

SECRET_KEY = "use-secrets-module-in-production"
ALGORITHM = "HS256"

app = FastAPI()
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/token",
    scopes={
        "read": "Read access",
        "write": "Write access",
        "admin": "Full admin access",
    },
)


class TokenPayload(BaseModel):
    sub: str
    scopes: list[str] = []


def create_access_token(sub: str, scopes: list[str]) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "scopes": scopes,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


async def get_current_user(
    security_scopes: SecurityScopes,
    token: str = Depends(oauth2_scheme),
) -> TokenPayload:
    if security_scopes.scopes:
        authenticate_value = f'Bearer scope="{security_scopes.scope_str}"'
    else:
        authenticate_value = "Bearer"

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": authenticate_value},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub: str = payload.get("sub")
        if sub is None:
            raise credentials_exception
        token_data = TokenPayload(sub=sub, scopes=payload.get("scopes", []))
    except JWTError:
        raise credentials_exception

    for scope in security_scopes.scopes:
        if scope not in token_data.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Scope '{scope}' required",
                headers={"WWW-Authenticate": authenticate_value},
            )
    return token_data


# Type aliases for clean route signatures
ReadUser = Annotated[TokenPayload, Security(get_current_user, scopes=["read"])]
WriteUser = Annotated[TokenPayload, Security(get_current_user, scopes=["write"])]
AdminUser = Annotated[TokenPayload, Security(get_current_user, scopes=["admin"])]


@app.post("/auth/token")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    # Validate credentials; assign appropriate scopes
    scopes = ["read"]
    if form.username == "admin":
        scopes = ["read", "write", "admin"]
    return {
        "access_token": create_access_token(form.username, scopes),
        "token_type": "bearer",
    }


@app.get("/items/")
async def list_items(user: ReadUser) -> list:
    return []


@app.post("/items/")
async def create_item(user: WriteUser) -> dict:
    return {"created": True}


@app.delete("/items/{item_id}")
async def delete_item(item_id: int, user: AdminUser) -> dict:
    return {"deleted": item_id}
```

---

## Under the Hood

`OAuth2PasswordBearer` inherits from `fastapi.security.OAuth2` which inherits from `SecurityBase`. At route registration, FastAPI detects `SecurityBase` subclasses in the dependency tree and adds their `model.flows` (OAuth2 flows description) to the OpenAPI `securitySchemes` component. This is purely for documentation — the security enforcement is in your dependency code.

`SecurityScopes` is a FastAPI-specific class (not from `python-jose`). When injected into a dependency, it receives the `scopes` list from the `Security(dep, scopes=[...])` call in the route that invoked this dependency. This allows the same auth dependency to know what scopes the current route requires, enabling scope checking in a single place rather than per-route.
