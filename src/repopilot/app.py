import os
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx2
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repopilot.architecture import GROUP_SIZE, Architecture, PackageTree
from repopilot.dependencies import DependencyReport, analyze_dependencies
from repopilot.explorer import PAGE_LINES, browse
from repopilot.github import GitHubError, RateLimited
from repopilot.grounding import StructuredModel
from repopilot.guide import Guide, Guides
from repopilot.imports import ImportService, import_service
from repopilot.issues import Action, Cooldown, IssuePages, QueueBusy
from repopilot.search import QUERY_LIMIT, LexicalSearch
from repopilot.snapshots import SnapshotError
from repopilot.state import Job, JobStateError

if TYPE_CHECKING:
    from repopilot.ask import Ask
    from repopilot.vectors import Embedder, VectorIndex


def create_app(
    *,
    data_dir: Path | None = None,
    transport: httpx2.BaseTransport | None = None,
    model: StructuredModel | None = None,
    embedder: "Embedder | None" = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    directory = data_dir or Path(
        os.environ.get("REPOPILOT_DATA_DIR", Path.home() / ".repopilot")
    )
    engine: ImportService
    architecture: Architecture
    guides: Guides
    issue_pages: IssuePages
    csrf = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal engine, architecture, guides, issue_pages
        with import_service(directory, transport=transport) as engine:
            architecture = Architecture(engine.cache, engine.store, model)
            guides = Guides(architecture)
            issue_pages = IssuePages(directory, engine.client, clock)
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
            context={
                "title": title,
                "fragment": fragment,
                "csrf": csrf,
                "query_limit": QUERY_LIMIT,
                **context,
            },
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

    def issue_repo(owner: str, repo: str, sha: str) -> str:
        repository = f"{owner}/{repo}"
        try:
            engine.cache.open(repository, sha)
        except (FileNotFoundError, SnapshotError, GitHubError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        return repository

    def issue_page(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        label: str = "",
        number: int | None = None,
        error: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        repository = issue_repo(owner, repo, sha)
        queue = issue_pages.read(repository)
        items = [i for i in queue.issues if not label or label in i.labels]
        selected = (
            next((i for i in items if i.number == number), None)
            if number is not None
            else next(iter(items), None)
        )
        if number is not None and selected is None:
            raise HTTPException(404, "Issue not found in the current selection")
        now = clock()
        paths = {"owner": owner, "repo": repo, "sha": sha}
        next_time = None
        if now < queue.next_at:
            try:
                next_time = datetime.fromtimestamp(queue.next_at, UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            except (OverflowError, OSError, ValueError):
                next_time = "beyond the supported calendar range"
        return page(
            request,
            "Issues",
            repository=repository,
            sha=sha,
            status_code=status_code,
            queue=queue,
            items=items,
            selected=selected,
            label=label,
            error=error,
            labels=sorted({name for item in queue.issues for name in item.labels}),
            next_time=next_time,
            limited=now < queue.retry_at,
            issue_url=request.url_for("issues_view", **paths),
            actions_url=request.url_for("update_issues", **paths),
            interval_url=request.url_for("issue_interval", **paths),
            explorer_url=request.url_for("explorer", **paths),
        )

    issue_path = "/repos/{owner}/{repo}/{sha}/issues"

    @app.get(issue_path, response_class=HTMLResponse)
    def issues_view(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        label: str = "",
        number: int | None = None,
    ) -> HTMLResponse:
        return issue_page(request, owner, repo, sha, label, number)

    @app.post(issue_path + "/load", dependencies=[Depends(protect)])
    def update_issues(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        action: Annotated[Action, Form()],
    ) -> Response:
        repository = issue_repo(owner, repo, sha)
        try:
            issue_pages.load(repository, action)
        except (QueueBusy, Cooldown) as exc:
            return issue_page(
                request, owner, repo, sha, error=str(exc), status_code=409
            )
        except RateLimited as exc:
            return issue_page(
                request, owner, repo, sha, error=str(exc), status_code=429
            )
        except GitHubError as exc:
            return issue_page(
                request, owner, repo, sha, error=str(exc), status_code=502
            )
        return RedirectResponse(
            request.url_for("issues_view", owner=owner, repo=repo, sha=sha),
            status_code=303,
        )

    @app.post(issue_path + "/interval", dependencies=[Depends(protect)])
    def issue_interval(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        hours: Annotated[str, Form(max_length=32)],
    ) -> Response:
        repository = issue_repo(owner, repo, sha)
        try:
            issue_pages.set_interval(repository, float(hours))
        except ValueError:
            return issue_page(
                request,
                owner,
                repo,
                sha,
                error="Enter a positive, finite number of hours.",
                status_code=422,
            )
        return RedirectResponse(
            request.url_for("issues_view", owner=owner, repo=repo, sha=sha),
            status_code=303,
        )

    @lru_cache(maxsize=4)
    def dependency_report(repository: str, sha: str) -> DependencyReport:
        return analyze_dependencies(engine.cache.open(repository, sha))

    @app.get("/repos/{owner}/{repo}/{sha}/dependencies", response_class=HTMLResponse)
    def dependencies(
        request: Request, owner: str, repo: str, sha: str, package: str = ""
    ) -> HTMLResponse:
        repository = f"{owner}/{repo}"
        try:
            report = dependency_report(repository, sha)
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        if package and package not in report.packages:
            raise HTTPException(404, "Dependency not found")
        selected = (
            report.packages.get(package)
            if package
            else next(iter(report.packages.values()), None)
        )
        paths = {"owner": owner, "repo": repo, "sha": sha}
        return page(
            request,
            "Dependencies",
            repository=repository,
            sha=sha,
            report=report,
            selected=selected,
            explorer_url=request.url_for("explorer", **paths),
            dependencies_url=request.url_for("dependencies", **paths),
        )

    @app.get("/repos/{owner}/{repo}/{sha}/architecture", response_class=HTMLResponse)
    def architecture_view(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        path: str = "",
        notice: str = "",
    ) -> HTMLResponse:
        repository = f"{owner}/{repo}"
        try:
            roles = architecture.roles(repository, sha)
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        selected = next((r for r in roles if r.path == path), None)
        if path and selected is None:
            raise HTTPException(404, "Module not found")
        if not selected and roles:
            selected = roles[0]
        start = roles.index(selected) // GROUP_SIZE * GROUP_SIZE if selected else 0
        paths = {"owner": owner, "repo": repo, "sha": sha}
        return page(
            request,
            "Architecture",
            repository=repository,
            sha=sha,
            explorer_url=request.url_for("explorer", **paths),
            architecture_url=request.url_for("architecture_view", **paths),
            generate_url=request.url_for("generate_roles", **paths),
            tree=PackageTree.build(roles),
            roles=roles[start : start + GROUP_SIZE],
            selected=selected,
            notice=notice,
            diagnostics=architecture.inventory(repository, sha)[1],
            sources=architecture.evidence(repository, sha, selected)
            if selected
            else [],
        )

    @app.post(
        "/repos/{owner}/{repo}/{sha}/architecture/descriptions",
        dependencies=[Depends(protect)],
    )
    def generate_roles(
        request: Request, owner: str, repo: str, sha: str, path: Annotated[str, Form()]
    ) -> Response:
        try:
            status = architecture.generate(f"{owner}/{repo}", sha, path)
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Module or snapshot not found") from exc
        target = request.url_for("architecture_view", owner=owner, repo=repo, sha=sha)
        return RedirectResponse(
            str(target.include_query_params(path=path, notice=status)), status_code=303
        )

    def snapshot_guide(owner: str, repo: str, sha: str) -> Guide:
        try:
            return guides.get(f"{owner}/{repo}", sha)
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc

    guide_path = "/repos/{owner}/{repo}/{sha}/getting-started"

    @app.get(guide_path, response_class=HTMLResponse)
    def getting_started(
        request: Request, owner: str, repo: str, sha: str, notice: str = ""
    ) -> HTMLResponse:
        guide = snapshot_guide(owner, repo, sha)
        paths = {"owner": owner, "repo": repo, "sha": sha}
        return page(
            request,
            "Getting Started",
            repository=guide.repository,
            sha=sha,
            guide=guide,
            notice=notice,
            explorer_url=request.url_for("explorer", **paths),
            download_url=request.url_for("download_guide", **paths),
            generate_url=request.url_for("generate_guide", **paths),
        )

    @app.post(guide_path + "/notes", dependencies=[Depends(protect)])
    def generate_guide(request: Request, owner: str, repo: str, sha: str) -> Response:
        try:
            status = guides.generate(f"{owner}/{repo}", sha)
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        target = request.url_for("getting_started", owner=owner, repo=repo, sha=sha)
        return RedirectResponse(
            str(target.include_query_params(notice=status)), status_code=303
        )

    @app.get(guide_path + ".md")
    def download_guide(owner: str, repo: str, sha: str) -> Response:
        return Response(
            snapshot_guide(owner, repo, sha).markdown(),
            media_type="text/markdown",
            headers={
                "Content-Disposition": 'attachment; filename="getting-started.md"'
            },
        )

    @lru_cache(maxsize=2)
    def lexical_index(repository: str, sha: str) -> LexicalSearch:
        return LexicalSearch(engine.cache.open(repository, sha))

    @lru_cache(maxsize=1)
    def vector_index() -> "VectorIndex":
        from repopilot.vectors import VectorIndex

        return VectorIndex(directory, engine.cache, embedder)

    @app.post(
        "/repos/{owner}/{repo}/{sha}/search/index", dependencies=[Depends(protect)]
    )
    def build_index(request: Request, owner: str, repo: str, sha: str) -> Response:
        from repopilot.vectors import VectorError

        try:
            count = vector_index().build(f"{owner}/{repo}", sha)
            notice = f"Semantic index ready with {count} source chunks."
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        except VectorError as exc:
            notice = str(exc)
        url = request.url_for("search_view", owner=owner, repo=repo, sha=sha)
        return RedirectResponse(
            str(url.include_query_params(notice=notice)), status_code=303
        )

    @app.get("/repos/{owner}/{repo}/{sha}/search", response_class=HTMLResponse)
    def search_view(
        request: Request, owner: str, repo: str, sha: str, q: str = "", notice: str = ""
    ) -> HTMLResponse:
        repository = f"{owner}/{repo}"
        paths = {"owner": owner, "repo": repo, "sha": sha}
        try:
            engine.cache.open(repository, sha)
        except (FileNotFoundError, GitHubError, SnapshotError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        error = ""
        hits = []
        if len(q) > QUERY_LIMIT:
            error = f"Enter at most {QUERY_LIMIT} characters."
        elif q.strip():
            hits = lexical_index(repository, sha).search(q)
        return page(
            request,
            "Search",
            status_code=422 if error else 200,
            repository=repository,
            sha=sha,
            explorer_url=request.url_for("explorer", **paths),
            query=q[:QUERY_LIMIT],
            index_url=request.url_for("build_index", **paths),
            index_notice=notice,
            hits=hits,
            error=error,
        )

    @lru_cache(maxsize=1)
    def questions() -> "Ask":
        from repopilot.ask import Ask

        return Ask(engine.cache, engine.store, vector_index(), model)

    @app.post("/repos/{owner}/{repo}/{sha}/ask", dependencies=[Depends(protect)])
    def ask_question(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        question: Annotated[str, Form()],
        path: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            answer_id = questions().run(f"{owner}/{repo}", sha, question, path)
        except (FileNotFoundError, SnapshotError, GitHubError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        url = request.url_for("explorer", owner=owner, repo=repo, sha=sha)
        location = (
            str(url.include_query_params(path=path, answer_id=answer_id)) + "#ask"
        )
        if request.headers.get("HX-Request") == "true":
            return Response(headers={"HX-Redirect": location})
        return RedirectResponse(location, status_code=303)

    @app.get("/repos/{owner}/{repo}/{sha}/code", response_class=HTMLResponse)
    def explorer(
        request: Request,
        owner: str,
        repo: str,
        sha: str,
        path: str = "",
        line: int | None = None,
        end: int | None = None,
        answer_id: int | None = None,
    ) -> HTMLResponse:
        repository = f"{owner}/{repo}"
        url = request.url_for("explorer", owner=owner, repo=repo, sha=sha)
        try:
            snapshot = engine.cache.open(repository, sha)
        except (FileNotFoundError, SnapshotError, GitHubError):
            return page(
                request, "Code Explorer", error="Snapshot not found.", status_code=404
            )
        try:
            view = browse(snapshot, path, line, end)
        except FileNotFoundError:
            return page(
                request,
                "Code Explorer",
                error="File not found in this snapshot.",
                explorer_url=url,
                status_code=404,
            )
        except ValueError as exc:
            return page(
                request,
                "Code Explorer",
                error=str(exc),
                explorer_url=url,
                status_code=422,
            )
        saved = None
        if answer_id is not None:
            try:
                saved = questions().saved(repository, sha, answer_id)
            except KeyError as exc:
                raise HTTPException(404, "Answer not found in this snapshot") from exc
        return page(
            request,
            "Code Explorer",
            repository=repository,
            saved=saved,
            ask_url=request.url_for("ask_question", owner=owner, repo=repo, sha=sha),
            sha=sha,
            explorer_url=url,
            view=view,
            path=path,
            line=line,
            end=end,
            page_lines=PAGE_LINES,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
