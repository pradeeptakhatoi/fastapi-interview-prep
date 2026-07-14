# API Key Auth, CORS Middleware Configuration, Scopes

## Concept

**API key authentication** is simpler than OAuth2 — clients include a static secret in headers or query params. FastAPI provides `APIKeyHeader`, `APIKeyQuery`, and `APIKeyCookie` from `fastapi.security` — like `OAuth2PasswordBearer`, they extract the value and document it in OpenAPI. Validation is your code.

**CORS (Cross-Origin Resource Sharing)** is enforced by browsers for cross-origin requests. The server must respond to preflight `OPTIONS` requests with appropriate `Access-Control-*` headers. Starlette's `CORSMiddleware` handles this.

**Key CORS settings:**
- `allow_origins`: list of origins (scheme + host + port) or `["*"]` for all. `["*"]` cannot be combined with `allow_credentials=True`.
- `allow_methods`: HTTP methods allowed. Default `["GET"]`.
- `allow_headers`: headers the browser can send in requests.
- `allow_credentials`: allows cookies/authorization headers cross-origin. Cannot combine with `allow_origins=["*"]`.
- `max_age`: how long browsers can cache preflight responses (seconds).

---

## Interview Questions

### Q1: Why can't you use `allow_origins=["*"]` with `allow_credentials=True`?

**Model answer:**

The CORS spec (RFC 6454 and Fetch spec) explicitly prohibits this combination. If a server responds to a credentialed request with `Access-Control-Allow-Origin: *`, the browser rejects it — `*` is not allowed for credentialed requests per spec.

The reason: `*` means "any origin can access this resource." Allowing credentials (cookies, Authorization headers) from any origin would mean any website could make authenticated API calls on behalf of your logged-in users — defeating the purpose of CORS protection.

With credentials, you must specify exact origins:
```python
CORSMiddleware(
    app,
    allow_origins=["https://app.example.com", "https://admin.example.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
```

Starlette's `CORSMiddleware` raises a `ValueError` at startup if you attempt this combination.

**Gotcha follow-up:** Your frontend is at `https://app.example.com` but you also need to allow `http://localhost:3000` for local development. What's the correct approach?

Use environment-based configuration:
```python
import os
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
# In production env: CORS_ORIGINS=https://app.example.com
```

Never hardcode `localhost` in production CORS config — it allows any local machine's browser to make credentialed cross-origin requests to your production API.

---

### Q2: How do you implement API key authentication that supports multiple keys per tenant?

**Model answer:**

```python
from fastapi import Security, HTTPException, status, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class Tenant(BaseModel):
    id: str
    name: str
    scopes: list[str]


# In production: store in DB with hashed keys
VALID_KEYS: dict[str, Tenant] = {
    "key_prod_abc123": Tenant(id="t1", name="Acme Corp", scopes=["read", "write"]),
    "key_prod_def456": Tenant(id="t2", name="Globex", scopes=["read"]),
}


async def get_tenant(api_key: str | None = Security(api_key_header)) -> Tenant:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    tenant = VALID_KEYS.get(api_key)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return tenant


def require_scope(scope: str):
    async def _check(tenant: Tenant = Depends(get_tenant)) -> Tenant:
        if scope not in tenant.scopes:
            raise HTTPException(status_code=403, detail=f"Scope '{scope}' required")
        return tenant
    return _check
```

**Security note:** compare API keys using `hmac.compare_digest()` (constant-time comparison) to prevent timing attacks:

```python
import hmac

async def get_tenant(api_key: str | None = Security(api_key_header)) -> Tenant:
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    # Constant-time comparison to prevent timing attacks
    for stored_key, tenant in VALID_KEYS.items():
        if hmac.compare_digest(api_key.encode(), stored_key.encode()):
            return tenant
    
    raise HTTPException(status_code=401, detail="Invalid API key")
```

---

### Q3: How do you support both API key and JWT auth on the same endpoint (multiple auth schemes)?

**Model answer:**

Use `auto_error=False` on both extractors and combine them in a dependency:

```python
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_principal(
    token: str | None = Depends(oauth2),
    api_key: str | None = Depends(api_key_header),
) -> Principal:
    if token:
        return validate_jwt(token)
    if api_key:
        return validate_api_key(api_key)
    raise HTTPException(status_code=401, detail="Authentication required")
```

`auto_error=False` means neither extractor raises if its credential is absent — it returns `None`. Your combining dependency then tries each in priority order.

The OpenAPI schema will show both security schemes. You can use `Security(get_principal)` in routes to document that either scheme is accepted.

---

## Code: Production CORS + API Key Setup

```python
import hmac
import os
from fastapi import FastAPI, Security, Depends, HTTPException, status, Request
from fastapi.security import APIKeyHeader
from starlette.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS — configure before adding auth middleware (CORS must be outer)
allowed_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:8080"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allowed_origins],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-RateLimit-Remaining"],
    max_age=600,  # browsers cache preflight for 10 minutes
)

# API Key auth
api_key_header = APIKeyHeader(name="X-API-Key")

VALID_API_KEYS = {
    "key_live_abc123": {"tenant": "acme", "scopes": ["read", "write"]},
}


async def require_api_key(
    api_key: str = Security(api_key_header),
) -> dict:
    # Constant-time comparison to prevent timing attacks
    for valid_key, metadata in VALID_API_KEYS.items():
        if hmac.compare_digest(api_key.encode("utf-8"), valid_key.encode("utf-8")):
            return metadata
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
    )


@app.get("/data/", dependencies=[Depends(require_api_key)])
async def get_data() -> dict:
    return {"data": []}


@app.post("/data/")
async def create_data(auth: dict = Depends(require_api_key)) -> dict:
    if "write" not in auth["scopes"]:
        raise HTTPException(status_code=403, detail="Write scope required")
    return {"created": True, "tenant": auth["tenant"]}
```

---

## Under the Hood

Starlette's `CORSMiddleware` is a raw ASGI middleware (not `BaseHTTPMiddleware`). For preflight `OPTIONS` requests, it short-circuits and returns a response without calling the inner app. For actual requests, it appends CORS headers to the response. The middleware reads `Origin` from the request headers and checks it against `allow_origins`. If using `allow_origins=["*"]` (non-credentialed), it echoes back `Access-Control-Allow-Origin: *`. If using specific origins, it echoes back the requesting origin (only if it's in the allowed list) — this is why you see `Vary: Origin` in CORS responses: different origins get different responses, so CDN/proxy caches must vary by origin.
