import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    check_password,
    clear_session_cookie,
    display_login,
    encrypted_login,
    find_user,
    guest_id_from_request,
    hash_password,
    same_origin,
    set_guest_cookie,
    set_session_cookie,
    user_from_request,
    validate_login,
    validate_password,
)
from app.crypto import decrypt_text, encrypt_text, validate_crypto_config
from app.db import check_db, get_session, init_db
from app.models import Task, User
from app.schemas import TaskCreate, TaskOut
from app.security import BodyLimitMiddleware, OriginCheckMiddleware, RateLimitMiddleware, security_headers, wants_json

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_crypto_config()
    init_db()
    yield


app = FastAPI(title="Список задач", lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(RateLimitMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    security_headers(response)
    return response


def _owner(request: Request, db: Session) -> tuple[User | None, uuid.UUID | None]:
    user = user_from_request(request, db)
    if user is not None:
        return user, None
    return None, guest_id_from_request(request)


def _tasks_for(db: Session, user: User | None, guest_id: uuid.UUID | None) -> list[Task]:
    if user is not None:
        condition = Task.user_id == user.id
    elif guest_id is not None:
        condition = Task.guest_id == guest_id
    else:
        return []
    rows = db.scalars(select(Task).where(condition).order_by(Task.created_at.asc(), Task.id.asc()))
    return [
        SimpleNamespace(id=task.id, title=decrypt_text(task.title), done=task.done, created_at=task.created_at)
        for task in rows
    ]


def _task_payload(task: Task, title: str | None = None) -> dict:
    return TaskOut(
        id=task.id,
        title=title if title is not None else decrypt_text(task.title),
        done=task.done,
        created_at=task.created_at,
    ).model_dump(mode="json")


def _owned_task(db: Session, task_id: uuid.UUID, user: User | None, guest_id: uuid.UUID | None) -> Task:
    task = _get_task(db, task_id)
    if user is not None and task.user_id == user.id:
        return task
    if user is None and guest_id is not None and task.guest_id == guest_id and task.user_id is None:
        return task
    raise HTTPException(status_code=404, detail="Задача не найдена.")


def _claim_guest_tasks(db: Session, request: Request, user: User) -> None:
    guest_id = guest_id_from_request(request)
    if guest_id is None:
        return
    for task in db.scalars(select(Task).where(Task.guest_id == guest_id, Task.user_id.is_(None))):
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
            "current_user": SimpleNamespace(login=display_login(user)) if user else None,
        },
        status_code=status_code,
    )
    if user is None and guest_id is None:
        set_guest_cookie(response, uuid.uuid4())
    return response


async def _read_credentials(request: Request) -> tuple[str, str]:
    if not same_origin(request):
        raise HTTPException(status_code=403, detail="Запрос отклонён.")
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Введите логин и пароль.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Введите логин и пароль.")
        login = payload.get("login", "")
        password = payload.get("password", "")
    else:
        form = await request.form()
        login = form.get("login", "")
        password = form.get("password", "")
    if not isinstance(login, str) or not isinstance(password, str):
        raise HTTPException(status_code=422, detail="Введите логин и пароль.")
    try:
        return validate_login(login), validate_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _auth_response(user: User, status_code: int = 200) -> JSONResponse:
    response = JSONResponse({"login": display_login(user)}, status_code=status_code)
    set_session_cookie(response, user.id)
    return response


async def _read_title(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Введите название задачи.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Введите название задачи.")
        title = payload.get("title", "")
    else:
        form = await request.form()
        title = form.get("title", "")
    if not isinstance(title, str):
        raise HTTPException(status_code=422, detail="Введите название задачи.")
    return title


def _parse_title(title: str) -> str:
    try:
        return TaskCreate(title=title).title
    except ValidationError as exc:
        message = exc.errors()[0]["msg"]
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        raise HTTPException(status_code=422, detail=message) from exc


def _get_task(db: Session, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    return task


@app.get("/health")
def health() -> JSONResponse:
    try:
        check_db()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/")
def index(request: Request, db: Session = Depends(get_session)):
    return _page(request, db)


@app.get("/auth/me")
def auth_me(request: Request, db: Session = Depends(get_session)):
    user = user_from_request(request, db)
    if user is None:
        return JSONResponse({"login": None}, status_code=401)
    return {"login": display_login(user)}


@app.post("/auth/register")
async def register(request: Request, db: Session = Depends(get_session)):
    login, password = await _read_credentials(request)
    lookup, login_enc = encrypted_login(login)
    user = User(login=lookup, login_enc=login_enc, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Пользователь с таким логином уже существует.") from None
    _claim_guest_tasks(db, request, user)
    db.commit()
    db.refresh(user)
    return _auth_response(user, status_code=201)


@app.post("/auth/login")
async def login(request: Request, db: Session = Depends(get_session)):
    login_name, password = await _read_credentials(request)
    user = find_user(db, login_name)
    stored = user.password_hash if user is not None else None
    if not check_password(password, stored) or user is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")
    _claim_guest_tasks(db, request, user)
    db.commit()
    return _auth_response(user)


@app.post("/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@app.post("/tasks")
async def create_task(request: Request, db: Session = Depends(get_session)):
    title = _parse_title(await _read_title(request))
    user, guest_id = _owner(request, db)
    if user is None and guest_id is None:
        guest_id = uuid.uuid4()
    task = Task(
        title=encrypt_text(title),
        user_id=user.id if user else None,
        guest_id=None if user else guest_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    if wants_json(request):
        response = JSONResponse(_task_payload(task, title), status_code=201)
    else:
        response = RedirectResponse("/", status_code=303)
    if user is None:
        _remember_guest(response, request, guest_id)
    return response


@app.post("/tasks/{task_id}/edit")
async def edit_task(task_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    try:
        title = _parse_title(await _read_title(request))
    except HTTPException as exc:
        if wants_json(request):
            raise
        detail = exc.detail if isinstance(exc.detail, str) else "Не удалось сохранить задачу."
        return _page(request, db, error=detail, status_code=exc.status_code)
    user, guest_id = _owner(request, db)
    task = _owned_task(db, task_id, user, guest_id)
    task.title = encrypt_text(title)
    db.commit()
    db.refresh(task)
    if wants_json(request):
        return JSONResponse(_task_payload(task, title))
    return RedirectResponse("/", status_code=303)


@app.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    user, guest_id = _owner(request, db)
    task = _owned_task(db, task_id, user, guest_id)
    task.done = not task.done
    db.commit()
    db.refresh(task)
    if wants_json(request):
        return JSONResponse(_task_payload(task))
    return RedirectResponse("/", status_code=303)


@app.get("/tasks")
def list_tasks(request: Request, db: Session = Depends(get_session)):
    user, guest_id = _owner(request, db)
    return [
        {"id": str(task.id), "title": task.title, "done": task.done}
        for task in _tasks_for(db, user, guest_id)
    ]


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    user, guest_id = _owner(request, db)
    task = _owned_task(db, task_id, user, guest_id)
    db.delete(task)
    db.commit()
    if wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse("/", status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.method == "POST" and request.url.path == "/tasks" and not wants_json(request):
        db = next(get_session())
        try:
            detail = exc.detail if isinstance(exc.detail, str) else "Не удалось сохранить задачу."
            return _page(request, db, error=detail, status_code=exc.status_code)
        finally:
            db.close()
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
