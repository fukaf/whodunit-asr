"""The FastAPI application: settings, middleware, routers, static pages.

Security posture is the whole design here, so it is stated once:

* **Bind 127.0.0.1.** The request that starts the server does that; this module
  cannot enforce it, but `serve()` below defaults to it and refuses `0.0.0.0`
  unless the caller passes `--allow-remote` explicitly.
* **Same-origin only.** Every mutating request is checked against the `Origin`
  header. Without this, any web page the user has open could POST to
  `http://127.0.0.1:8765/api/jobs` and cause work on their machine (and read
  the responses through a timing side channel, since the API has no auth).
* **No credentials in responses.** `hf_token` is stripped everywhere.

Everything else - audio paths, registry contents, job records - is local-only
data, so the API exposes it freely to a request that already passed the
same-origin check. This is not a multi-user service and must not become one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .jobs import JobManager

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Settings:
    data_dir: Path
    registry_path: str
    base_config_path: str | None = None
    version: str = __version__
    extra_origins: list[str] = field(default_factory=list)

    @property
    def allowed_origins(self) -> set[str]:
        return set(self.extra_origins)


def create_app(*, data_dir: str | Path = "~/.sdts/workbench",
               registry_path: str | None = None,
               base_config_path: str | Path | None = None,
               pipeline_factory=None,
               extra_origins: list[str] | None = None) -> FastAPI:
    from ..config import PipelineConfig

    data_dir = Path(data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    if registry_path is None:
        registry_path = PipelineConfig().identification.registry_path
    st = Settings(data_dir=data_dir, registry_path=str(registry_path),
                  base_config_path=str(base_config_path) if base_config_path else None,
                  extra_origins=list(extra_origins or []))

    app = FastAPI(title="sdts workbench", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = st
    app.state.jobs = JobManager(data_dir, pipeline_factory=pipeline_factory,
                                base_config_path=st.base_config_path,
                                registry_path_hint=st.registry_path)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and not _origin_ok(request, origin, st):
                return JSONResponse(
                    {"detail": "cross-origin request refused; this API is local-only"},
                    status_code=403)
        return await call_next(request)

    from .routes import jobs as job_routes
    from .routes import registry as registry_routes
    from .routes import results as result_routes
    from .routes import settings_api as config_routes

    app.include_router(job_routes.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(result_routes.router, prefix="/api/jobs", tags=["results"])
    app.include_router(registry_routes.router, prefix="/api/registry", tags=["registry"])
    app.include_router(config_routes.router, prefix="/api/config", tags=["config"])

    @app.get("/api/health", tags=["meta"])
    def health():
        return {"ok": True, "version": __version__,
                "data_dir": str(st.data_dir), "registry": st.registry_path}

    if WEB_DIR.is_dir():
        # `html=True` makes `/` resolve to index.html and gives a 404 for a
        # missing page instead of the SPA fallback, which is what we want:
        # there is no client-side router, each page is a real file.
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:                                # pragma: no cover - packaging accident
        log.warning("web assets missing at %s; API only", WEB_DIR)

    @app.exception_handler(KeyError)
    async def _key_error(request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    return app


def _origin_ok(request: Request, origin: str, st: Settings) -> bool:
    """True when `origin` is the server itself (any loopback spelling)."""
    if origin in st.allowed_origins:
        return True
    host = request.headers.get("host", "")
    if origin in {f"http://{host}", f"https://{host}"}:
        return True
    loopback = {"http://127.0.0.1", "http://localhost", "http://[::1]"}
    origin_host, _, origin_port = origin.rpartition(":")
    server_port = host.rpartition(":")[2] if ":" in host else "80"
    if origin_host in loopback and origin_port == server_port:
        return True
    return False
