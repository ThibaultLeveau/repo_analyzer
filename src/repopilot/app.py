from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


def create_app() -> FastAPI:
    app = FastAPI(title="RepoPilot")
    resources = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=resources / "static"), name="static")
    templates = Jinja2Templates(directory=resources / "templates")

    def page(request: Request, title: str) -> HTMLResponse:
        fragment = (
            request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true"
        )
        return templates.TemplateResponse(
            request=request,
            name="workspace.html" if fragment else "base.html",
            context={"title": title, "fragment": fragment},
            headers={"Vary": "HX-Request, HX-History-Restore-Request"},
        )

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        return page(request, "Overview")

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request) -> HTMLResponse:
        return page(request, "Local setup")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
