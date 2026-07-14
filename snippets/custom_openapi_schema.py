"""
Programmatic OpenAPI schema override.
Demonstrates: adding security schemes, hiding internal routes,
adding custom extensions, and modifying generated schema.
"""

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute


def build_custom_openapi(app: FastAPI) -> dict:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title="My Production API",
        version="1.0.0",
        description=(
            "Public API.\n\n"
            "**Authentication:** Bearer JWT via `/auth/token`.\n\n"
            "**Rate limits:** 60 req/s sustained, burst 100."
        ),
        routes=app.routes,
        servers=[
            {"url": "https://api.example.com", "description": "Production"},
            {"url": "https://staging-api.example.com", "description": "Staging"},
        ],
    )

    # Add security schemes
    schema.setdefault("components", {})
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Obtain via POST /auth/token",
        },
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        },
    }

    # Apply BearerAuth globally (override per-route with security: [] to opt-out)
    schema["security"] = [{"BearerAuth": []}]

    # Remove internal/health routes from public docs
    paths_to_hide = {"/health", "/metrics", "/internal/debug"}
    for path in list(schema.get("paths", {}).keys()):
        if path in paths_to_hide or path.startswith("/internal/"):
            del schema["paths"][path]

    # Add custom x- extensions for documentation tooling
    schema["info"]["x-logo"] = {"url": "https://example.com/logo.png"}
    schema["info"]["x-ratelimit"] = {"burst": 100, "sustained": 60}

    # Mark deprecated routes
    for path, path_item in schema.get("paths", {}).items():
        if "/v1/" in path:
            for method in ("get", "post", "put", "patch", "delete"):
                if method in path_item:
                    path_item[method]["deprecated"] = True

    # Fix up operation IDs to be cleaner (FastAPI generates verbose ones)
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                # Strip the function name suffix FastAPI adds (e.g., "_get_items__items__get")
                op_id = operation["operationId"]
                operation["operationId"] = op_id.rsplit("_", 2)[0]

    app.openapi_schema = schema
    return schema


# --- Usage ---

app = FastAPI()
app.openapi = lambda: build_custom_openapi(app)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}  # hidden from docs


@app.get("/items/")
async def list_items() -> list:
    return []


@app.post("/auth/token")
async def login() -> dict:
    return {"access_token": "...", "token_type": "bearer"}
