import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx2
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repopilot.github import GitHubError
from repopilot.imports import ImportService, import_service
from repopilot.state import Job, JobStateError


def create_app(
    *, data_dir: Path | None = None, transport: httpx2.BaseTransport | None = None
) -> FastAPI:
    directory = data_dir or Path(
        os.environ.get("REPOPILOT_DATA_DIR", Path.home() / ".repopilot")
    )
    engine: ImportService
    csrf = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal engine
        with import_service(directory, transport=transport) as engine:
            yield

    app = FastAPI(title="RepoPilot", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
        www_redirect=False,
    )
    resources = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=resources / "static"), name="static")
    templates = Jinja2Templates(directory=resources / "templates")

    def page(
        request: Request, title: str, *, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        fragment = (
            request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true"
        )
        return templates.TemplateResponse(
            request=request,
            name="workspace.html" if fragment else "base.html",
            context={"title": title, "fragment": fragment, "csrf": csrf, **context},
            status_code=status_code,
            headers={
                "Vary": "HX-Request, HX-History-Restore-Request",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        return page(request, "Overview", jobs=engine.store.recent_imports())

    def protect(request: Request, csrf_token: Annotated[str, Form()]) -> None:
        origin = request.headers.get("Origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin is not None and origin != expected:
            raise HTTPException(403, "Cross-origin submission is unavailable")
        if not secrets.compare_digest(csrf_token.encode(), csrf.encode()):
            raise HTTPException(
                403, "The form has expired. Reload the page and try again."
            )

    def job_redirect(request: Request, job: Job) -> Response:
        url = str(request.url_for("import_status", job_id=job.id))
        if request.headers.get("HX-Request") == "true":
            return Response(headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    @app.post("/imports", dependencies=[Depends(protect)])
    def submit_import(
        request: Request, url: Annotated[str, Form(max_length=2048)]
    ) -> Response:
        try:
            job = engine.submit(url)
        except GitHubError as exc:
            return page(
                request,
                "Overview",
                error=str(exc),
                url=url,
                jobs=engine.store.recent_imports(),
                status_code=422,
            )
        return job_redirect(request, job)

    @app.get("/imports/{job_id}", response_class=HTMLResponse)
    def import_status(request: Request, job_id: int) -> HTMLResponse:
        try:
            job = engine.store.job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Import not found") from exc
        head, fetched = engine.store.source(job_id)
        stage = "Resolving repository"
        if head:
            stage = "Publishing snapshot" if fetched else "Downloading source"
        return page(request, "Repository import", job=job, head=head, stage=stage)

    @app.post("/imports/{job_id}/retry", dependencies=[Depends(protect)])
    def retry_import(request: Request, job_id: int) -> Response:
        try:
            job = engine.retry(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Import not found") from exc
        except JobStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        return job_redirect(request, job)

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request) -> HTMLResponse:
        return page(request, "Local setup")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
