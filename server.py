"""Сервер для мини-приложения «Домашние дела».

Хранит общие данные семьи (задачи, оценки, баллы, награды) в SQLite,
проверяет, что запросы действительно пришли из Telegram, и присылает
уведомления через бота.

Настройки берутся из переменных окружения (файл .env):
  BOT_TOKEN   — токен бота от @BotFather (обязательно)
  PUBLIC_URL  — адрес приложения, например https://1-2-3-4.sslip.io
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

BASE = Path(__file__).resolve().parent
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
DB_PATH = os.environ.get("DB_PATH", str(BASE / "data.db"))
BOT_POLLING = os.environ.get("BOT_POLLING", "1") != "0"
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

INIT_DATA_MAX_AGE = 2 * 24 * 3600   # сколько живёт «пропуск» из Telegram
INVITE_TTL = 7 * 24 * 3600          # сколько действует приглашение
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в лог попадёт токен из URL
log = logging.getLogger("kidsbot")

# ---------------------------------------------------------------- база данных
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS families (
    id      TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    user_id   INTEGER PRIMARY KEY,
    family_id TEXT NOT NULL,
    role      TEXT NOT NULL,      -- 'parent' или 'child'
    child_id  TEXT,               -- для ребёнка: какой профиль в семье
    name      TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    code      TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    role      TEXT NOT NULL,
    child_id  TEXT,
    created   INTEGER NOT NULL
);
""")
lock = asyncio.Lock()


class ApiError(Exception):
    """Понятная пользователю ошибка — уходит в приложение как текст."""


class AuthError(Exception):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return secrets.token_hex(6)


def pl(n: int) -> str:
    n = abs(n) % 100
    d = n % 10
    if 10 < n < 20:
        return "баллов"
    if d == 1:
        return "балл"
    if 1 < d < 5:
        return "балла"
    return "баллов"


def text(b: dict, key: str, maxlen: int) -> str:
    return str(b.get(key) or "").strip()[:maxlen]


def num(b: dict, key: str, lo: int, hi: int, default: int) -> int:
    try:
        v = int(b.get(key))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def need(x, msg: str):
    if not x:
        raise ApiError(msg)
    return x


def find(items: list, item_id):
    return next((x for x in items if x.get("id") == item_id), None)


