import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import (
    check_password,
    clear_session_cookie,
    display_login,
    encrypted_login,
    end_all_sessions,
    end_session,
    find_user,
    guest_id_from_request,
    hash_password,
    set_guest_cookie,
    set_session_cookie,
    start_session,
    user_from_request,
    validate_login,
    validate_password,
)
from app.cleanup import maybe_run_cleanup, run_cleanup
from app.crypto import decrypt_text, encrypt_text, validate_crypto_config
from app.db import check_db, get_session, init_db, session_scope
from app.models import Task, User
from app.schemas import TaskCreate, TaskOut
from app.security import BodyLimitMiddleware, OriginCheckMiddleware, RateLimitMiddleware, security_headers, wants_json

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_MAX_TASKS = 500

DbSession = Annotated[Session, Depends(get_session)]


@cache
def static_url(name: str) -> str:
    digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_url"] = static_url


def max_tasks_per_owner() -> int:
    try:
        return max(1, int(os.environ.get("MAX_TASKS_PER_OWNER", DEFAULT_MAX_TASKS)))
    except ValueError:
        return DEFAULT_MAX_TASKS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_crypto_config()
    init_db()
    with session_scope() as db:
        run_cleanup(db)
    yield


app = FastAPI(title="Список задач", lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(RateLimitMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    security_headers(request, response)
    return response


def _owner(request: Request, db: Session) -> tuple[User | None, uuid.UUID | None]:
    user = user_from_request(request, db)
    if user is not None:
        return user, None
    return None, guest_id_from_request(request)


def _owner_condition(user: User | None, guest_id: uuid.UUID | None):
    if user is not None:
        return Task.user_id == user.id
    if guest_id is not None:
        return (Task.guest_id == guest_id) & Task.user_id.is_(None)
    return None


def _task_out(task: Task, title: str | None = None) -> TaskOut:
    return TaskOut(
        id=task.id,
        title=title if title is not None else decrypt_text(task.title),
        done=task.done,
        created_at=task.created_at,
    )


def _task_payload(task: Task, title: str | None = None) -> dict:
    return _task_out(task, title).model_dump(mode="json")


def _tasks_for(db: Session, user: User | None, guest_id: uuid.UUID | None) -> list[TaskOut]:
    condition = _owner_condition(user, guest_id)
    if condition is None:
        return []
    rows = db.scalars(select(Task).where(condition).order_by(Task.created_at.asc(), Task.id.asc()))
    return [_task_out(task) for task in rows]


def _count_tasks(db: Session, user: User | None, guest_id: uuid.UUID | None) -> int:
    condition = _owner_condition(user, guest_id)
    if condition is None:
        return 0
    return db.scalar(select(func.count()).select_from(Task).where(condition)) or 0


def _owned_task(db: Session, task_id: uuid.UUID, user: User | None, guest_id: uuid.UUID | None) -> Task:
    task = db.get(Task, task_id)
    if task is not None:
        if user is not None and task.user_id == user.id:
            return task
        if user is None and guest_id is not None and task.guest_id == guest_id and task.user_id is None:
            return task
    raise HTTPException(status_code=404, detail="Задача не найдена.")


def _claim_guest_tasks(db: Session, request: Request, user: User) -> None:
    guest_id = guest_id_from_request(request)
    if guest_id is None:
        return
    for task in db.scalars(select(Task).where(Task.guest_id == guest_id, Task.user_id.is_(None))).all():
        task.user_id = user.id
        task.guest_id = None


def _remember_guest(response, request: Request, guest_id: uuid.UUID | None) -> None:
    if guest_id is not None and guest_id_from_request(request) is None:
        set_guest_cookie(response, guest_id)


def _page(request: Request, db: Session, error: str | None = None, status_code: int = 200):
    user, guest_id = _owner(request, db)
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "tasks": _tasks_for(db, user, guest_id),
            "error": error,
            "current_user": {"login": display_login(user)} if user else None,
        },
        status_code=status_code,
    )
    if user is None and guest_id is None:
        set_guest_cookie(response, uuid.uuid4())
    return response


async def _read_payload(request: Request, fields: tuple[str, ...], error: str) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=error) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail=error)
    else:
        payload = await request.form()
    values = {name: payload.get(name, "") for name in fields}
    if not all(isinstance(value, str) for value in values.values()):
        raise HTTPException(status_code=422, detail=error)
    return values


async def _read_credentials(request: Request) -> tuple[str, str]:
    values = await _read_payload(request, ("login", "password"), "Введите логин и пароль.")
    try:
        return validate_login(values["login"]), validate_password(values["password"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _read_title(request: Request) -> str:
    values = await _read_payload(request, ("title",), "Введите название задачи.")
    try:
        return TaskCreate(title=values["title"]).title
    except ValidationError as exc:
        message = exc.errors()[0]["msg"].removeprefix("Value error, ")
        raise HTTPException(status_code=422, detail=message) from exc


def _auth_response(db: Session, user: User, status_code: int = 200) -> JSONResponse:
    token = start_session(db, user)
    db.commit()
    response = JSONResponse({"login": display_login(user)}, status_code=status_code)
    set_session_cookie(response, token)
    return response


def _done(request: Request, payload: dict | None = None, status_code: int = 200):
    if wants_json(request):
        return JSONResponse(payload if payload is not None else {"ok": True}, status_code=status_code)
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health() -> JSONResponse:
    try:
        check_db()
    except (SQLAlchemyError, RuntimeError, OSError):
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/")
def index(request: Request, db: DbSession):
    return _page(request, db)


@app.get("/auth/me")
def auth_me(request: Request, db: DbSession):
    user = user_from_request(request, db)
    if user is None:
        return JSONResponse({"login": None}, status_code=401)
    return {"login": display_login(user)}


@app.post("/auth/register")
async def register(request: Request, db: DbSession):
    login, password = await _read_credentials(request)
    lookup, login_enc = encrypted_login(login)
    user = User(login=lookup, login_enc=login_enc, password_hash=hash_password(password), session_version=0)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Пользователь с таким логином уже существует.") from None
    _claim_guest_tasks(db, request, user)
    maybe_run_cleanup(db)
    return _auth_response(db, user, status_code=201)


@app.post("/auth/login")
async def login(request: Request, db: DbSession):
    login_name, password = await _read_credentials(request)
    user = find_user(db, login_name)
    stored = user.password_hash if user is not None else None
    if not check_password(password, stored) or user is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")
    _claim_guest_tasks(db, request, user)
    maybe_run_cleanup(db)
    return _auth_response(db, user)


@app.post("/auth/logout")
def logout(request: Request, db: DbSession):
    end_session(request, db)
    db.commit()
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@app.post("/auth/logout-all")
def logout_all(request: Request, db: DbSession):
    user = user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт.")
    end_all_sessions(db, user)
    db.commit()
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@app.get("/tasks")
def list_tasks(request: Request, db: DbSession):
    user, guest_id = _owner(request, db)
    return [task.model_dump(mode="json") for task in _tasks_for(db, user, guest_id)]


@app.post("/tasks")
async def create_task(request: Request, db: DbSession):
    title = await _read_title(request)
    user, guest_id = _owner(request, db)
    if user is None and guest_id is None:
        guest_id = uuid.uuid4()
    limit = max_tasks_per_owner()
    if _count_tasks(db, user, guest_id) >= limit:
        raise HTTPException(status_code=409, detail=f"Достигнут лимит: не больше {limit} задач.")
    task = Task(
        title=encrypt_text(title),
        user_id=user.id if user else None,
        guest_id=None if user else guest_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    maybe_run_cleanup(db)
    response = _done(request, _task_payload(task, title), status_code=201)
    if user is None:
        _remember_guest(response, request, guest_id)
    return response


@app.post("/tasks/{task_id}/edit")
async def edit_task(task_id: uuid.UUID, request: Request, db: DbSession):
    try:
        title = await _read_title(request)
    except HTTPException as exc:
        if wants_json(request):
            raise
        return _page(request, db, error=str(exc.detail), status_code=exc.status_code)
    user, guest_id = _owner(request, db)
    task = _owned_task(db, task_id, user, guest_id)
    task.title = encrypt_text(title)
    db.commit()
    db.refresh(task)
    return _done(request, _task_payload(task, title))


@app.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: uuid.UUID, request: Request, db: DbSession):
    user, guest_id = _owner(request, db)
    task = _owned_task(db, task_id, user, guest_id)
    task.done = not task.done
    db.commit()
    db.refresh(task)
    return _done(request, _task_payload(task))


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: uuid.UUID, request: Request, db: DbSession):
    user, guest_id = _owner(request, db)
    task = _owned_task(db, task_id, user, guest_id)
    db.delete(task)
    db.commit()
    return _done(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.method == "POST" and request.url.path == "/tasks" and not wants_json(request):
        detail = exc.detail if isinstance(exc.detail, str) else "Не удалось сохранить задачу."
        with session_scope() as db:
            return _page(request, db, error=detail, status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