# ------------------------------------------------- проверка подписи Telegram
def check_init_data(init_data: str) -> dict:
    """Проверяет, что initData подписаны Telegram для нашего бота.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        raise AuthError("Откройте приложение через бота в Telegram")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        raise AuthError("Не удалось подтвердить вход через Telegram")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > INIT_DATA_MAX_AGE:
        raise AuthError("Сессия устарела — закройте и снова откройте приложение")
    try:
        user = json.loads(pairs.get("user", ""))
        user["id"] = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("Нет данных пользователя")
    return user


# ------------------------------------------------------------ данные семьи
def default_family() -> dict:
    return {
        "children": [], "tasks": [], "grades": [], "requests": [], "history": [],
        "rewards": [
            {"id": new_id(), "emoji": "🍦", "title": "Мороженое", "cost": 20},
            {"id": new_id(), "emoji": "📺", "title": "Час мультиков", "cost": 30},
            {"id": new_id(), "emoji": "🎬", "title": "Поход в кино", "cost": 100},
        ],
        "settings": {"gradePoints": {"5": 10, "4": 5, "3": 0, "2": -5}},
    }


def load_family(fid: str) -> dict:
    row = db.execute("SELECT data FROM families WHERE id = ?", (fid,)).fetchone()
    return json.loads(row["data"]) if row else default_family()


def save_family(fid: str, d: dict) -> None:
    done = 0
    tasks = []
    for t in d["tasks"]:
        if t["status"] == "done":
            done += 1
            if done > 100:
                continue
        tasks.append(t)
    d["tasks"] = tasks
    d["history"] = d["history"][:200]
    d["grades"] = d["grades"][:300]
    db.execute("UPDATE families SET data = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), fid))


def get_member(uid: int):
    return db.execute("SELECT * FROM members WHERE user_id = ?", (uid,)).fetchone()


def state_for(m) -> dict:
    fid = m["family_id"]
    rows = db.execute("SELECT role, child_id, name FROM members WHERE family_id = ?", (fid,)).fetchall()
    return {
        "member": {"role": m["role"], "childId": m["child_id"], "name": m["name"]},
        "family": load_family(fid),
        "members": [{"role": r["role"], "childId": r["child_id"], "name": r["name"]} for r in rows],
    }


class Ctx:
    """Всё, что нужно одному действию: участник, данные семьи, очередь уведомлений."""

    def __init__(self, m, d: dict, notes: list):
        self.m, self.d, self.notes = m, d, notes
        self.fid = m["family_id"]
        self.extra: dict = {}

    def kid(self, child_id):
        return find(self.d["children"], child_id)

    def add_points(self, child_id, delta: int, why: str) -> None:
        c = self.kid(child_id)
        if not c:
            return
        c["points"] = c.get("points", 0) + delta
        self.d["history"].insert(0, {"id": new_id(), "childId": child_id, "delta": delta,
                                     "text": why, "date": now_ms()})

    def to_parents(self, msg: str) -> None:
        for r in db.execute("SELECT user_id FROM members WHERE family_id = ? AND role = 'parent'", (self.fid,)):
            self.notes.append((r["user_id"], msg))

    def to_child(self, child_id, msg: str) -> None:
        for r in db.execute("SELECT user_id FROM members WHERE family_id = ? AND child_id = ?", (self.fid, child_id)):
            self.notes.append((r["user_id"], msg))


ACTIONS: dict = {}


def action(role: str):
    def deco(fn):
        ACTIONS[fn.__name__] = (role, fn)
        return fn
    return deco


# ------------------------------------------------------- действия родителя
@action("parent")
def addChild(c: Ctx, b: dict):
    name = need(text(b, "name", 20), "Введите имя")
    cid = new_id()
    c.d["children"].append({"id": cid, "name": name, "emoji": text(b, "emoji", 8) or "👦", "points": 0})
    c.extra["childId"] = cid


@action("parent")
def delChild(c: Ctx, b: dict):
    cid = need(c.kid(b.get("childId")), "Ребёнок не найден")["id"]
    c.d["children"] = [x for x in c.d["children"] if x["id"] != cid]
    for key in ("tasks", "grades", "requests", "history"):
        c.d[key] = [x for x in c.d[key] if x.get("childId") != cid]
    db.execute("DELETE FROM members WHERE family_id = ? AND child_id = ?", (c.fid, cid))
    db.execute("DELETE FROM invites WHERE family_id = ? AND child_id = ?", (c.fid, cid))


@action("parent")
def invite(c: Ctx, b: dict):
    role = b.get("role")
    child_id = None
    if role == "child":
        child_id = need(c.kid(b.get("childId")), "Ребёнок не найден")["id"]
    elif role != "parent":
        raise ApiError("Неизвестная роль")
    db.execute("DELETE FROM invites WHERE created < ?", (int(time.time()) - INVITE_TTL,))
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    db.execute("INSERT INTO invites (code, family_id, role, child_id, created) VALUES (?, ?, ?, ?, ?)",
               (code, c.fid, role, child_id, int(time.time())))
    link = f"https://t.me/{BOT_USERNAME}?startapp={code}" if BOT_USERNAME else None
    c.extra["invite"] = {"code": code, "link": link}


@action("parent")
def addTask(c: Ctx, b: dict):
    child = need(c.kid(b.get("childId")), "Ребёнок не найден")
    title = need(text(b, "title", 80), "Напишите, что нужно сделать")
    points = num(b, "points", 0, 1000, 5)
    c.d["tasks"].insert(0, {"id": new_id(), "childId": child["id"], "title": title, "points": points,
                            "status": "todo", "created": now_ms()})
    c.to_child(child["id"], f"📌 Новое задание: {title} (+{points} {pl(points)})")


def _task(c: Ctx, b: dict) -> dict:
    return need(find(c.d["tasks"], b.get("id")), "Задание не найдено")


@action("parent")
def approveTask(c: Ctx, b: dict):
    t = _task(c, b)
    if t["status"] == "done":
        raise ApiError("Задание уже выполнено")
    t["status"], t["doneAt"] = "done", now_ms()
    c.add_points(t["childId"], t["points"], f"Задание: {t['title']}")
    c.to_child(t["childId"], f"✅ Задание «{t['title']}» принято! +{t['points']} {pl(t['points'])}")


@action("parent")
def returnTask(c: Ctx, b: dict):
    t = _task(c, b)
    if t["status"] == "pending":
        t["status"] = "todo"
        c.to_child(t["childId"], f"↩️ Задание «{t['title']}» вернули на доработку")


@action("parent")
def repeatTask(c: Ctx, b: dict):
    t = _task(c, b)
    copy = {k: v for k, v in t.items() if k != "doneAt"}
    copy.update(id=new_id(), status="todo", created=now_ms())
    c.d["tasks"].insert(0, copy)
    c.to_child(t["childId"], f"📌 Снова задание: {t['title']} (+{t['points']} {pl(t['points'])})")


@action("parent")
def delTask(c: Ctx, b: dict):
    c.d["tasks"] = [x for x in c.d["tasks"] if x["id"] != b.get("id")]


@action("parent")
def addGrade(c: Ctx, b: dict):
    child = need(c.kid(b.get("childId")), "Ребёнок не найден")
    g = num(b, "grade", 2, 5, 5)
    subject = text(b, "subject", 40) or "Без предмета"
    pts = int(c.d["settings"]["gradePoints"].get(str(g), 0))
    c.d["grades"].insert(0, {"id": new_id(), "childId": child["id"], "subject": subject, "grade": g,
                             "points": pts, "date": now_ms()})
    if pts:
        c.add_points(child["id"], pts, f"Оценка {g} · {subject}")
    tail = f": {pts:+d} {pl(pts)}" if pts else ""
    c.to_child(child["id"], f"📝 Оценка {g} — {subject}{tail}")


@action("parent")
def delGrade(c: Ctx, b: dict):
    g = need(find(c.d["grades"], b.get("id")), "Оценка не найдена")
    if g["points"]:
        c.add_points(g["childId"], -g["points"], f"Отмена оценки {g['grade']} · {g['subject']}")
    c.d["grades"] = [x for x in c.d["grades"] if x["id"] != g["id"]]


@action("parent")
def addReward(c: Ctx, b: dict):
    title = need(text(b, "title", 40), "Введите название награды")
    c.d["rewards"].append({"id": new_id(), "emoji": text(b, "emoji", 8) or "🎁", "title": title,
                           "cost": num(b, "cost", 1, 100000, 50)})


@action("parent")
def delReward(c: Ctx, b: dict):
    rid = b.get("id")
    c.d["rewards"] = [x for x in c.d["rewards"] if x["id"] != rid]
    c.d["requests"] = [x for x in c.d["requests"] if x["rewardId"] != rid]


def _give(c: Ctx, child: dict, reward: dict):
    if child.get("points", 0) < reward["cost"]:
        raise ApiError("Не хватает баллов")
    c.add_points(child["id"], -reward["cost"], f"Награда: {reward['title']}")
    c.to_child(child["id"], f"🎁 Тебе награда: {reward['emoji']} {reward['title']}!")


@action("parent")
def giveReward(c: Ctx, b: dict):
    child = need(c.kid(b.get("childId")), "Ребёнок не найден")
    reward = need(find(c.d["rewards"], b.get("rewardId")), "Награда не найдена")
    _give(c, child, reward)


@action("parent")
def approveReq(c: Ctx, b: dict):
    r = need(find(c.d["requests"], b.get("id")), "Просьба не найдена")
    child, reward = c.kid(r["childId"]), find(c.d["rewards"], r["rewardId"])
    if child and reward:
        _give(c, child, reward)
    c.d["requests"] = [x for x in c.d["requests"] if x["id"] != r["id"]]


@action("parent")
def declineReq(c: Ctx, b: dict):
    r = need(find(c.d["requests"], b.get("id")), "Просьба не найдена")
    c.d["requests"] = [x for x in c.d["requests"] if x["id"] != r["id"]]
    reward = find(c.d["rewards"], r["rewardId"])
    if reward:
        c.to_child(r["childId"], f"Просьбу о награде «{reward['title']}» пока отклонили")


@action("parent")
def saveGp(c: Ctx, b: dict):
    gp = b.get("gradePoints") or {}
    for g in ("5", "4", "3", "2"):
        c.d["settings"]["gradePoints"][g] = num(gp, g, -100, 100, 0)


# -------------------------------------------------------- действия ребёнка
@action("child")
def childDone(c: Ctx, b: dict):
    t = _task(c, b)
    if t["childId"] != c.m["child_id"]:
        raise ApiError("Это не твоё задание")
    if t["status"] != "todo":
        raise ApiError("Задание уже отправлено на проверку")
    t["status"] = "pending"
    child = c.kid(t["childId"])
    c.to_parents(f"✋ {child['name']} выполнил(а) задание: {t['title']}. Проверьте в приложении.")


@action("child")
def requestReward(c: Ctx, b: dict):
    child = need(c.kid(c.m["child_id"]), "Профиль не найден")
    reward = need(find(c.d["rewards"], b.get("rewardId")), "Награда не найдена")
    if any(r["childId"] == child["id"] and r["rewardId"] == reward["id"] for r in c.d["requests"]):
        raise ApiError("Ты уже попросил эту награду")
    if child.get("points", 0) < reward["cost"]:
        raise ApiError("Пока не хватает баллов")
    c.d["requests"].append({"id": new_id(), "childId": child["id"], "rewardId": reward["id"], "date": now_ms()})
    c.to_parents(f"🙋 {child['name']} просит награду: {reward['emoji']} {reward['title']} ({reward['cost']} {pl(reward['cost'])})")


# ----------------------------------------------------- обработка запросов
def handle(user: dict, b: dict):
    uid = user["id"]
    name = (user.get("first_name") or user.get("username") or "Без имени")[:40]
    act = b.get("action")
    notes: list = []
    m = get_member(uid)

    if act == "me":
        if not m:
            return {"member": None}, notes
        if m["name"] != name:
            db.execute("UPDATE members SET name = ? WHERE user_id = ?", (name, uid))
            db.commit()
            m = get_member(uid)
        return state_for(m), notes

    if act == "createFamily":
        if m:
            raise ApiError("Вы уже состоите в семье")
        fid = new_id()
        db.execute("INSERT INTO families (id, data, created) VALUES (?, ?, ?)",
                   (fid, json.dumps(default_family(), ensure_ascii=False), int(time.time())))
        db.execute("INSERT INTO members (user_id, family_id, role, child_id, name) VALUES (?, ?, 'parent', NULL, ?)",
                   (uid, fid, name))
        db.commit()
        return state_for(get_member(uid)), notes

    if act == "join":
        code = text(b, "code", 64).upper()
        inv = db.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
        if not inv or time.time() - inv["created"] > INVITE_TTL:
            raise ApiError("Приглашение не найдено или устарело. Попросите новое.")
        if m:
            if m["family_id"] == inv["family_id"]:
                return state_for(m), notes
            raise ApiError("Вы уже состоите в другой семье. Сначала выйдите из неё.")
        d = load_family(inv["family_id"])
        child = find(d["children"], inv["child_id"]) if inv["role"] == "child" else None
        if inv["role"] == "child" and not child:
            raise ApiError("Профиль ребёнка удалён. Попросите новое приглашение.")
        db.execute("INSERT INTO members (user_id, family_id, role, child_id, name) VALUES (?, ?, ?, ?, ?)",
                   (uid, inv["family_id"], inv["role"], inv["child_id"], name))
        db.execute("DELETE FROM invites WHERE code = ?", (code,))
        db.commit()
        m = get_member(uid)
        who = f"{child['emoji']} {child['name']}" if child else f"{name} (родитель)"
        Ctx(m, d, notes).to_parents(f"👋 {who} присоединился(ась) к семье")
        notes[:] = [(chat, msg) for chat, msg in notes if chat != uid]
        return state_for(m), notes

    if not m:
        raise ApiError("Сначала создайте семью или войдите по приглашению")

    if act == "leave":
        fid = m["family_id"]
        db.execute("DELETE FROM members WHERE user_id = ?", (uid,))
        if m["role"] == "parent" and not db.execute(
                "SELECT 1 FROM members WHERE family_id = ? AND role = 'parent'", (fid,)).fetchone():
            db.execute("DELETE FROM members WHERE family_id = ?", (fid,))
            db.execute("DELETE FROM invites WHERE family_id = ?", (fid,))
            db.execute("DELETE FROM families WHERE id = ?", (fid,))
        db.commit()
        return {"member": None}, notes

    entry = ACTIONS.get(act)
    if not entry:
        raise ApiError("Неизвестное действие")
    role, fn = entry
    if m["role"] != role:
        raise ApiError("Это действие недоступно")
    c = Ctx(m, load_family(m["family_id"]), notes)
    try:
        fn(c, b)
        save_family(c.fid, c.d)
        db.commit()
    except Exception:
        db.rollback()
        raise
    result = state_for(m)
    result.update(c.extra)
    return result, notes


# -------------------------------------------------------------- бот
http: httpx.AsyncClient
background: set = set()


async def send(chat_id: int, msg: str) -> None:
    payload = {"chat_id": chat_id, "text": msg}
    if PUBLIC_URL:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть приложение", "web_app": {"url": PUBLIC_URL}}]]}
    try:
        r = await http.post(f"{TG_API}/sendMessage", json=payload, timeout=15)
        if r.status_code != 200:
            log.info("sendMessage %s: %s", chat_id, r.json().get("description"))
    except Exception as e:  # уведомления — не критично
        log.warning("sendMessage failed: %s", type(e).__name__)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


START_TEXT = ("Привет! Это «Домашние дела» — задания, оценки и награды для детей.\n\n"
              "Нажмите кнопку ниже, чтобы открыть приложение.")


async def bot_loop() -> None:
    """Отвечает на /start и узнаёт имя бота для ссылок-приглашений."""
    global BOT_USERNAME
    offset = 0
    while True:
        try:
            if not BOT_USERNAME:
                r = await http.get(f"{TG_API}/getMe", timeout=15)
                BOT_USERNAME = r.json()["result"]["username"]
                log.info("Бот: @%s", BOT_USERNAME)
            r = await http.get(f"{TG_API}/getUpdates", timeout=40, params={
                "offset": offset, "timeout": 30, "allowed_updates": json.dumps(["message"])})
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates: %s", data.get("description"))
                await asyncio.sleep(10)
                continue
            for u in data["result"]:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if (msg.get("text") or "").startswith("/start"):
                    await send(msg["chat"]["id"], START_TEXT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("bot loop: %s", type(e).__name__)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app):
    global http
    http = httpx.AsyncClient()
    task = asyncio.create_task(bot_loop()) if BOT_POLLING else None
    yield
    if task:
        task.cancel()
    await http.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def index():
    return FileResponse(BASE / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api")
async def api(request: Request):
    try:
        user = check_init_data(request.headers.get("x-init-data", ""))
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise ApiError("Некорректный запрос")
        async with lock:
            result, notes = handle(user, body)
    except AuthError as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    except ApiError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    for chat_id, msg in notes:
        spawn(send(chat_id, msg))
    return result
