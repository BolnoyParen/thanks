"""
Thenks: соцсеть и мессенджер на Flask + SQLite.
Запуск:  pip install flask   →   python app.py   →   http://127.0.0.1:5000

Что умеет: регистрация/вход, посты (до 280 символов), лента «Все» и «Подписки»,
лайки, профили с био, подписки, личные сообщения.
"""
import hashlib
import io
import os
import re
import secrets
import time
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (send_from_directory, Flask, g, redirect, render_template_string, request,
                   session, url_for, flash, abort, get_flashed_messages)
from markupsafe import Markup, escape
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)  # где лежат база, фото и ключ
DB_PATH = os.path.join(DATA_DIR, "krug.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")


def load_secret():
    """Ключ подписи сессий: из SECRET_KEY или из файла secret.key (создаётся один раз)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


app.secret_key = load_secret()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure-куки: на сервере (импорт модуля) включены, при локальном «python3 app.py» выключены
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("KRUG_HTTPS", "0" if __name__ == "__main__" else "1") == "1"
Image.MAX_IMAGE_PIXELS = 40_000_000  # защита от «бомб» в картинках
FAILS = {}  # неудачные входы: имя -> список времён
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
app.config["MAX_CONTENT_LENGTH"] = 48 * 1024 * 1024  # запрос целиком (видео в канал до 40 МБ)
IMAGE_MAX = 8 * 1024 * 1024
VIDEO_MAX = 40 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pw TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS likes (
    user_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, post_id)
);
CREATE TABLE IF NOT EXISTS follows (
    follower_id INTEGER NOT NULL,
    followed_id INTEGER NOT NULL,
    PRIMARY KEY (follower_id, followed_id)
);
CREATE TABLE IF NOT EXISTS chat_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chat_group_members (
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS chat_group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    post_id INTEGER,
    seen INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS blocks (
    blocker_id INTEGER NOT NULL,
    blocked_id INTEGER NOT NULL,
    PRIMARY KEY (blocker_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    descr TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS community_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    art TEXT NOT NULL DEFAULT 'orbit',
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS mutes (
    user_id INTEGER NOT NULL,
    muted_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, muted_id)
);
CREATE TABLE IF NOT EXISTS chat_ttl (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    days INTEGER NOT NULL,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS reactions (
    kind TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    PRIMARY KEY (kind, target_id, user_id)
);
CREATE TABLE IF NOT EXISTS hidden_msgs (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, kind, msg_id)
);
CREATE TABLE IF NOT EXISTS channel_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    pos INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS i_chmedia_post ON channel_media(post_id);
CREATE INDEX IF NOT EXISTS i_posts_user ON posts(user_id);
CREATE INDEX IF NOT EXISTS i_likes_post ON likes(post_id);
CREATE INDEX IF NOT EXISTS i_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS i_msg_rcpt ON messages(recipient_id);
CREATE INDEX IF NOT EXISTS i_msg_send ON messages(sender_id);
CREATE INDEX IF NOT EXISTS i_notif_user ON notifications(user_id, seen);
CREATE INDEX IF NOT EXISTS i_follows_followed ON follows(followed_id);
CREATE INDEX IF NOT EXISTS i_chpost_ch ON channel_posts(channel_id);
CREATE INDEX IF NOT EXISTS i_chmem_user ON channel_members(user_id);
CREATE INDEX IF NOT EXISTS i_grpmsg_grp ON chat_group_messages(group_id);
CREATE INDEX IF NOT EXISTS i_grpmem_user ON chat_group_members(user_id);
CREATE INDEX IF NOT EXISTS i_blocks_blocked ON blocks(blocked_id);
"""


# ---------- база данных ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA temp_store=MEMORY")
        g.db.execute("PRAGMA cache_size=-8000")
        g.db.create_function("pylower", 1, lambda s: s.lower())  # LIKE без учёта регистра для кириллицы
        g.db.create_function("vis", 2, lambda p, o: 1 if visible(p, o) else 0)
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


MILESTONES = {
    "project": "Первый проект",
    "course": "Закончил курс",
    "order": "Первый заказ",
    "interview": "Первое собеседование",
    "offer": "Оффер",
    "remote": "Перешёл на удалёнку",
}

SEED_NEWS = [
    ("Привет! Это Thenks", "Thenks: соцсеть и мессенджер для тех, кто учится и растёт в IT. Делись тем, что изучил, "
     "отмечай вехи на своём пути и общайся с теми, кто идёт тем же маршрутом. Все обновления проекта "
     "публикуем в официальном канале Thenks.", "welcome"),
]

OFFICIAL_SLUG = "thenks"
FOUNDERS = {"kul": "Основатель проекта"}  # галочки: юзернейм -> подпись
BADGES = dict(FOUNDERS, **{"#thenks": "Официальный канал Thenks"})
ANNOUNCEMENTS = [  # (текст, раскладка медиа, файлы из папки seed_media)
    ("Привет! Это Thenks\n\nThenks: соцсеть и мессенджер для тех, кто учится и растёт в IT. Мы команда "
     "разработчиков-самоучек и делаем площадку, которой самим не хватало: без шума и гонки за лайками, "
     "с упором на реальный прогресс.", "", ()),
    ("Зачем всё это\n\nГлавная идея: видимый путь от мысли к делу. Отмечай «Что изучил сегодня» и держи серию, "
     "фиксируй вехи: первый проект, первое собеседование, оффер. Находи тех, кто идёт тем же маршрутом, "
     "и общайся в личных чатах, группах и каналах.", "", ()),
    ("Что дальше\n\nВ планах: файлы и голосовые в сообщениях, живой чат без перезагрузки и уведомления "
     "на телефон. Все важные новости публикуем здесь.", "", ()),
    ("Обновление: новый стиль\n\nНовое имя Thenks, новый логотип и свежая палитра: чёрный, белый и голубой. "
     "Сдвинь ползунок на картинке, чтобы сравнить ленту до и после.", "ba", ("feed_before.jpg", "feed_after.jpg")),
    ("Директ и чаты\n\nЧат стал отдельным экраном: поле ввода больше не перекрывает сообщения, даже с открытой "
     "клавиатурой. Список чатов обновляется сам, а новые сообщения появляются без мигания.", "ba",
     ("chat_before.jpg", "chat_after.jpg")),
    ("Меню сообщения\n\nЗажми сообщение: реакции, ответ, копирование, правка и удаление. Свайп вбок отвечает "
     "на сообщение. Удалённое сообщение плавно исчезает.", "", ("menu.mp4",)),
    ("Автоудаление с ползунком\n\nПрофиль собеседника → «Ещё» → «Автоудаление сообщений». Выбери срок "
     "от одного дня до полугода, и в чате появится заметка для вас обоих.", "", ("ttl.mp4",)),
    ("Цвет и фон на выбор\n\nНастройки → Оформление: семь цветов, четыре фона и тема. Всё меняется сразу, "
     "без перезагрузки.", "", ("look.mp4",)),
    ("Новый профиль\n\nКанал автора, публикации и вехи прямо в профиле, а фото можно обрезать и приблизить "
     "перед сохранением.", "ba", ("profile_before.jpg", "profile_after.jpg")),
    ("Фото профиля: кадр и масштаб\n\nВыбери фото, подвинь его в круге и приблизи ползунком или двумя "
     "пальцами. Сохраняется ровно то, что видно в круге.", "", ("crop.mp4",)),
]
SEED_DIR = os.path.join(BASE_DIR, "seed_media")


def ensure_official(conn):
    """Официальный канал Thenks: владелец основатель, внутри все объявления проекта (с фото и видео)."""
    owner = conn.execute("SELECT id FROM users WHERE username=?", (next(iter(FOUNDERS)),)).fetchone()
    if owner is None:
        return
    ch = conn.execute("SELECT id FROM channels WHERE slug=?", (OFFICIAL_SLUG,)).fetchone()
    if ch is None:
        cur = conn.execute("INSERT INTO channels (owner_id, slug, title, descr, private, invite, nocopy) "
                           "VALUES (?, ?, 'Thenks', 'Официальный канал: обновления и новости проекта', 0, ?, 0)",
                           (owner[0], OFFICIAL_SLUG, secrets.token_urlsafe(9)))
        conn.execute("INSERT OR IGNORE INTO channel_members (channel_id, user_id) VALUES (?, ?)", (cur.lastrowid, owner[0]))
        cid = cur.lastrowid
    else:
        cid = ch[0]
    have = {}
    for pid, body in conn.execute("SELECT id, body FROM channel_posts WHERE channel_id=?", (cid,)):
        have.setdefault(body.split("\n")[0], pid)
    for body, layout, files in ANNOUNCEMENTS:  # новые объявления из кода добавляются сами
        title = body.split("\n")[0]
        pid = have.get(title)
        if pid is None:
            pid = conn.execute("INSERT INTO channel_posts (channel_id, body) VALUES (?, ?)", (cid, body)).lastrowid
            have[title] = pid
        if files and not conn.execute("SELECT 1 FROM channel_media WHERE post_id=?", (pid,)).fetchone() \
                and all(os.path.exists(os.path.join(SEED_DIR, f)) for f in files):
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            for i, f in enumerate(files):
                name = secrets.token_hex(12) + os.path.splitext(f)[1].lower()
                with open(os.path.join(SEED_DIR, f), "rb") as src, open(os.path.join(UPLOAD_DIR, name), "wb") as dst:
                    dst.write(src.read())
                kind = "video" if f.lower().endswith((".mp4", ".mov", ".webm")) else "img"
                conn.execute("INSERT INTO channel_media (post_id, kind, name, pos) VALUES (?, ?, ?, ?)", (pid, kind, name, i))
            conn.execute("UPDATE channel_posts SET layout=? WHERE id=?", (layout, pid))


MIGRATIONS = [
    ("users", "goal", "TEXT NOT NULL DEFAULT ''"),
    ("users", "stack", "TEXT NOT NULL DEFAULT ''"),
    ("users", "started", "TEXT NOT NULL DEFAULT ''"),
    ("users", "display_name", "TEXT NOT NULL DEFAULT ''"),
    ("users", "avatar", "TEXT NOT NULL DEFAULT ''"),
    ("users", "cover", "TEXT NOT NULL DEFAULT ''"),
    ("posts", "milestone", "TEXT NOT NULL DEFAULT ''"),
    ("users", "msg_policy", "TEXT NOT NULL DEFAULT 'all'"),
    ("users", "comment_policy", "TEXT NOT NULL DEFAULT 'all'"),
    ("users", "notif_off", "TEXT NOT NULL DEFAULT ''"),
    ("channels", "private", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "invite", "TEXT NOT NULL DEFAULT ''"),
    ("channels", "nocopy", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_seen", "TEXT NOT NULL DEFAULT ''"),
    ("users", "show_seen", "INTEGER NOT NULL DEFAULT 1"),
    ("messages", "edited", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_group_messages", "edited", "INTEGER NOT NULL DEFAULT 0"),
    ("channel_posts", "edited", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "reply_to", "INTEGER"),
    ("chat_group_messages", "reply_to", "INTEGER"),
    ("posts", "image", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "image", "TEXT NOT NULL DEFAULT ''"),
    ("messages", "seen", "INTEGER NOT NULL DEFAULT 1"),
    ("chat_group_messages", "image", "TEXT NOT NULL DEFAULT ''"),
    ("channel_posts", "image", "TEXT NOT NULL DEFAULT ''"),
    ("chat_group_members", "last_read", "INTEGER NOT NULL DEFAULT 0"),
    ("channel_members", "last_read", "INTEGER NOT NULL DEFAULT 0"),
    ("posts", "kind", "TEXT NOT NULL DEFAULT 'post'"),
    ("users", "seen_policy", "TEXT NOT NULL DEFAULT 'all'"),
    ("users", "avatar_policy", "TEXT NOT NULL DEFAULT 'all'"),
    ("users", "bio_policy", "TEXT NOT NULL DEFAULT 'all'"),
    ("users", "group_policy", "TEXT NOT NULL DEFAULT 'all'"),
    ("users", "autodel", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "kind", "TEXT NOT NULL DEFAULT ''"),
    ("channel_posts", "layout", "TEXT NOT NULL DEFAULT ''"),
]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        added = set()
        for table, col, ddl in MIGRATIONS:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                added.add((table, col))
        if ("chat_group_members", "last_read") in added:  # старые сообщения считаем прочитанными
            conn.execute("UPDATE chat_group_members SET last_read = (SELECT COALESCE(MAX(id), 0) "
                         "FROM chat_group_messages WHERE group_id = chat_group_members.group_id)")
        if ("channel_members", "last_read") in added:
            conn.execute("UPDATE channel_members SET last_read = (SELECT COALESCE(MAX(id), 0) "
                         "FROM channel_posts WHERE channel_id = channel_members.channel_id)")
        if ("users", "seen_policy") in added:  # старая галочка «показывать время захода»
            conn.execute("UPDATE users SET seen_policy='nobody' WHERE show_seen=0")
        have = [tuple(r) for r in conn.execute("SELECT title, body, art FROM community_posts ORDER BY id")]
        if have != SEED_NEWS:  # в ленте только один приветственный пост
            conn.execute("DELETE FROM community_posts")
            conn.executemany("INSERT INTO community_posts (title, body, art) VALUES (?, ?, ?)", SEED_NEWS)
        conn.execute("DELETE FROM notifications WHERE kind='message'")  # уведомления о сообщениях отключены
        ensure_official(conn)


_last_backup = None


def daily_backup():
    """Раз в сутки копия базы в папку backups (хранятся последние 7)."""
    global _last_backup
    today = date.today().isoformat()
    if _last_backup == today:
        return
    _last_backup = today
    try:
        folder = os.path.join(DATA_DIR, "backups")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, "krug-%s.db" % today)
        if not os.path.exists(dest):
            src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(dest)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        for old in sorted(os.listdir(folder))[:-7]:
            os.remove(os.path.join(folder, old))
    except Exception:
        pass
    try:
        with sqlite3.connect(DB_PATH) as conn:
            old = conn.execute("SELECT id FROM users WHERE autodel > 0 AND last_seen != '' AND "
                               "last_seen < datetime('now', '-' || (autodel * 30) || ' days')").fetchall()
            for (uid,) in old:
                erase_user(conn, uid)
    except Exception:
        pass


def visible(policy, owner_id):
    """Можно ли текущему зрителю видеть поле владельца (all / following / nobody)."""
    me = g.user["id"] if g.get("user") else 0
    if owner_id == me or policy in (None, "", "all"):
        return True
    return policy == "following" and owner_id in g.get("fans", ())


@app.before_request
def load_user():
    g.nonce = secrets.token_urlsafe(16)
    daily_backup()
    uid = session.get("uid")
    g.user, g.unread, g.unread_dm, g.fans = None, 0, 0, set()
    if uid:
        g.user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user:
            g.fans = {r[0] for r in db().execute("SELECT follower_id FROM follows WHERE followed_id=?", (uid,))}
            g.unread = db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND seen=0",
                                    (g.user["id"],)).fetchone()[0]
            g.unread_dm = db().execute("SELECT COUNT(*) FROM messages WHERE recipient_id=? AND seen=0", (g.user["id"],)).fetchone()[0]
            db().execute("UPDATE users SET last_seen=datetime('now') WHERE id=? AND "
                         "(last_seen='' OR last_seen < datetime('now','-1 minute'))", (g.user["id"],))
            db().commit()


@app.before_request
def check_csrf():
    if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
        abort(400)


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


# ---------- шаблоны ----------
BASE = r"""<!doctype html>
<html lang="ru"{% if full %} class="chatmode"{% endif %}><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Thenks">
<meta name="format-detection" content="telephone=no">
<title>Thenks</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%230b0b0c'/%3E%3Cpath d='M10 11h12M16 11v11' stroke='%23fff' stroke-width='3' stroke-linecap='round'/%3E%3Ccircle cx='22.3' cy='21.3' r='2.3' fill='%233d9bff'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&family=Unbounded:wght@600;700&display=swap">
<script nonce="{{ nonce }}">(function(){var d=document.documentElement,g=function(k){try{return localStorage.getItem(k)}catch(e){return null}};var t=g('theme')||'dark';if(t==='auto')t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';d.dataset.theme=t;d.dataset.accent=g('accent')||'sky';d.dataset.bg=g('bg')||'pattern';var mc=document.querySelector('meta[name=theme-color]');if(mc)mc.content=t==='dark'?'#000000':'#f2f2f7'})()</script>
<style>
:root{--bg:#f2f2f7;--ink:#0b0b0c;--mute:#8a8a8e;--card:#fff;--bar:#fff;--line:rgba(0,0,0,.08);--seg:rgba(0,0,0,.05);--seg-on:#fff;--theirs:#fff;--shadow:0 8px 24px rgba(0,0,0,.08);--warm:#f08c00;--danger:#ff3b30;--on:#fff;--dot:rgba(0,0,0,.13);--mkedge:transparent;
 --accent:#2b8af7;--mine:#1f7ae8;--soft:rgba(43,138,247,.12);--glow:rgba(43,138,247,.32);
 --font:"Onest",-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--display:"Unbounded","Onest",-apple-system,sans-serif;
 --ease:cubic-bezier(.2,.85,.25,1);--spring:cubic-bezier(.34,1.36,.64,1);--sb:env(safe-area-inset-bottom,0px);--st:env(safe-area-inset-top,0px);color-scheme:light}
:root[data-theme="dark"]{--bg:#000;--ink:#fff;--mute:#8d8d93;--card:#1c1c1e;--bar:#1c1c1e;--line:rgba(255,255,255,.09);--seg:rgba(255,255,255,.08);--seg-on:#3a3a3c;--theirs:#1c1c1e;--shadow:0 8px 24px rgba(0,0,0,.6);--warm:#ffa94d;--danger:#ff453a;--dot:rgba(255,255,255,.13);--mkedge:rgba(255,255,255,.22);
 --accent:#3d9bff;--mine:#2b8af7;--soft:rgba(61,155,255,.16);--glow:rgba(61,155,255,.3);color-scheme:dark}
:root[data-accent="blue"]{--accent:#3a5bff;--mine:#3452f0;--soft:rgba(58,91,255,.12);--glow:rgba(58,91,255,.3)}
:root[data-theme="dark"][data-accent="blue"]{--accent:#6683ff;--mine:#3a5bff;--soft:rgba(102,131,255,.17)}
:root[data-accent="violet"]{--accent:#7c5cff;--mine:#6c4cf0;--soft:rgba(124,92,255,.12);--glow:rgba(124,92,255,.3)}
:root[data-theme="dark"][data-accent="violet"]{--accent:#9b85ff;--mine:#7c5cff;--soft:rgba(155,133,255,.17)}
:root[data-accent="mint"]{--accent:#0c9f73;--mine:#0c9469;--soft:rgba(12,159,115,.12);--glow:rgba(18,184,134,.3)}
:root[data-theme="dark"][data-accent="mint"]{--accent:#20c997;--mine:#0c9469;--soft:rgba(32,201,151,.16)}
:root[data-accent="orange"]{--accent:#f76707;--mine:#e8590c;--soft:rgba(247,103,7,.12);--glow:rgba(247,103,7,.28)}
:root[data-theme="dark"][data-accent="orange"]{--accent:#ff8a3d;--mine:#e8590c;--soft:rgba(255,138,61,.16)}
:root[data-accent="pink"]{--accent:#e64980;--mine:#d6336c;--soft:rgba(230,73,128,.12);--glow:rgba(230,73,128,.28)}
:root[data-theme="dark"][data-accent="pink"]{--accent:#f06595;--mine:#d6336c;--soft:rgba(240,101,149,.16)}
:root[data-accent="graphite"]{--accent:#1c1c1e;--mine:#2c2c2e;--soft:rgba(0,0,0,.08);--glow:rgba(0,0,0,.14)}
:root[data-theme="dark"][data-accent="graphite"]{--accent:#f2f2f7;--mine:#3a3a3c;--soft:rgba(255,255,255,.12);--glow:rgba(255,255,255,.12);--on:#000}
*{box-sizing:border-box}
html{background:#f2f2f7;-webkit-tap-highlight-color:transparent;-webkit-text-size-adjust:100%;text-size-adjust:100%}
:root[data-theme="dark"]{background:#000}
body{margin:0;min-height:100vh;min-height:100dvh;background:transparent;color:var(--ink);font:400 16px/1.45 var(--font);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;font-feature-settings:"ss01"}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.6;transform:translateZ(0);background-size:auto,auto,300px 300px;background-image:radial-gradient(900px 520px at 88% -8%,var(--soft),transparent 62%),radial-gradient(760px 520px at -8% 108%,var(--soft),transparent 62%),url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%27300%27%20height%3D%27300%27%20viewBox%3D%270%200%20300%20300%27%20fill%3D%27none%27%20stroke%3D%27rgb%2820%2C30%2C50%29%27%20stroke-width%3D%271.6%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%20opacity%3D%27.11%27%20font-family%3D%27ui-monospace%2CMenlo%2Cmonospace%27%20font-size%3D%2721%27%3E%3Cg%20transform%3D%27translate%2820%2046%29%20rotate%28-8%29%27%3E%3Ctext%20fill%3D%27rgb%2820%2C30%2C50%29%27%20stroke%3D%27none%27%3E%26lt%3B%2F%26gt%3B%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28176%2040%29%20rotate%286%29%27%3E%3Ctext%20fill%3D%27rgb%2820%2C30%2C50%29%27%20stroke%3D%27none%27%3E%7B%20%7D%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28236%20138%29%20rotate%28-5%29%27%3E%3Ctext%20fill%3D%27rgb%2820%2C30%2C50%29%27%20stroke%3D%27none%27%3E%26gt%3B_%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%2892%20120%29%20rotate%287%29%27%3E%3Ctext%20fill%3D%27rgb%2820%2C30%2C50%29%27%20stroke%3D%27none%27%3E01%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28190%20205%29%27%3E%3Crect%20width%3D%2722%27%20height%3D%2722%27%20rx%3D%274%27%2F%3E%3Crect%20x%3D%276%27%20y%3D%276%27%20width%3D%2710%27%20height%3D%2710%27%20rx%3D%272%27%2F%3E%3Cpath%20d%3D%27M6%20-5v5M11%20-5v5M16%20-5v5M6%2022v5M11%2022v5M16%2022v5M-5%206h5M-5%2011h5M-5%2016h5M22%206h5M22%2011h5M22%2016h5%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%2834%20200%29%20rotate%28-6%29%27%3E%3Ccircle%20cx%3D%275%27%20cy%3D%275%27%20r%3D%273%27%2F%3E%3Ccircle%20cx%3D%275%27%20cy%3D%2725%27%20r%3D%273%27%2F%3E%3Ccircle%20cx%3D%2721%27%20cy%3D%2711%27%20r%3D%273%27%2F%3E%3Cpath%20d%3D%27M5%208v14M21%2014c0%206-8%206-14%209%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28120%20262%29%20rotate%288%29%27%3E%3Cpath%20d%3D%27M2%202l8%2020%203-8%208-3z%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28258%20250%29%27%3E%3Cpath%20d%3D%27M2%2010a14%2014%200%200%201%2022%200M6%2015a8%208%200%200%201%2014%200M11%2020a2%202%200%200%201%204%200%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%2870%2020%29%27%3E%3Ctext%20fill%3D%27rgb%2820%2C30%2C50%29%27%20stroke%3D%27none%27%20font-size%3D%2716%27%3E%23%3C%2Ftext%3E%3C%2Fg%3E%3C%2Fsvg%3E")}
:root[data-theme="dark"] body::before{background-image:radial-gradient(900px 520px at 88% -8%,var(--soft),transparent 62%),radial-gradient(760px 520px at -8% 108%,var(--soft),transparent 62%),url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%27300%27%20height%3D%27300%27%20viewBox%3D%270%200%20300%20300%27%20fill%3D%27none%27%20stroke%3D%27rgb%28255%2C255%2C255%29%27%20stroke-width%3D%271.6%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%20opacity%3D%27.07%27%20font-family%3D%27ui-monospace%2CMenlo%2Cmonospace%27%20font-size%3D%2721%27%3E%3Cg%20transform%3D%27translate%2820%2046%29%20rotate%28-8%29%27%3E%3Ctext%20fill%3D%27rgb%28255%2C255%2C255%29%27%20stroke%3D%27none%27%3E%26lt%3B%2F%26gt%3B%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28176%2040%29%20rotate%286%29%27%3E%3Ctext%20fill%3D%27rgb%28255%2C255%2C255%29%27%20stroke%3D%27none%27%3E%7B%20%7D%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28236%20138%29%20rotate%28-5%29%27%3E%3Ctext%20fill%3D%27rgb%28255%2C255%2C255%29%27%20stroke%3D%27none%27%3E%26gt%3B_%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%2892%20120%29%20rotate%287%29%27%3E%3Ctext%20fill%3D%27rgb%28255%2C255%2C255%29%27%20stroke%3D%27none%27%3E01%3C%2Ftext%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28190%20205%29%27%3E%3Crect%20width%3D%2722%27%20height%3D%2722%27%20rx%3D%274%27%2F%3E%3Crect%20x%3D%276%27%20y%3D%276%27%20width%3D%2710%27%20height%3D%2710%27%20rx%3D%272%27%2F%3E%3Cpath%20d%3D%27M6%20-5v5M11%20-5v5M16%20-5v5M6%2022v5M11%2022v5M16%2022v5M-5%206h5M-5%2011h5M-5%2016h5M22%206h5M22%2011h5M22%2016h5%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%2834%20200%29%20rotate%28-6%29%27%3E%3Ccircle%20cx%3D%275%27%20cy%3D%275%27%20r%3D%273%27%2F%3E%3Ccircle%20cx%3D%275%27%20cy%3D%2725%27%20r%3D%273%27%2F%3E%3Ccircle%20cx%3D%2721%27%20cy%3D%2711%27%20r%3D%273%27%2F%3E%3Cpath%20d%3D%27M5%208v14M21%2014c0%206-8%206-14%209%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28120%20262%29%20rotate%288%29%27%3E%3Cpath%20d%3D%27M2%202l8%2020%203-8%208-3z%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%28258%20250%29%27%3E%3Cpath%20d%3D%27M2%2010a14%2014%200%200%201%2022%200M6%2015a8%208%200%200%201%2014%200M11%2020a2%202%200%200%201%204%200%27%2F%3E%3C%2Fg%3E%3Cg%20transform%3D%27translate%2870%2020%29%27%3E%3Ctext%20fill%3D%27rgb%28255%2C255%2C255%29%27%20stroke%3D%27none%27%20font-size%3D%2716%27%3E%23%3C%2Ftext%3E%3C%2Fg%3E%3C%2Fsvg%3E")}
:root[data-bg="plain"] body::before{background-image:none}
:root[data-bg="dots"] body::before{opacity:1;background-image:radial-gradient(var(--dot) 1.15px,transparent 1.7px);background-size:22px 22px}
:root[data-bg="gradient"] body::before{opacity:1;background-size:auto;background-image:radial-gradient(1000px 700px at 90% -10%,var(--glow),transparent 62%),radial-gradient(900px 700px at -10% 105%,var(--glow),transparent 62%),radial-gradient(600px 400px at 50% 50%,var(--soft),transparent 70%)}
html.chatmode,html.chatmode body{overflow:hidden;overscroll-behavior:none}
a{color:inherit}
strong,b{font-weight:600}
[hidden]{display:none!important}
h1{font-size:30px;font-weight:700;letter-spacing:-.8px;margin:0 0 14px;line-height:1.15}
h2.sec{font-size:13px;font-weight:500;color:var(--mute);text-transform:uppercase;letter-spacing:.4px;margin:22px 12px 8px}
/* ---------- каркас ---------- */
.app{max-width:600px;margin:0 auto;padding:calc(10px + var(--st)) 16px calc(104px + var(--sb))}
.top{display:flex;justify-content:space-between;align-items:center;min-height:48px;margin-bottom:12px}
.brand{display:flex;align-items:center;gap:7px;text-decoration:none}
.brand svg{width:32px;height:32px;display:block;transition:transform .5s var(--spring)}
.brand:active svg{transform:scale(.9) rotate(-6deg)}
.brand .mk rect{stroke:var(--mkedge);stroke-width:1.2}
.brand span{font:600 21px/1 var(--display);letter-spacing:-.04em;color:var(--ink)}
.hr,.auth{display:flex;gap:8px;align-items:center}
.auth a:not(.btn){text-decoration:none;font-weight:500;padding:8px 6px}
.rbtn{position:relative;flex:none;width:42px;height:42px;border-radius:50%;display:grid;place-items:center;padding:0;background:var(--bar);border:1px solid var(--line);color:var(--ink);text-decoration:none;cursor:pointer}
.rbtn svg{width:21px;height:21px}
.rbtn.back{margin-bottom:12px}
.gbtn{background:transparent;border:0}
.gbtn::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1.6px;background:var(--accent);-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask:linear-gradient(#000 0 0) content-box exclude,linear-gradient(#000 0 0);pointer-events:none}
.thbtn{display:none}
.bell b{position:absolute;top:-3px;right:-3px;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--accent);color:var(--on);font-size:11px;font-weight:600;display:grid;place-items:center;animation:pop .3s var(--spring)}
.side{display:none}
/* ---------- нижняя панель вкладок ---------- */
.tabbar{position:fixed;z-index:50;left:50%;bottom:calc(10px + var(--sb));width:min(420px,calc(100% - 24px));transform:translate3d(-50%,0,0);transition:transform .4s var(--ease),opacity .25s}
.tabs4{position:relative;display:flex;padding:5px;border-radius:26px;background:var(--bar);border:1px solid var(--line);box-shadow:var(--shadow)}
.tabbar .pill{position:absolute;top:5px;bottom:5px;left:5px;width:calc((100% - 10px) / 4);border-radius:21px;background:var(--pillbg);transform:translate3d(calc(var(--i,0) * 100%),0,0);transition:transform .5s var(--spring),opacity .2s;will-change:transform}
.tabs4 a{position:relative;z-index:1;flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:2px;padding:7px 0 6px;font-size:10.5px;font-weight:500;letter-spacing:-.1px;color:var(--mute);text-decoration:none;transition:color .25s;white-space:nowrap}
.tabs4 a svg{width:24px;height:24px;transition:transform .45s var(--spring)}
.tabs4 a.on{color:var(--accent)}
.tabs4 a.on svg{transform:translateY(-1px) scale(1.08)}
.tabs4 a:active svg{transform:scale(.86)}
.tbadge{position:absolute;top:2px;left:calc(50% + 5px);min-width:17px;height:17px;padding:0 4px;border-radius:9px;background:var(--accent);color:var(--on);font:600 11px/17px var(--font);text-align:center;font-style:normal;box-shadow:0 0 0 2px var(--bar)}
.kb .tabbar,.tabbar.fullhide{transform:translate3d(-50%,140%,0);opacity:0;pointer-events:none}
.prog{position:fixed;z-index:120;top:0;left:0;height:2.5px;width:100%;background:var(--accent);transform-origin:0 50%;transform:scaleX(0);pointer-events:none}
.prog.on{animation:prog 2.5s cubic-bezier(.1,.7,.2,1) forwards}
@keyframes prog{from{transform:scaleX(.05)}to{transform:scaleX(.85)}}
/* ---------- элементы ---------- */
.card,.post{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px 16px;margin-bottom:12px}
.post{content-visibility:auto;contain-intrinsic-size:auto 150px}
.phead{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.post .who{font-weight:600;text-decoration:none}
.post time,.muted{color:var(--mute);font-size:14px}
.post time{margin-left:auto}
.post p{margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.row{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
.rowh{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}
.rowh h1{margin:0}
textarea,input[type=text],input[type=password],input[type=date],select{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:12px;font:inherit;font-size:16px;background:var(--seg);color:var(--ink);transition:border-color .2s,background .2s}
textarea{resize:vertical;min-height:84px}
select{width:auto}
input[type=radio],input[type=checkbox]{accent-color:var(--accent)}
input[type=file]{width:100%;font-size:14px;color:var(--mute)}
input[type=file]::file-selector-button{margin-right:12px;padding:8px 14px;border:0;border-radius:10px;background:var(--pillbg);color:var(--accent);font:inherit;font-weight:500;cursor:pointer}
textarea:focus,input:focus,select:focus{outline:none;border-color:var(--accent);background:var(--card)}
button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button,.btn{background:var(--accent);color:var(--on);border:0;border-radius:12px;padding:10px 18px;font:inherit;font-weight:500;cursor:pointer;text-decoration:none;display:inline-block;transition:transform .2s var(--spring),opacity .2s,background .25s;-webkit-user-select:none;user-select:none}
button:active,.btn:active,.rbtn:active,.tile:active{transform:scale(.95)}
button.ghost,.btn.ghost{background:var(--seg);color:var(--ink)}
button.like{background:none;color:var(--mute);padding:4px 8px;border-radius:9px;font-weight:400;font-size:14px}
button.like:hover{background:var(--seg)}
label{display:block;margin:12px 0 5px;font-size:14px;color:var(--mute)}
.opt{display:inline-flex;gap:6px;align-items:center;margin:0;color:var(--ink);font-size:15px}
.flash{background:var(--soft);border:1px solid var(--soft);padding:10px 14px;border-radius:14px;margin-bottom:12px;animation:rise .35s var(--ease)}
.note{color:var(--mute);font-size:13px;margin:6px 8px 16px}
.soon{color:var(--mute);font-size:14px}
.streak{margin:0 0 10px;font-size:15px}
.ava{display:inline-grid;place-items:center;flex:none;width:32px;height:32px;border-radius:50%;color:#fff;font-weight:600;font-size:14px;object-fit:cover}
.at{font-style:normal;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
a.hash{color:var(--accent);text-decoration:none}
a.hash.at{text-decoration:underline}
.tag{display:inline-block;padding:1px 10px;border-radius:999px;background:var(--soft);color:var(--accent);font-size:12.5px;font-weight:500}
.tag.ms{background:rgba(240,140,0,.14);color:var(--warm)}
.post.is-ms{border-left:4px solid var(--warm)}
.pic{display:block;width:100%;max-height:440px;object-fit:cover;border-radius:14px;margin:6px 0;cursor:zoom-in;background:var(--seg)}
.tabs{display:flex;gap:6px;margin-bottom:14px;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tabs a{padding:7px 15px;border-radius:999px;text-decoration:none;background:var(--seg);font-size:14px;font-weight:500;white-space:nowrap;transition:background .25s,color .25s}
.tabs a.here{background:var(--ink);color:var(--bg)}
.news{padding:0;overflow:hidden}
.news .art svg{display:block;width:100%;height:150px}
.news .nbody{padding:12px 16px 14px}
.news h3{margin:8px 0 4px;font-size:18px;font-weight:600;letter-spacing:-.3px}
.news p{margin:0 0 6px;color:var(--mute);font-size:15px}
.news .by{display:flex;align-items:center;gap:8px;font-size:14px}
/* профиль, списки, настройки */
.hero{text-align:center;padding:4px 0 14px}
.pava{display:grid;place-items:center;width:96px;height:96px;margin:0 auto;border-radius:50%;object-fit:cover;color:#fff;font:600 40px var(--font);cursor:zoom-in}
span.pava{cursor:default}
.hero .cava{width:96px;height:96px;font-size:40px;margin:0 auto}
.hero h1{font-size:22px;margin:12px 0 2px;letter-spacing:-.3px}
.hero small{color:var(--mute);font-size:14px}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:4px 0 16px}
.tiles.t3{grid-template-columns:repeat(3,1fr)}
.tiles>form{display:contents}
.tile{position:relative;display:flex;flex-direction:column;align-items:center;gap:5px;padding:11px 6px;border-radius:16px;background:var(--card);border:1px solid var(--line);color:var(--accent);font:inherit;font-size:13px;font-weight:500;text-decoration:none;cursor:pointer;transition:transform .2s var(--spring),background .2s}
button.tile{border:1px solid var(--line)}
.tile.off{color:var(--mute)}
details.tile summary{list-style:none;display:flex;flex-direction:column;align-items:center;gap:5px;width:100%;cursor:pointer}
details.tile summary::-webkit-details-marker{display:none}
.menu{position:absolute;right:0;top:100%;margin-top:6px;min-width:220px;background:var(--bar);border:1px solid var(--line);border-radius:16px;padding:6px;z-index:60;text-align:left;box-shadow:0 14px 36px rgba(0,0,0,.25);animation:popin .22s var(--spring);transform-origin:top right}
.menu button,.menu a{display:block;width:100%;text-align:left;background:none;color:var(--ink);padding:11px 12px;border-radius:10px;font-size:15px;text-decoration:none;font-weight:400}
.menu button:hover,.menu a:hover{background:var(--seg)}
.menu button:active{transform:none;background:var(--seg)}
.danger,.menu .danger{color:var(--danger)!important}
.list{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:8px}
.list>a,.list>div,.list>label{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 16px;text-decoration:none;border-bottom:1px solid var(--line);margin:0;color:var(--ink);font-size:16px;transition:background .15s}
.list>a:active{background:var(--seg)}
@media(hover:hover){.list>a:hover{background:var(--seg)}}
.list>:last-child{border-bottom:0}
.kv{display:block!important}
.kv small{display:block;color:var(--mute);font-size:12px;margin-bottom:2px}
.kv span{color:var(--accent);font-size:15px}
a.kv-a{color:var(--accent);font-size:15px;text-decoration:none}
.mono{font:14px ui-monospace,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.seg{display:flex;background:var(--seg);border-radius:12px;padding:3px;margin-top:10px}
.seg button{flex:1;background:none;color:var(--mute);border-radius:9px;padding:8px 0;font-size:14px;transition:background .25s,color .25s,box-shadow .25s}
.seg button.on{background:var(--seg-on);color:var(--ink);box-shadow:0 1px 4px rgba(0,0,0,.18)}
input.sw{appearance:none;-webkit-appearance:none;width:51px;height:31px;border-radius:16px;background:var(--seg);border:0;position:relative;transition:background .25s;flex:none;margin:0;cursor:pointer;padding:0}
input.sw::after{content:"";position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:transform .3s var(--spring)}
input.sw:checked{background:var(--accent)}
input.sw:checked::after{transform:translateX(20px)}
input.rd{appearance:none;-webkit-appearance:none;width:24px;height:24px;border-radius:50%;border:2px solid var(--line);flex:none;margin:0;display:grid;place-items:center;cursor:pointer;padding:0;transition:background .2s,border-color .2s}
input.rd:checked{background:var(--accent);border-color:var(--accent)}
input.rd:checked::after{content:"\2713";color:#fff;font-size:14px;line-height:1}
/* ---------- директ ---------- */
.folders{display:flex;gap:6px;margin:0 0 12px;overflow-x:auto;scrollbar-width:none}
.folders::-webkit-scrollbar{display:none}
.folders button{background:var(--seg);color:var(--mute);border-radius:999px;padding:7px 14px;font-size:14px;font-weight:500;white-space:nowrap}
.folders button.on{background:var(--pillbg);color:var(--accent)}
.folders i{font-style:normal;background:var(--accent);color:var(--on);border-radius:9px;font-size:11px;padding:1px 6px;margin-left:6px}
.rcs{background:var(--card);border-radius:18px;overflow:hidden;border:1px solid var(--line);margin-bottom:8px}
.rc{display:flex;gap:12px;align-items:center;padding:10px 14px;text-decoration:none;color:inherit;position:relative;transition:background .15s}
.rc+.rc::before{content:"";position:absolute;left:80px;right:0;top:0;border-top:1px solid var(--line)}
.rc:active{background:var(--seg)}
@media(hover:hover){.rc:hover{background:var(--seg)}}
.rc-ava{width:54px;height:54px;border-radius:50%;flex:none;display:grid;place-items:center;color:#fff;font-weight:600;font-size:21px;object-fit:cover}
.rc-b{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.rc-t,.rc-s{display:flex;justify-content:space-between;gap:8px;align-items:center}
.rc-t b{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc-t time{font-size:13px;color:var(--mute);flex:none}
.rc-x{color:var(--mute);font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{flex:none;min-width:22px;text-align:center;padding:2px 7px;border-radius:11px;background:var(--accent);color:var(--on);font:600 13px/18px var(--font);font-style:normal}
.badge.mute{background:var(--mute)}
.rc.unread{background:linear-gradient(90deg,var(--soft),transparent 72%)}
.rc.unread .rc-t b{font-weight:700}
.rc.unread .rc-x{color:var(--ink)}
.rc.unread::after{content:"";position:absolute;left:0;top:14px;bottom:14px;width:3px;border-radius:0 3px 3px 0;background:var(--accent)}
/* ---------- экран чата ---------- */
.chat{position:fixed;z-index:20;left:0;right:0;top:var(--vvt,0px);height:var(--vvh,100vh);display:flex;flex-direction:column}
@supports(height:100dvh){.chat{height:var(--vvh,100dvh)}}
.chead{flex:none;display:flex;gap:8px;align-items:center;padding:calc(8px + var(--st)) 12px 8px;position:relative;z-index:2}
.cpill{flex:1;min-width:0;display:flex;gap:10px;align-items:center;padding:5px 16px 5px 5px;border-radius:999px;background:var(--bar);border:1px solid var(--line);color:inherit;text-decoration:none;transition:transform .2s var(--spring)}
a.cpill:active{transform:scale(.98)}
.cava{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:600;flex:none;object-fit:cover}
.cpill b{display:block;font-size:16px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cpill small{display:block;color:var(--mute);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cscroll{flex:1;min-height:0;overflow-anchor:none;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;padding:4px 12px 6px}
[data-bottom]{min-height:100%;display:flex;flex-direction:column;justify-content:flex-end;max-width:780px;margin:0 auto}
.sys{align-self:center;text-align:center;color:var(--mute);font-size:13px;margin:10px auto;padding:3px 12px;border-radius:12px;background:var(--seg);max-width:90%}
.bubble{position:relative;max-width:min(80%,540px);width:fit-content;padding:7px 12px 7px;border-radius:18px 18px 18px 6px;margin:3px 0;background:var(--theirs);border:1px solid var(--line);overflow-wrap:anywhere;touch-action:pan-y;transition:transform .3s var(--spring);-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
.bubble.me{margin-left:auto;border-radius:18px 18px 6px 18px;background:var(--mine);color:#fff;border-color:transparent}
.bubble .who2{display:block;font-size:13px;color:var(--accent);font-weight:600;text-decoration:none}
.mt{white-space:pre-wrap;overflow-wrap:anywhere}
.bt{float:right;margin:7px 0 -3px 10px;font-size:11.5px;line-height:1.2;opacity:.6;white-space:nowrap}
.bubble.me .bt{opacity:.78}
.bubble.me a.hash{color:#fff}
.ed{font-style:normal;margin-right:4px}
.bubble .pic{width:min(260px,62vw);max-height:340px;margin:0 0 4px;border-radius:13px}
.bubble.media{padding:4px}
.bubble.media .bt{position:absolute;right:10px;bottom:9px;float:none;margin:0;padding:1px 7px;border-radius:9px;background:rgba(0,0,0,.45);color:#fff;opacity:1}
.bubble.media .pic{margin:0}
.bubble.media .rxs{padding:0 6px 4px}
.bubble.pending{opacity:.7}
.bubble.press{transform:scale(.97)}
.new,.bubble.pending{animation:bin .34s var(--spring)}
@keyframes bin{from{opacity:0;transform:translate3d(0,14px,0) scale(.94)}}
.bubble::after{content:"";position:absolute;top:50%;width:30px;height:30px;margin-top:-15px;border-radius:50%;background:var(--seg) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='%238a8083' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M9 14l-5-5 5-5M4 9h10a6 6 0 0 1 6 6v3'/%3E%3C/svg%3E") center/16px no-repeat;opacity:var(--p,0);transform:scale(calc(.4 + var(--p,0) * .6));pointer-events:none}
.bubble.me::after{right:-40px}
.bubble:not(.me)::after{left:-40px}
.quote{display:flex;flex-direction:column;border-left:3px solid var(--accent);background:var(--soft);border-radius:8px;padding:3px 8px;margin:0 0 5px;font-size:13px;cursor:pointer;min-width:0}
.bubble.me .quote{background:rgba(255,255,255,.16);border-left-color:#fff}
.quote b{font-size:12px}
.quote span{opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
.cmsg{position:relative;max-width:min(92%,580px);width:fit-content;background:var(--theirs);border:1px solid var(--line);border-radius:18px 18px 18px 6px;padding:9px 14px 7px;margin:4px 0;overflow-wrap:anywhere;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
.cmsg p{margin:0;white-space:pre-wrap}
.cht{display:block;font-size:16.5px;font-weight:700;margin-bottom:6px;letter-spacing:-.2px}
.cmsg .pic{margin:0 0 6px}
.cmsg time{display:block;text-align:right;color:var(--mute);font-size:12px;margin-top:2px}
[data-nocopy],[data-nocopy] *{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
@keyframes hl{30%{box-shadow:0 0 0 3px var(--accent)}}
.hl{animation:hl 1.1s ease}
.rxs{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px;clear:both}
.rxc{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:999px;background:var(--seg);color:var(--ink);font-size:13px;font-weight:500;transition:transform .2s var(--spring)}
.rxc.mine{background:var(--soft);box-shadow:inset 0 0 0 1px var(--accent)}
.bubble.me .rxc{background:rgba(255,255,255,.2);color:#fff;box-shadow:none}
.bubble.me .rxc.mine{background:rgba(255,255,255,.34)}
.em{display:inline-block;font-style:normal;line-height:1}
.composer{flex:none;display:flex;flex-direction:column;gap:6px;width:100%;max-width:804px;margin:0 auto;padding:6px 10px calc(10px + var(--sb))}
.kb .composer{padding-bottom:8px}
.replybar{display:flex;align-items:center;gap:10px;background:var(--bar);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:14px;padding:6px 6px 6px 10px;animation:bin .25s var(--spring)}
.replybar div{flex:1;min-width:0;display:flex;flex-direction:column;font-size:13px}
.replybar b{color:var(--accent)}
.replybar span{color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.replybar .x{background:none;color:var(--mute);font-size:24px;line-height:1;padding:2px 8px}
.cbox{display:flex;align-items:flex-end;gap:4px;background:var(--bar);border:1px solid var(--line);border-radius:24px;padding:4px;box-shadow:0 4px 18px rgba(0,0,0,.08);transition:border-color .2s}
.cbox:focus-within{border-color:var(--accent)}
.cbtn{flex:none;width:40px;height:40px;margin:0;border-radius:50%;display:grid;place-items:center;color:var(--mute);cursor:pointer;transition:color .2s,background .2s}
.cbtn:hover{color:var(--accent);background:var(--seg)}
.cbox textarea{flex:1;width:auto;min-width:0;min-height:0;height:40px;max-height:140px;border:0;border-radius:0;background:none;resize:none;padding:10px 6px;font:inherit;font-size:16px;line-height:20px;color:var(--ink)}
.cbox textarea:focus{background:none;border:0}
.send{flex:none;width:40px;height:40px;border-radius:50%;padding:0;display:grid;place-items:center;background:var(--accent);color:var(--on);transform:scale(.3) rotate(-60deg);opacity:0;pointer-events:none;transition:transform .35s var(--spring),opacity .15s}
.composer.has .send{transform:none;opacity:1;pointer-events:auto}
.composer.has .send:active{transform:scale(.88)}
.wide{display:block;width:100%;border-radius:999px;padding:14px;background:var(--bar);border:1px solid var(--line);color:var(--accent);font-weight:600;text-align:center;text-decoration:none;box-shadow:0 4px 18px rgba(0,0,0,.08)}
/* ---------- всплывающие слои ---------- */
.ctx{position:fixed;z-index:80;background:var(--bar);border:1px solid var(--line);border-radius:999px;padding:4px 6px;box-shadow:0 12px 34px rgba(0,0,0,.3);animation:pop .24s var(--spring)}
.rxp{display:flex;gap:2px}
.rxp button,.rxbar button{background:none;font-size:25px;padding:5px 7px;border-radius:50%;line-height:1}
.rxp button:hover,.rxbar button:hover{background:var(--seg);transform:scale(1.2)}
.ovl{position:fixed;inset:0;z-index:90}
.ovl-bg{position:absolute;inset:0;background:rgba(0,0,0,.38);animation:fadein .2s ease}
.ovl.out .ovl-bg{animation:fadeout .22s ease forwards}
.ovl.out .rxbar,.ovl.out .mmenu{animation:menuout .17s ease forwards;pointer-events:none}
@keyframes menuout{to{opacity:0;transform:scale(.9)}}
.settle{animation:settle .35s ease}
@keyframes settle{from{opacity:.7}}
@keyframes fadein{from{opacity:0}}
@keyframes fadeout{to{opacity:0}}
.ovl-msg{position:fixed;margin:0!important;max-height:42vh;overflow:hidden;pointer-events:none;will-change:transform;box-shadow:0 12px 34px rgba(0,0,0,.28)}
.rxbar{position:fixed;display:flex;gap:2px;padding:5px 8px;border-radius:999px;background:var(--bar);border:1px solid var(--line);box-shadow:0 10px 30px rgba(0,0,0,.35);animation:pop .26s var(--spring);transform-origin:center bottom}
.rxbar button{animation:emo .45s var(--spring) backwards;will-change:transform}
.rxbar button:nth-child(2){animation-delay:.03s}.rxbar button:nth-child(3){animation-delay:.06s}.rxbar button:nth-child(4){animation-delay:.09s}.rxbar button:nth-child(5){animation-delay:.12s}
@keyframes emo{from{opacity:0;transform:translateY(10px) scale(.4)}}
.mmenu{position:fixed;width:min(250px,calc(100vw - 24px));padding:6px;border-radius:18px;background:var(--bar);border:1px solid var(--line);box-shadow:0 14px 40px rgba(0,0,0,.4);animation:popin .26s var(--spring);transform-origin:top center}
.mmenu button{display:flex;align-items:center;gap:12px;width:100%;text-align:left;background:none;color:var(--ink);padding:12px;border-radius:12px;font-size:16px;font-weight:400}
.mmenu button:active,.mmenu button:hover{background:var(--seg);transform:none}
.mmenu button svg{width:20px;height:20px;flex:none}
.mmenu hr{border:0;border-top:1px solid var(--line);margin:4px 8px}
.lb{position:fixed;inset:0;z-index:95;background:rgba(0,0,0,.9);display:grid;place-items:center;animation:fadein .22s ease;cursor:zoom-out}
.lb img{max-width:96vw;max-height:92vh;border-radius:10px}
.toast{position:fixed;left:50%;bottom:calc(100px + var(--sb));transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:14px;font-size:14px;z-index:130;animation:toast .3s var(--spring);max-width:calc(100% - 32px);text-align:center}
@keyframes toast{from{opacity:0;transform:translate(-50%,12px) scale(.95)}}
.pop{position:fixed;z-index:70;top:68px;right:24px;width:380px;max-height:72vh;overflow:auto;overscroll-behavior:contain;padding:16px;border-radius:22px;background:var(--bar);border:1px solid var(--line);box-shadow:0 18px 50px rgba(0,0,0,.3);animation:popin .26s var(--spring);transform-origin:top right}
.pop h1{font-size:19px}
.pop .card{margin-bottom:8px}
.cmenu{position:absolute;top:100%;right:12px;margin-top:4px}
@keyframes pop{from{opacity:0;transform:scale(.85)}}
@keyframes popin{from{opacity:0;transform:translateY(-6px) scale(.94)}}
@keyframes rise{from{opacity:0;transform:translate3d(0,12px,0)}}
main.stagger>*,main.stagger .rc{animation:rise .45s var(--ease) both}
main.stagger>.rcs{animation:none}
main.stagger>:nth-child(2),main.stagger .rc:nth-child(2){animation-delay:.035s}
main.stagger>:nth-child(3),main.stagger .rc:nth-child(3){animation-delay:.07s}
main.stagger>:nth-child(4),main.stagger .rc:nth-child(4){animation-delay:.1s}
main.stagger>:nth-child(5),main.stagger .rc:nth-child(5){animation-delay:.13s}
main.stagger>:nth-child(6),main.stagger .rc:nth-child(6){animation-delay:.16s}
main.stagger>:nth-child(n+7),main.stagger .rc:nth-child(n+7){animation-delay:.19s}
button.like.on,.rxc.bump{animation:pop .35s var(--spring)}
@keyframes beat{0%,100%{transform:scale(1)}30%{transform:scale(1.4)}60%{transform:scale(.95)}}
@keyframes flick{0%,100%{transform:scale(1) rotate(0)}25%{transform:scale(1.18) rotate(-7deg)}75%{transform:scale(1.1) rotate(7deg)}}
@keyframes bob{50%{transform:translateY(-4px) rotate(-14deg)}}
@keyframes sink{50%{transform:translateY(4px) rotate(14deg)}}
@keyframes wob{25%{transform:rotate(-12deg)}75%{transform:rotate(12deg)}}
@media(hover:hover){[data-e=like]:hover .em{animation:bob 1s ease-in-out infinite}[data-e=dislike]:hover .em{animation:sink 1s ease-in-out infinite}[data-e=heart]:hover .em{animation:beat .9s ease-in-out infinite}[data-e=fire]:hover .em{animation:flick .8s ease-in-out infinite}[data-e=stone]:hover .em{animation:wob 1.4s ease-in-out infinite}
 .card.lift,.post{transition:transform .25s var(--ease)}}
.rxc.mine[data-e=like] .em{animation:bob 1s ease-in-out 2}.rxc.mine[data-e=dislike] .em{animation:sink 1s ease-in-out 2}.rxc.mine[data-e=heart] .em{animation:beat .9s ease-in-out 2}.rxc.mine[data-e=fire] .em{animation:flick .8s ease-in-out 2}.rxc.mine[data-e=stone] .em{animation:wob 1.4s ease-in-out 2}
::view-transition-old(root),::view-transition-new(root){animation-duration:.32s}
/* ---------- галочка ---------- */
.vb{display:inline-grid;place-items:center;flex:none;width:18px;height:18px;margin-left:4px;vertical-align:-3px;color:var(--accent);border-radius:50%;cursor:pointer;transition:transform .25s var(--spring)}
.vb svg{width:18px;height:18px;display:block}
.vb:active{transform:scale(.8)}
.hero h1 .vb,.hero h1 .vb svg{width:24px;height:24px}
/* ---------- лента ---------- */
.cprompt{display:flex;align-items:center;gap:12px;padding:10px 10px 10px 12px;margin:0 0 14px;background:var(--card);border:1px solid var(--line);border-radius:22px;text-decoration:none;color:var(--mute);transition:transform .25s var(--spring),border-color .25s}
.cprompt .ava{width:40px;height:40px;font-size:16px}
.cprompt>span:not(.ava){flex:1;min-width:0;font-size:15.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cprompt:active{transform:scale(.98)}
@media(hover:hover){.cprompt:hover{border-color:var(--accent)}}
.schip{flex:none;font-size:13px;font-weight:600;color:var(--warm);background:rgba(240,140,0,.13);padding:4px 9px;border-radius:999px}
.cplus{flex:none;display:grid;place-items:center;width:38px;height:38px;border-radius:50%;background:var(--accent);color:var(--on);transition:transform .3s var(--spring)}
.cprompt:hover .cplus{transform:rotate(90deg)}
.post .phead{display:flex;align-items:center;gap:10px}
.pa{flex:none;text-decoration:none;display:block}
.post .ava{width:42px;height:42px;font-size:17px}
.pwho{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3}
.pn{display:flex;align-items:center;min-width:0}
.pn .who{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-decoration:none}
.pwho small{color:var(--mute);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.phead .tag{flex:none;align-self:flex-start;margin-top:2px}
.post p{margin:10px 0 8px}
.pact{display:flex;align-items:center;gap:2px;margin:4px -8px -6px}
.act{display:inline-flex;align-items:center;gap:6px;background:none;color:var(--mute);padding:7px 9px;border-radius:12px;font-size:14px;font-weight:500;text-decoration:none;cursor:pointer;border:0;transition:background .2s,color .2s,transform .25s var(--spring)}
.act svg{width:20px;height:20px}
.act:active{transform:scale(.88)}
@media(hover:hover){.act:hover{background:var(--seg);color:var(--ink)}}
.pact .ml{margin-left:auto}
.empty{display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center;padding:34px 20px;color:var(--mute);background:var(--card);border:1px dashed var(--line);border-radius:18px;font-size:14px}
.empty b{color:var(--ink);font-size:16px}
.news .btn{margin-top:10px}
.mk-s svg{width:24px;height:24px;display:block}
.wart .fl1{animation:fl 6s ease-in-out infinite}
.wart .fl2{animation:fl 7s ease-in-out -2s infinite}
.wart .fl3{animation:fl 5s ease-in-out -1s infinite}
.wart .dot{animation:pulse 2.4s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
@keyframes fl{50%{transform:translateY(-8px)}}
@keyframes pulse{50%{transform:scale(1.4);opacity:.7}}
/* ---------- профиль ---------- */
.pbar{display:flex;justify-content:space-between;align-items:center;min-height:42px;margin-bottom:2px}
.pillbtn{padding:9px 16px;border-radius:999px;background:var(--bar);border:1px solid var(--line);color:var(--ink);font-weight:500;text-decoration:none;transition:transform .2s var(--spring)}
.pillbtn:active{transform:scale(.94)}
.avring{width:118px;height:118px;margin:0 auto;border-radius:50%;padding:4px;background:conic-gradient(from 200deg,var(--accent),var(--soft) 45%,var(--accent));animation:ringin .7s var(--spring) both}
.avring .pava{width:110px;height:110px;font-size:44px;border:3px solid var(--bg);margin:0}
@keyframes ringin{from{transform:scale(.8);opacity:0}}
.hero h1{display:flex;align-items:center;justify-content:center;font-size:25px;margin:14px 0 2px;padding:0 8px}
.role{display:inline-block;margin:2px 0 6px;padding:3px 11px;border-radius:999px;background:var(--soft);color:var(--accent);font-size:13px;font-weight:600}
.hero small{display:block}
.hero small.online{color:var(--accent);font-weight:500}
.sech{display:flex;justify-content:space-between;margin:18px 12px 8px;font-size:13px;color:var(--mute);text-transform:uppercase;letter-spacing:.4px}
.chcard{display:flex;gap:12px;align-items:center;padding:12px 14px;background:var(--card);border:1px solid var(--line);border-radius:20px;text-decoration:none;color:inherit;margin-bottom:12px;transition:transform .25s var(--spring)}
.chcard .cava{width:52px;height:52px;font-size:21px}
.chcard:active{transform:scale(.98)}
.list.info{margin-top:4px}
.kvrow{display:flex!important;justify-content:space-between;align-items:center}
.kvt{color:var(--ink)!important;white-space:pre-wrap;overflow-wrap:anywhere}
.cpy{background:none;color:var(--accent);padding:6px;border-radius:10px}
.ptabs{position:relative;display:flex;gap:4px;margin:20px 0 12px;padding:0 4px;border-bottom:1px solid var(--line)}
.ptabs button{background:none;color:var(--mute);padding:10px 12px;border-radius:0;font-weight:600;font-size:15px;transition:color .25s}
.ptabs button:active{transform:none}
.ptabs button.on{color:var(--accent)}
.ptabs button i{font-style:normal;font-size:12px;margin-left:6px;padding:1px 7px;border-radius:9px;background:var(--seg);color:var(--mute)}
.ptabs .ul{position:absolute;bottom:-1px;left:0;width:0;height:3px;border-radius:3px 3px 0 0;background:var(--accent);transition:transform .45s var(--spring),width .45s var(--spring)}
.mval{float:right;color:var(--mute);margin-left:12px}
/* ---------- настройки ---------- */
.mecard{display:flex;align-items:center;gap:14px;padding:12px 14px;margin-bottom:16px;background:var(--card);border:1px solid var(--line);border-radius:20px;text-decoration:none;color:inherit;transition:transform .25s var(--spring)}
.mecard:active{transform:scale(.98)}
.mecard .ava{width:58px;height:58px;font-size:23px}
.mecard b{display:flex;align-items:center;font-size:18px}
.mecard small{color:var(--mute);font-size:14px}
.chev{flex:none;color:var(--mute);opacity:.7}
.list .val{display:flex;align-items:center;gap:6px;color:var(--mute);font-size:15px;white-space:nowrap}
.li{display:flex;align-items:center;gap:12px}
.si{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:var(--c);color:#fff;flex:none;font-style:normal}
.si svg{width:18px;height:18px}
.list.ic>a{padding:10px 14px}
.list.ic{margin-bottom:16px}
.cnt{background:var(--seg);color:var(--mute);font-size:13px;padding:1px 8px;border-radius:9px}
.wide-btn{width:100%;margin-top:4px;color:var(--danger)!important;padding:13px}
.lt{display:flex;flex-direction:column;gap:2px;min-width:0}
.lt b{font-weight:500;font-size:16px}
.lt small{color:var(--mute);font-size:13px;line-height:1.3}
.list>label{cursor:pointer}
.lookprev{display:flex;flex-direction:column;padding:16px 14px;border-radius:22px;border:1px solid var(--line);margin-bottom:6px;background:var(--seg)}
.lookprev .bubble{margin:3px 0}
.sw-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(74px,1fr));gap:6px;padding:10px}
.swatch{display:flex;flex-direction:column;align-items:center;gap:7px;background:none;color:var(--ink);padding:8px 2px;border-radius:14px;font-size:12px;font-weight:500}
.swatch:active{transform:scale(.92)}
.swatch i{width:36px;height:36px;border-radius:50%;background:var(--sw);transition:box-shadow .3s,transform .35s var(--spring)}
.swatch.on i{box-shadow:0 0 0 3px var(--card),0 0 0 5px var(--sw);transform:scale(1.06)}
.bgs{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.bgopt{display:flex;flex-direction:column;gap:7px;align-items:center;background:none;color:var(--ink);padding:0;font-size:13px;font-weight:500}
.bgopt:active{transform:scale(.95)}
.bgprev{display:block;width:100%;aspect-ratio:3/4;border-radius:14px;border:2px solid var(--line);background-color:var(--bg);transition:border-color .25s,transform .35s var(--spring)}
.bgopt.on .bgprev{border-color:var(--accent);transform:scale(1.04)}
[data-bgp=pattern]{background-image:radial-gradient(circle at 85% 0,var(--soft),transparent 65%),repeating-linear-gradient(45deg,var(--dot) 0 1px,transparent 1px 11px)}
[data-bgp=dots]{background-image:radial-gradient(var(--dot) 1.1px,transparent 1.6px);background-size:10px 10px}
[data-bgp=gradient]{background-image:radial-gradient(circle at 95% 0,var(--glow),transparent 65%),radial-gradient(circle at 0 100%,var(--glow),transparent 65%)}
/* ---------- окно снизу ---------- */
.sheet-bg{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.42);animation:fadein .22s ease}
.sheet{position:fixed;z-index:101;left:0;right:0;bottom:0;margin:0 auto;max-width:520px;background:var(--bar);border-radius:26px 26px 0 0;padding:10px 20px calc(20px + var(--sb));box-shadow:0 -10px 40px rgba(0,0,0,.25);animation:sheetin .42s var(--ease);will-change:transform}
.sheet-bg.out{animation:fadeout .22s ease forwards}
.sheet.out{animation:sheetout .24s cubic-bezier(.4,0,1,1) forwards}
@keyframes sheetin{from{transform:translate3d(0,100%,0)}}
@keyframes sheetout{to{transform:translate3d(0,100%,0)}}
.sheet .grab{width:38px;height:5px;border-radius:3px;background:var(--line);margin:0 auto 14px}
.sheet h3{margin:0 0 4px;font-size:19px;text-align:center}
.sheet p{margin:0 0 10px;color:var(--mute);font-size:14px;text-align:center}
.ttlval{display:flex;align-items:center;justify-content:center;gap:8px;font-size:27px;font-weight:700;color:var(--accent);margin:6px 0 16px;min-height:38px}
.ttlval svg{width:26px;height:26px}
.ttlval span{display:inline-block}
.ttlval span.flip{animation:flip .3s var(--spring)}
@keyframes flip{from{opacity:0;transform:translateY(8px) scale(.9)}}
input.range{-webkit-appearance:none;appearance:none;display:block;width:100%;height:30px;background:none!important;padding:0;border:0!important;margin:0;cursor:pointer}
input.range::-webkit-slider-runnable-track{height:6px;border-radius:3px;background:linear-gradient(90deg,var(--accent) var(--pct,0%),var(--seg) var(--pct,0%))}
input.range::-webkit-slider-thumb{-webkit-appearance:none;width:28px;height:28px;border-radius:50%;background:#fff;margin-top:-11px;box-shadow:0 2px 8px rgba(0,0,0,.3);transition:transform .25s var(--spring)}
input.range:active::-webkit-slider-thumb{transform:scale(1.15)}
input.range::-moz-range-track{height:6px;border-radius:3px;background:var(--seg)}
input.range::-moz-range-progress{height:6px;border-radius:3px;background:var(--accent)}
input.range::-moz-range-thumb{width:28px;height:28px;border:0;border-radius:50%;background:#fff;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.ticks{position:relative;height:18px;margin:8px 0 22px;font-size:11.5px;color:var(--mute)}
.ticks span{position:absolute;top:0;left:calc(14px + var(--k) * (100% - 28px) / 5);transform:translateX(-50%);white-space:nowrap;transition:color .2s;cursor:pointer;padding:0 2px}
.ticks span:first-child{transform:translateX(-14px)}.ticks span:last-child{transform:translateX(calc(-100% + 14px))}
.ticks span.on{color:var(--accent);font-weight:600}
.sheet .main{display:block;width:100%;padding:14px;border-radius:14px;font-weight:600}
/* ---------- обрезка фото ---------- */
.crop{position:fixed;inset:0;z-index:110;background:#000;display:flex;flex-direction:column;animation:fadein .25s;color:#fff}
.crop-top{display:flex;justify-content:space-between;align-items:center;padding:calc(10px + var(--st)) 12px 10px}
.crop-top b{font-size:17px}
.crop-top button{background:none;color:#fff;font-weight:500;padding:8px 10px}
.crop-top button.ok{font-weight:700;color:#4ea1ff}
.crop-area{flex:1;position:relative;overflow:hidden;touch-action:none;cursor:grab}
.crop-area:active{cursor:grabbing}
.crop-area img{position:absolute;left:0;top:0;transform-origin:0 0;-webkit-user-select:none;user-select:none;-webkit-user-drag:none;max-width:none;will-change:transform}
.crop-hole{position:absolute;border-radius:50%;box-shadow:0 0 0 9999px rgba(0,0,0,.62);border:2px solid rgba(255,255,255,.9);pointer-events:none}
.crop-bot{display:flex;align-items:center;gap:14px;padding:16px 24px calc(22px + var(--sb))}
.crop-bot svg{flex:none;width:18px;height:18px;opacity:.75}
.crop-bot input.range::-webkit-slider-runnable-track{background:linear-gradient(90deg,#fff var(--pct,0%),rgba(255,255,255,.25) var(--pct,0%))}
.crop-note{text-align:center;font-size:13px;opacity:.65;padding:0 20px}
/* ---------- редактирование профиля, канал ---------- */
.avedit{display:flex;flex-direction:column;align-items:center;gap:8px;margin:2px 0 4px;cursor:pointer;color:var(--accent)}
.avwrap{position:relative;display:block}
.avedit .pava{width:100px;height:100px;font-size:40px}
.avcam{position:absolute;right:-2px;bottom:-2px;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:var(--accent);color:var(--on);border:3px solid var(--card);transition:transform .3s var(--spring)}
.avedit:active .avcam{transform:scale(.85)}
.avcam svg{width:16px;height:16px}
.avhint{font-weight:600;font-size:15px}
.slugin{display:flex;align-items:center;border:1px solid var(--line);border-radius:12px;background:var(--seg);padding-left:13px;transition:border-color .2s}
.slugin span{color:var(--mute)}
.slugin input{border:0!important;background:none!important;padding-left:3px}
.slugin:focus-within{border-color:var(--accent)}
.hint{margin:6px 2px 0;font-size:13px;color:var(--mute)}
.flash.err{background:rgba(255,59,48,.1);border-color:rgba(255,59,48,.3)}
.btn.ghost{background:var(--seg);color:var(--ink)}
/* ---------- правая колонка, системные сообщения ---------- */
.side a.upd{display:block;text-decoration:none;color:inherit;transition:border-color .2s}
.side a.upd:hover{border-color:var(--accent)}
.updi{display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}
.updi b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.updi time{color:var(--mute);font-size:12px;flex:none}
.updgo{display:block;margin-top:8px;color:var(--accent);font-weight:600;font-size:14px}
.tsys{display:inline-flex;align-items:center;gap:6px}
.tsys svg{flex:none}
/* ---------- медиа в канале ---------- */
.cmsg.mpost{width:min(92%,560px)}
.gal{display:grid;gap:3px;margin:-5px -10px 9px;border-radius:15px 15px 6px 6px;overflow:hidden}
.gal.g2,.gal.g3,.gal.g4{grid-template-columns:1fr 1fr}
.gal.g3>:first-child{grid-column:1/-1}
.gal .pic{margin:0;border-radius:0;width:100%;height:100%;max-height:none}
.gal.g1 .pic{max-height:480px}
.gal:not(.g1)>*{aspect-ratio:1}
.gal.g3>:first-child{aspect-ratio:16/9}
.vid{position:relative;background:#000;overflow:hidden;cursor:pointer;min-height:120px}
.vid video{display:block;width:100%;height:100%;max-height:520px;object-fit:cover}
.gal.g1 .vid video{object-fit:contain;background:#000}
.vplay{position:absolute;left:50%;top:50%;width:64px;height:64px;margin:-32px 0 0 -32px;border-radius:50%;padding:0;display:grid;place-items:center;background:rgba(0,0,0,.38);color:#fff;box-shadow:inset 0 0 0 2px rgba(255,255,255,.9),0 0 0 7px rgba(255,255,255,.12);transition:transform .4s var(--spring),opacity .25s}
.vplay svg{width:26px;height:26px;margin-left:3px}
.vid:hover .vplay{transform:scale(1.08)}
.vid.playing .vplay{opacity:0;transform:scale(.5);pointer-events:none}
.vsnd{position:absolute;right:10px;bottom:14px;width:34px;height:34px;border-radius:50%;padding:0;display:grid;place-items:center;background:rgba(0,0,0,.5);color:#fff;opacity:0;transition:opacity .25s,transform .25s var(--spring)}
.vsnd svg{width:17px;height:17px}
.vsnd .on{display:none}.vid.sound .vsnd .on{display:inline}.vid.sound .vsnd .off{display:none}
.vid.playing .vsnd{opacity:1}
.vbar{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.18)}
.vbar b{display:block;height:100%;background:var(--accent);transform-origin:0 50%;transform:scaleX(var(--t,0))}
.vlabel{position:absolute;left:10px;top:10px;padding:3px 9px;border-radius:999px;background:rgba(0,0,0,.5);color:#fff;font-size:12px;font-weight:600}
.ba{position:relative;margin:-5px -10px 9px;border-radius:15px 15px 6px 6px;overflow:hidden;touch-action:pan-y;-webkit-user-select:none;user-select:none;cursor:ew-resize;--x:50%;background:#000}
.ba img{display:block;width:100%;max-height:600px;object-fit:cover;object-position:top;pointer-events:none;-webkit-user-drag:none}
.ba-b{position:absolute;inset:0;clip-path:inset(0 calc(100% - var(--x)) 0 0);will-change:clip-path}
.ba-b img{width:100%;height:100%;max-height:none;object-position:top}
.ba-h{position:absolute;top:0;bottom:0;left:var(--x);width:3px;margin-left:-1.5px;background:#fff;box-shadow:0 0 14px rgba(0,0,0,.45);pointer-events:none}
.ba-h b{position:absolute;top:50%;left:50%;width:42px;height:42px;margin:-21px 0 0 -21px;border-radius:50%;background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='22' height='22' viewBox='0 0 24 24' fill='none' stroke='%23111' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M9 7l-5 5 5 5M15 7l5 5-5 5'/%3E%3C/svg%3E") center no-repeat;box-shadow:0 4px 16px rgba(0,0,0,.35);transition:transform .3s var(--spring)}
.ba.drag .ba-h b{transform:scale(1.12)}
.ba-l,.ba-r{position:absolute;top:10px;padding:4px 11px;border-radius:999px;font-size:11.5px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;pointer-events:none}
.ba-l{left:10px;background:rgba(0,0,0,.55);color:#fff}
.ba-r{right:10px;background:var(--accent);color:var(--on)}
.tray{background:var(--bar);border:1px solid var(--line);border-radius:18px;padding:8px;display:flex;flex-direction:column;gap:8px;animation:bin .25s var(--spring)}
.tray-items{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none}
.tray-items::-webkit-scrollbar{display:none}
.titem{position:relative;flex:none;width:76px;height:76px;border-radius:13px;overflow:hidden;background:#000;animation:pop .32s var(--spring)}
.titem img,.titem video{width:100%;height:100%;object-fit:cover;display:block}
.titem .tx{position:absolute;top:4px;right:4px;width:22px;height:22px;border-radius:50%;padding:0;background:rgba(0,0,0,.6);color:#fff;font-size:15px;line-height:22px;text-align:center}
.titem .tl{position:absolute;left:4px;bottom:4px;font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px;background:rgba(0,0,0,.6);color:#fff;text-transform:uppercase}
.titem .tl.after{background:var(--accent);color:var(--on)}
.batog:not([hidden]){display:flex;align-items:center;gap:10px;margin:0 4px;color:var(--ink);font-size:14px;cursor:pointer}
.upbar{height:4px;border-radius:2px;background:var(--seg);overflow:hidden}
.upbar b{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
/* ---------- телефон: экран чата без шапки и панели ---------- */
@media(max-width:1023px){
 .app.full .top,.app.bare .top{display:none}
}
/* ---------- компьютер ---------- */
@media(min-width:1024px){
 :root{--sl:max(24px,calc((100vw - 1240px) / 2))}
 html{scrollbar-gutter:stable}
 html.chatmode{scrollbar-gutter:auto}
 .app{max-width:none;margin:0;padding:84px var(--sl) 64px calc(var(--sl) + 256px)}
 main{max-width:660px;margin:0 auto}
 .app>.flash{max-width:660px;margin:0 auto 12px}
 .top{position:fixed;z-index:55;top:0;left:0;right:0;height:70px;min-height:0;margin:0;padding:0 var(--sl);pointer-events:none;background:linear-gradient(var(--bg) 60%,transparent)}
 .top>*{pointer-events:auto}
 .chatmode .top{background:none}
 .thbtn{display:grid}
 .tabbar,.tabbar.fullhide,.kb .tabbar{left:var(--sl);top:84px;bottom:auto;width:224px;transform:none;opacity:1;pointer-events:auto}
 .tabs4{flex-direction:column;padding:6px;border-radius:22px;box-shadow:none}
 .tabbar .pill{top:6px;left:6px;bottom:auto;width:calc(100% - 12px);height:calc((100% - 12px) / 4);border-radius:16px;transform:translate3d(0,calc(var(--i,0) * 100%),0)}
 .tabs4 a{flex-direction:row;gap:12px;padding:12px 16px;font-size:15px;justify-content:flex-start;letter-spacing:0}
 .tabs4 a svg{width:22px;height:22px}
 .tabs4 a.on svg{transform:scale(1.06)}
 .tbadge{position:static;margin-left:auto;box-shadow:none}
 .chat{left:calc(var(--sl) + 256px);right:var(--sl);top:84px;bottom:20px;height:auto!important;border:1px solid var(--line);border-radius:24px;overflow:hidden;background:rgba(244,239,237,.55)}
 :root[data-theme="dark"] .chat{background:rgba(15,11,12,.55)}
 .app.guest{padding-left:var(--sl)}
 .guest .chat{left:var(--sl)}
 .chead{padding:10px 14px;background:var(--bar);border-bottom:1px solid var(--line)}
 .chead .cpill{border:0;background:none;padding-left:0}
 .chead .rbtn{width:38px;height:38px}
 .cscroll{padding:8px 20px}
 .composer{padding:8px 16px 14px}
 .bubble::after{display:none}
 .toast{bottom:32px}
 .pop{right:var(--sl)}
 .ovl-bg{background:rgba(0,0,0,.28)}
 @media(hover:hover){.post:hover,.card.lift:hover{transform:translateY(-2px)}}
}
@media(min-width:1280px){
 .app.has-side{padding-right:calc(var(--sl) + 332px)}
 .app.has-side .side{display:block;position:fixed;top:84px;right:var(--sl);width:300px;max-height:calc(100vh - 104px);overflow:auto;scrollbar-width:none}
 .side h3{font-size:12px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.5px;margin:0 0 8px}
 .side .card{padding:12px 14px;margin-bottom:12px}
 .side a.row2{display:flex;gap:10px;align-items:center;padding:6px 0;text-decoration:none;font-size:14px}
 .side .chip{display:inline-block;margin:0 6px 6px 0;padding:3px 10px;border-radius:999px;background:var(--seg);font-size:13px;text-decoration:none;transition:background .2s}
 .side .chip:hover{background:var(--pillbg)}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style></head><body>
<div class="prog"></div>
<div class="app{{ ' guest' if not g.user }}{{ ' full' if full }}{{ ' bare' if bare }}{{ ' has-side' if g.user and side and not full }}">
<header class="top">
 <a class="brand" href="{{ url_for('feed') }}" aria-label="Thenks">
  <svg class="mk" viewBox="0 0 32 32" aria-hidden="true"><rect x=".6" y=".6" width="30.8" height="30.8" rx="9" fill="#0b0b0c"/><path d="M10 11h12M16 11v11" stroke="#fff" stroke-width="3" stroke-linecap="round"/><circle cx="22.3" cy="21.3" r="2.3" fill="#3d9bff"/></svg>
  <span>Thenks</span>
 </a>
 {% if g.user %}
 <span class="hr"><button type="button" class="rbtn thbtn" data-theme-toggle aria-label="Сменить тему"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 0 0 16z" fill="currentColor"/></svg></button>{% if plus %}<a class="rbtn gbtn" href="{{ plus }}" aria-label="Создать"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></a>{% endif %}{% if gear %}<a class="rbtn gbtn" href="{{ url_for('edit_profile') }}" aria-label="Изменить профиль"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></a>{% endif %}<a class="rbtn bell" href="{{ url_for('notifications') }}" aria-label="Уведомления"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>{% if g.unread %}<b>{{ g.unread if g.unread < 100 else '99+' }}</b>{% endif %}</a></span>
 {% else %}<span class="auth"><a href="{{ url_for('login') }}">Войти</a><a class="btn" href="{{ url_for('register') }}">Регистрация</a></span>{% endif %}
</header>
{% for m in get_flashed_messages() %}<div class="flash">{{ m|atmark }}</div>{% endfor %}
<main{% if request.endpoint == 'feed' %} class="pg-feed"{% endif %}>{{ content }}</main>
<form id="actform" method="post" hidden><input name="body"><input name="mode"></form>
{% if g.user and side and not full %}
<aside class="side">
 {% if side.tags %}<div class="card"><h3>Теги</h3>{% for t, n in side.tags %}<a class="chip" href="{{ url_for('tag', name=t) }}">#{{ t }}</a>{% endfor %}</div>{% endif %}
 {% if side.news %}<a class="card upd" href="{{ url_for('channel_view', slug=OFFICIAL_SLUG) }}"><h3>Обновления Thenks</h3>{% for n in side.news %}<span class="updi"><b>{{ n.title }}</b><time data-ut="{{ n.created }}"></time></span>{% endfor %}<span class="updgo">Открыть канал</span></a>{% endif %}
 {% if side.people %}<div class="card"><h3>Люди</h3>{% for u in side.people %}<a class="row2" href="{{ url_for('profile', username=u.username) }}">{% if u.avatar %}<img class="ava" src="{{ url_for('media', name=u.avatar) }}" alt="">{% else %}<span class="ava" style="background:hsl({{ u.username|hue }} 55% 42%)">{{ u.username[0]|upper }}</span>{% endif %}<span>{{ u.display_name or u.username }}{{ u.username|vb }}<br><i class="at muted" style="font-size:13px">@{{ u.username }}</i></span></a>{% endfor %}</div>{% endif %}
</aside>
{% endif %}
</div>
{% if g.user %}
{% set ai = ['profile','chats','feed','settings'].index(tab) if tab in ['profile','chats','feed','settings'] else -1 %}
<nav class="tabbar{{ ' fullhide' if full }}" aria-label="Разделы" data-active="{{ ai }}">
 <div class="tabs4">
  <span class="pill" style="--i:{{ [ai, 0]|max }};{{ 'opacity:0' if ai < 0 }}"></span>
  <a href="{{ url_for('profile', username=g.user.username) }}" class="{{ 'on' if ai == 0 }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></svg><span>Профиль</span></a>
  <a href="{{ url_for('inbox') }}" class="{{ 'on' if ai == 1 }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg><span>Директ</span>{% if g.unread_dm %}<i class="tbadge">{{ g.unread_dm if g.unread_dm < 100 else '99+' }}</i>{% endif %}</a>
  <a href="{{ url_for('feed') }}" class="{{ 'on' if ai == 2 }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h13a2 2 0 0 1 2 2v14H6a2 2 0 0 1-2-2zM8 8h7M8 12h7M8 16h4"/></svg><span>Лента</span></a>
  <a href="{{ url_for('settings') }}" class="{{ 'on' if ai == 3 }}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg><span>Настройки</span></a>
 </div>
</nav>
{% endif %}
<script nonce="{{ nonce }}">
(()=>{
const r=document.documentElement,rm=matchMedia('(prefers-reduced-motion: reduce)').matches;
const $=(s,x=document)=>x.querySelector(s),$$=(s,x=document)=>[...x.querySelectorAll(s)];
const LS={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
const desk=()=>matchMedia('(min-width: 1024px)').matches;
const finePtr=()=>matchMedia('(hover: hover) and (pointer: fine)').matches;
// ---------- тема ----------
const cur=()=>LS.get('theme')||'dark';
const resolve=v=>v==='auto'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):v;
const setMeta=()=>{const m=$('meta[name=theme-color]');if(m)m.content=r.dataset.theme==='dark'?'#000000':'#f2f2f7'};
const mark=()=>{$$('[data-set-theme]').forEach(b=>b.classList.toggle('on',b.dataset.setTheme===cur()));
 $$('[data-set-accent]').forEach(b=>b.classList.toggle('on',b.dataset.setAccent===(r.dataset.accent||'sky')));
 $$('[data-set-bg]').forEach(b=>b.classList.toggle('on',b.dataset.setBg===(r.dataset.bg||'pattern')))};
const setLook=(k,v)=>{LS.set(k,v);if(r.dataset[k]===v)return;const run=()=>{r.dataset[k]=v;mark()};
 if(document.startViewTransition&&!rm){try{document.startViewTransition(run);return}catch(e){}}run()};
const setTheme=v=>{LS.set('theme',v);mark();const t=resolve(v);if(t===r.dataset.theme)return;
 const run=()=>{r.dataset.theme=t;setMeta()};
 if(document.startViewTransition&&!rm){try{document.startViewTransition(run);return}catch(e){}}
 run()};
try{matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(cur()==='auto'){r.dataset.theme=resolve('auto');setMeta()}})}catch(e){}
setMeta();
// ---------- мелочи ----------
const toast=t=>{if(!t)return;$$('.toast').forEach(x=>x.remove());const e=document.createElement('div');e.className='toast';e.textContent=t;document.body.appendChild(e);setTimeout(()=>{e.style.transition='opacity .25s';e.style.opacity='0';setTimeout(()=>e.remove(),260)},2200)};
const DAYS=['вс','пн','вт','ср','чт','пт','сб'];
const pad=n=>('0'+n).slice(-2);
const hm=d=>pad(d.getHours())+':'+pad(d.getMinutes());
const fmtTimes=root=>{
 $$('[data-utc]',root).forEach(e=>{const d=new Date(e.dataset.utc.replace(' ','T')+'Z');if(!isNaN(d))e.textContent=hm(d)});
 $$('[data-ut]',root).forEach(e=>{const d=new Date(e.dataset.ut.replace(' ','T')+'Z');if(isNaN(d))return;const n=new Date();
  e.textContent=d.toDateString()===n.toDateString()?hm(d):(n-d)/864e5<6?DAYS[d.getDay()]:pad(d.getDate())+'.'+pad(d.getMonth()+1)});
};
const prog=on=>{const p=$('.prog');if(!p)return;if(on){p.classList.remove('on');void p.offsetWidth;p.classList.add('on')}else{p.classList.remove('on')}};
// ---------- чат: прокрутка и клавиатура ----------
let stick=true,ro=null,liveRaw='',lastC=null;
const sc=()=>$('.cscroll');
const nearBottom=()=>{const s=sc();return !s||s.scrollHeight-s.scrollTop-s.clientHeight<140};
const toBottom=smooth=>{const s=sc();if(!s)return;if(smooth&&!rm)s.scrollTo({top:s.scrollHeight,behavior:'smooth'});else s.scrollTop=s.scrollHeight;stick=true};
const vv=window.visualViewport;
const vvUpdate=()=>{if(!vv||desk()){r.style.removeProperty('--vvh');r.style.removeProperty('--vvt');r.classList.remove('kb');return}
 r.style.setProperty('--vvh',Math.round(vv.height)+'px');r.style.setProperty('--vvt',Math.round(vv.offsetTop)+'px');
 r.classList.toggle('kb',innerHeight-vv.height>120||(document.activeElement&&document.activeElement.matches&&document.activeElement.matches('textarea,input[type=text],input[type=password]')&&innerHeight-vv.height>60))};
if(vv){vv.addEventListener('resize',()=>{const b=stick;vvUpdate();if(b)toBottom(false)});vv.addEventListener('scroll',vvUpdate)}
addEventListener('resize',vvUpdate);
const setupChat=()=>{
 if(ro){ro.disconnect();ro=null}
 const s=sc(),box=$('[data-bottom]');r.classList.toggle('chatmode',!!s);
 if(!s||!box)return;
 stick=true;toBottom(false);
 let sh0=s.scrollHeight,ch0=s.clientHeight;
 s.addEventListener('scroll',()=>{if(s.scrollHeight!==sh0||s.clientHeight!==ch0){sh0=s.scrollHeight;ch0=s.clientHeight;if(stick)toBottom(false);return}stick=nearBottom()},{passive:true});
 if(window.ResizeObserver){ro=new ResizeObserver(()=>{if(stick)toBottom(false)});ro.observe(box);ro.observe(s)}
 $$('.flash').forEach(f=>{toast(f.textContent.trim());f.remove()});
};
const init=keep=>{
 const lv=$('.rcs[data-live]');liveRaw=lv?lv.innerHTML:'';
 fmtTimes(document);
 $$('[data-path]').forEach(e=>e.textContent=location.origin+e.dataset.path);
 mark();vvUpdate();setupChat();placeUL(true);setupVids(document);if(lastC)setCounts(lastC);
 if(!sc()&&!keep)scrollTo(0,0);
};
const placeUL=instant=>{const on=$('.ptabs button.on'),ul=$('.ptabs .ul');if(!on||!ul)return;
 if(instant)ul.style.transition='none';ul.style.width=on.offsetWidth+'px';ul.style.transform='translateX('+on.offsetLeft+'px)';
 if(instant){void ul.offsetWidth;ul.style.transition=''}};
if(document.fonts&&document.fonts.ready)document.fonts.ready.then(()=>placeUL(true));
const selectPT=b=>{$$('[data-pt]').forEach(x=>x.classList.toggle('on',x===b));
 $$('[data-ptab]').forEach(p=>{const show=p.dataset.ptab===b.dataset.pt;if(p.hidden===!show)return;p.hidden=!show;
  if(show&&!rm)p.animate([{opacity:0,transform:'translate3d(0,10px,0)'},{opacity:1,transform:'none'}],{duration:300,easing:'cubic-bezier(.2,.85,.25,1)'})});placeUL()};
// ---------- переходы между страницами без перезагрузки ----------
const fetchPage=async(url,opt,frag)=>{
 const h={'X-SPA':'1'};if(frag)h['X-Frag']='1';
 const res=await fetch(url,Object.assign({headers:h,credentials:'same-origin'},opt||{}));
 if(!(res.headers.get('content-type')||'').includes('text/html'))throw new Error('type');
 return {html:await res.text(),url:res.url,status:res.status};
};
const pre=new Map();
const prefetch=href=>{if(pre.has(href))return;pre.set(href,fetchPage(href).catch(()=>null));setTimeout(()=>pre.delete(href),8000)};
const pill=i=>{const p=$('.tabbar .pill');if(!p)return;if(i>=0){p.style.setProperty('--i',i);p.style.opacity='1'}else p.style.opacity='0'};
const syncTabbar=nb=>{
 const ob=$('.tabbar');
 if(ob&&nb){const nl=$$('.tabs4 a',nb);ob.dataset.active=nb.dataset.active;ob.className=nb.className;
  $$('.tabs4 a',ob).forEach((a,i)=>{if(!nl[i])return;a.href=nl[i].href;a.className=nl[i].className;const nbadge=nl[i].querySelector('.tbadge'),obadge=a.querySelector('.tbadge');
   if(nbadge&&obadge)obadge.textContent=nbadge.textContent;else if(nbadge)a.appendChild(nbadge.cloneNode(true));else if(obadge)obadge.remove()});
  pill(+nb.dataset.active)}
 else if(ob)ob.remove();
 else if(nb)document.body.insertBefore(document.adoptNode(nb),$('body>script'));
};
const ANIM={fwd:[34,0],back:[-34,0],tabR:[22,0],tabL:[-22,0],up:[0,14]};
const enter=kind=>{
 if(rm||!kind||kind==='none')return;
 const tgt=$('.chat')||$('main');if(!tgt)return;
 if(kind==='fade'){tgt.animate([{opacity:0},{opacity:1}],{duration:170,easing:'ease-out'});return}
 const [dx,dy]=ANIM[kind]||[0,10];
 tgt.animate([{opacity:0,transform:'translate3d('+dx+'px,'+dy+'px,0)'},{opacity:1,transform:'translate3d(0,0,0)'}],{duration:kind.startsWith('tab')?280:340,easing:'cubic-bezier(.2,.85,.25,1)'});
 const m=$('main');if(m&&!$('.chat')){m.classList.add('stagger');setTimeout(()=>m.classList.remove('stagger'),750)}
};
const swap=(page,o)=>{
 const doc=new DOMParser().parseFromString(page.html,'text/html'),na=doc.querySelector('.app');
 if(!na)throw new Error('no app');
 document.title=doc.title;
 syncTabbar(doc.querySelector('.tabbar'));
 closeAll();
 $('.app').replaceWith(document.adoptNode(na));
 if(o.push){history.pushState(null,'',page.url);depth++}else if(o.replace||page.url!==location.href)history.replaceState(null,'',page.url);
 init(o.keep);
 enter(o.anim);
};
let navId=0,depth=0;
const go=async(url,o={})=>{
 const id=++navId;closeAll();
 if(o.ti!=null)pill(o.ti);
 const t=setTimeout(()=>prog(true),120);
 try{
  const p=pre.get(url)||fetchPage(url);pre.delete(url);
  const page=await p;
  if(id!==navId)return;
  if(!page)throw new Error('net');
  swap(page,{push:o.push!==false,anim:o.anim});
 }catch(e){if(id===navId)location.href=url}
 finally{clearTimeout(t);if(id===navId)prog(false)}
};
const local=a=>{const u=new URL(a.href,location.href);return u.origin===location.origin&&!u.pathname.startsWith('/media/')&&!(u.pathname===location.pathname&&u.search===location.search&&u.hash)?u:null};
document.addEventListener('pointerdown',e=>{const a=e.target.closest&&e.target.closest('a[href]');if(!a||a.target||e.button||!local(a))return;prefetch(a.href)},{passive:true});
let hovT=0;
document.addEventListener('mouseover',e=>{const a=e.target.closest&&e.target.closest('a[href]');clearTimeout(hovT);if(!a||a.target||!local(a)||a.href===location.href)return;hovT=setTimeout(()=>prefetch(a.href),90)},{passive:true});
// ---------- действия без перезагрузки (реакции, удаление, правка) ----------
const act=async(path,body,mode)=>{
 const f=$('#actform');if(!f)return;
 const fd=new FormData(f);fd.set('body',body||'');fd.set('mode',mode||'');
 const chat=!!$('[data-bottom]');pre.clear();
 try{const page=await fetchPage(path,{method:'POST',body:fd},chat);if(chat)updateChat(page.html,'keep');else swap(page,{keep:true})}
 catch(e){toast('Не получилось. Проверь интернет.')}
};
const RX=[['like','👍'],['dislike','👎'],['heart','❤️'],['fire','🔥'],['stone','🗿']];
let menu=null;
const close=()=>{if(menu){menu.remove();menu=null}};
const openPicker=el=>{close();menu=document.createElement('div');menu.className='ctx';const row=document.createElement('div');row.className='rxp';
 RX.forEach(([k,em])=>{const b=document.createElement('button');b.type='button';b.dataset.e=k;b.innerHTML='<span class="em">'+em+'</span>';b.onclick=()=>{close();act('/react/'+el.dataset.react,'',k)};row.appendChild(b)});
 menu.appendChild(row);document.body.appendChild(menu);const b=el.getBoundingClientRect();
 menu.style.left=Math.max(8,Math.min(b.left,innerWidth-menu.offsetWidth-8))+'px';
 const top=b.top-menu.offsetHeight-8;menu.style.top=(top>8?top:b.bottom+8)+'px'};
// ---------- ответ и правка сообщения ----------
const composer=()=>$('.composer[data-chat]');
const showBar=(f,title,text)=>{const bar=f.querySelector('.replybar');if(!bar)return;bar.querySelector('b').textContent=title;bar.querySelector('span').textContent=text;bar.hidden=false;bar.style.animation='none';void bar.offsetWidth;bar.style.animation=''};
const clearBar=f=>{if(!f)return;const i=f.querySelector('[name=reply_to]');if(i)i.value='';const b=f.querySelector('.replybar');if(b)b.hidden=true;if(f.dataset.edit){delete f.dataset.edit;const ta=f.querySelector('textarea');ta.value='';autosize(ta)}};
const startReply=el=>{const [kind,id]=el.dataset.msg.split(':');if(kind==='chp')return;const f=composer();if(!f)return;clearBar(f);
 f.querySelector('[name=reply_to]').value=id;showBar(f,'Ответ: '+(el.dataset.who||''),(el.dataset.text||'Фото').slice(0,90));f.querySelector('textarea').focus()};
const startEdit=el=>{const f=composer();if(!f)return;clearBar(f);f.dataset.edit=el.dataset.msg.replace(':','/');
 showBar(f,'Редактирование',(el.dataset.text||'').slice(0,90));const ta=f.querySelector('textarea');ta.value=el.dataset.text||'';autosize(ta);ta.focus();try{ta.setSelectionRange(ta.value.length,ta.value.length)}catch(e){}};
const autosize=ta=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,140)+'px';const c=ta.closest('.composer');if(c)c.classList.toggle('has',!!ta.value.trim()||!!(c._files&&c._files.length))};
// ---------- меню сообщения как в iOS ----------
let ovl=null,lifted=null,pressT=0,suppress=false,tm=null;
const closeOvl=instant=>{if(!ovl)return;const o=ovl,l=lifted;ovl=null;lifted=null;
 let fin=false;const done=()=>{if(fin)return;fin=true;o.remove();if(l)l.style.visibility=''};
 if(rm||instant===true||!o._c){done();return}
 o.classList.add('out');
 const lr=l&&l.isConnected?l.getBoundingClientRect():null,dy=lr?lr.top-o._top:0;
 if(o._lift)o._lift.cancel();
 o._c.animate([{transform:'translate3d(0,0,0) scale(1.03)'},{transform:'translate3d(0,'+dy+'px,0) scale(1)'}],{duration:230,easing:'cubic-bezier(.2,.8,.2,1)',fill:'forwards'}).onfinish=done;
 setTimeout(done,450)};
const vanish=el=>{if(!el)return;el.dataset.gone='1';if(rm||!el.animate){el.remove();return}
 const h=el.offsetHeight;el.style.overflow='hidden';el.style.pointerEvents='none';
 el.animate([{opacity:1,transform:'scale(1)',height:h+'px'},{opacity:0,transform:'scale(.86)',height:'0px',marginTop:'0px',marginBottom:'0px',paddingTop:'0px',paddingBottom:'0px'}],{duration:260,easing:'cubic-bezier(.4,0,.2,1)'}).onfinish=()=>el.remove()};
const IC={reply:'<path d="M9 14l-5-5 5-5M4 9h10a6 6 0 0 1 6 6v3"/>',copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',edit:'<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/>',del:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'};
const ico=n=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+IC[n]+'</svg>';
const openMenu=(el,touch)=>{
 closeOvl(true);close();el.classList.remove('press');el.style.transition='none';void el.offsetWidth;
 const kind=el.dataset.msg.split(':')[0],id=el.dataset.msg.replace(':','/'),own=el.dataset.own==='1',all=el.dataset.all==='1',text=el.dataset.text||'';
 const rc=el.getBoundingClientRect(),W=innerWidth,H=(vv?vv.height:innerHeight),side=el.classList.contains('me');
 ovl=document.createElement('div');ovl.className='ovl';
 const clone=el.cloneNode(true);clone.removeAttribute('data-msg');clone.classList.remove('new','hl');clone.classList.add('ovl-msg');clone.style.left=rc.left+'px';clone.style.width=rc.width+'px';clone.style.transform='';clone.style.transition='none';clone.style.setProperty('--p',0);el.style.transition='';
 const bar=document.createElement('div');bar.className='rxbar';
 RX.forEach(([k,em])=>{const b=document.createElement('button');b.type='button';b.dataset.e=k;b.innerHTML='<span class="em">'+em+'</span>';b.onclick=ev=>{ev.stopPropagation();closeOvl();act('/react/'+id,'',k)};bar.appendChild(b)});
 const mm=document.createElement('div');mm.className='mmenu';
 const add=(icon,t,fn,cls)=>{const b=document.createElement('button');b.type='button';b.innerHTML=ico(icon)+'<span>'+t+'</span>';if(cls)b.className=cls;b.onclick=ev=>{ev.stopPropagation();closeOvl(cls==='danger');fn()};mm.appendChild(b)};
 if(kind!=='chp'&&composer())add('reply','Ответить',()=>startReply(el));
 if(text)add('copy','Скопировать',()=>{if(navigator.clipboard)navigator.clipboard.writeText(text).then(()=>toast('Скопировано'),()=>{});else toast('Копирование недоступно')});
 if(own&&text&&composer())add('edit','Изменить',()=>startEdit(el));
 if(mm.children.length)mm.appendChild(document.createElement('hr'));
 add('del','Удалить у меня',()=>{vanish(el);act('/msg/'+id+'/delete','','me')},'danger');
 if(all)add('del','Удалить у всех',()=>{if(confirm('Удалить сообщение у всех?')){vanish(el);act('/msg/'+id+'/delete','','all')}},'danger');
 const bgd=document.createElement('div');bgd.className='ovl-bg';
 ovl.append(bgd,clone,bar,mm);document.body.appendChild(ovl);
 const mh=mm.offsetHeight,mw=mm.offsetWidth,bh=bar.offsetHeight,bw=bar.offsetWidth,ch=Math.min(rc.height,H*.42),need=bh+12;
 let top=rc.top;if(top-need<12)top=12+need;if(top+ch+mh+24>H)top=Math.max(12+need,H-ch-mh-24);
 clone.style.top=top+'px';bar.style.top=(top-bh-10)+'px';mm.style.top=(top+ch+12)+'px';
 const cl=(x,w)=>Math.max(12,Math.min(x,W-w-12));
 bar.style.left=cl(side?rc.right-bw:rc.left,bw)+'px';mm.style.left=cl(side?rc.right-mw:rc.left,mw)+'px';
 ovl._c=clone;ovl._top=top;
 if(!rm)ovl._lift=clone.animate([{transform:'translate3d(0,'+(rc.top-top)+'px,0) scale(1)'},{transform:'translate3d(0,0,0) scale(1.03)'}],{duration:340,easing:'cubic-bezier(.34,1.36,.64,1)',fill:'forwards'});
 lifted=el;el.style.visibility='hidden';
 const opened=Date.now();ovl.addEventListener('click',e=>{if((touch&&Date.now()-opened<450)||e.target.closest('.rxbar,.mmenu'))return;closeOvl()});
 if(navigator.vibrate)try{navigator.vibrate(10)}catch(e){}
};
document.addEventListener('contextmenu',e=>{
 const m=e.target.closest('[data-msg]');
 if(m&&!e.target.closest('a,.quote')){e.preventDefault();openMenu(m)}
 else if(m||e.target.closest('[data-nocopy]'))e.preventDefault();
});
document.addEventListener('touchstart',e=>{
 const m=e.target.closest('[data-msg]');if(!m||e.touches.length>1||m.classList.contains('pending'))return;
 const t=e.touches[0];tm={el:m,x:t.clientX,y:t.clientY,mode:'',armed:false};
 clearTimeout(pressT);
 const pt=setTimeout(()=>{if(tm&&tm.el===m&&!tm.mode)m.classList.add('press')},120);
 pressT=setTimeout(()=>{clearTimeout(pt);if(tm&&tm.el===m&&!tm.mode){suppress=true;openMenu(m,true);tm=null}},430);
},{passive:true});
document.addEventListener('touchmove',e=>{
 if(!tm)return;const t=e.touches[0],dx=t.clientX-tm.x,dy=t.clientY-tm.y;
 if(!tm.mode){if(Math.abs(dx)<8&&Math.abs(dy)<8)return;clearTimeout(pressT);tm.el.classList.remove('press');tm.mode=Math.abs(dx)>Math.abs(dy)*1.3?'h':'v'}
 if(tm.mode!=='h')return;
 const el=tm.el;
 if(el.dataset.msg.startsWith('chp')||!composer()||(el.classList.contains('me')?dx>0:dx<0))return;
 const c=Math.min(Math.abs(dx)*.6,72)*(dx<0?-1:1);
 el.style.transition='none';el.style.transform='translate3d('+c+'px,0,0)';el.style.setProperty('--p',Math.min(Math.abs(c)/48,1));
 const armed=Math.abs(c)>=48;if(armed&&!tm.armed&&navigator.vibrate)try{navigator.vibrate(8)}catch(x){}tm.armed=armed;
},{passive:true});
const endTouch=e=>{clearTimeout(pressT);if(suppress){suppress=false;if(e.cancelable)e.preventDefault()}if(!tm)return;const el=tm.el;el.classList.remove('press');
 if(tm.mode==='h'){el.style.transition='';el.style.transform='';el.style.setProperty('--p',0);if(tm.armed)startReply(el)}
 tm=null};
['touchend','touchcancel'].forEach(n=>document.addEventListener(n,endTouch,{passive:false}));

// ---------- фото и видео в канале ----------
const renderTray=f=>{
 const tray=f.querySelector('.tray');if(!tray)return;const files=f._files||[],items=tray.querySelector('.tray-items');
 (f._urls||[]).forEach(u=>URL.revokeObjectURL(u));f._urls=[];items.innerHTML='';
 const imgs=files.filter(x=>!/^video\//.test(x.type)),ba=files.length===2&&imgs.length===2,tog=tray.querySelector('.batog'),cb=tray.querySelector('[data-ba]');
 tog.hidden=!ba;if(!ba)cb.checked=false;
 files.forEach((file,k)=>{const d=document.createElement('div');d.className='titem';const u=URL.createObjectURL(file);f._urls.push(u);
  const isV=/^video\//.test(file.type)||/\.(mp4|mov|webm)$/i.test(file.name);
  const m=document.createElement(isV?'video':'img');m.src=u;if(isV){m.muted=true;m.playsInline=true;m.preload='metadata'}d.appendChild(m);
  if(isV){const l=document.createElement('span');l.className='tl';l.textContent='Видео';d.appendChild(l)}
  else if(ba&&cb.checked){const l=document.createElement('span');l.className='tl'+(k?' after':'');l.textContent=k?'После':'До';d.appendChild(l)}
  const x=document.createElement('button');x.type='button';x.className='tx';x.textContent='×';x.setAttribute('aria-label','Убрать');x.onclick=()=>{f._files.splice(k,1);renderTray(f)};d.appendChild(x);
  items.appendChild(d)});
 tray.hidden=!files.length;autosize(f.querySelector('textarea'));
};
const sendMedia=(f,text)=>new Promise((ok,bad)=>{
 const fd=new FormData(f);fd.delete('media');(f._files||[]).forEach(x=>fd.append('media',x,x.name));fd.set('ba',f.querySelector('[data-ba]').checked?'1':'');
 const tray=f.querySelector('.tray'),up=tray.querySelector('.upbar'),bar=up.querySelector('b');up.hidden=false;bar.style.width='3%';
 const x=new XMLHttpRequest();x.open('POST',f.action);x.setRequestHeader('X-SPA','1');x.setRequestHeader('X-Frag','1');
 x.upload.onprogress=e=>{if(e.lengthComputable)bar.style.width=Math.max(3,e.loaded/e.total*100)+'%'};
 x.onload=()=>{up.hidden=true;if(x.status>=200&&x.status<300)ok(x.responseText);else bad(x.status)};x.onerror=()=>{up.hidden=true;bad(0)};x.send(fd)});
let vio=null;
const setupVids=root=>{
 if(!vio&&'IntersectionObserver' in window)vio=new IntersectionObserver(es=>es.forEach(e=>{const w=e.target,v=w.querySelector('video');if(!v)return;
  if(e.isIntersecting&&e.intersectionRatio>.6&&!w._user&&!rm){v.muted=true;w.classList.remove('sound');v.play().then(()=>w.classList.add('playing'),()=>{})}
  else if(!e.isIntersecting||e.intersectionRatio<.3){if(!v.paused){v.pause();w.classList.remove('playing')}}}),{threshold:[0,.3,.6,1]});
 $$('[data-vid]',root).forEach(w=>{if(w._ok)return;w._ok=1;const v=w.querySelector('video'),b=w.querySelector('.vbar b');
  v.addEventListener('timeupdate',()=>{if(v.duration)b.style.setProperty('--t',v.currentTime/v.duration)});
  v.addEventListener('pause',()=>w.classList.remove('playing'));v.addEventListener('play',()=>w.classList.add('playing'));
  if(vio)vio.observe(w)});
 $$('[data-ba]',root).forEach(el=>{if(el._ok||el.tagName==='INPUT')return;el._ok=1;
  const set=cx=>{const rc=el.getBoundingClientRect();el.style.setProperty('--x',Math.max(0,Math.min(100,(cx-rc.left)/rc.width*100))+'%')};
  let on=false,sx=0,sy=0,lock='';
  el.addEventListener('pointerdown',e=>{on=true;lock='';sx=e.clientX;sy=e.clientY;if(e.pointerType==='mouse'){lock='h';el.classList.add('drag');set(e.clientX)}});
  el.addEventListener('pointermove',e=>{if(!on)return;if(!lock){if(Math.abs(e.clientX-sx)>6)lock='h';else if(Math.abs(e.clientY-sy)>6){lock='v';on=false;return}else return;try{el.setPointerCapture(e.pointerId)}catch(x){}el.classList.add('drag')}set(e.clientX)});
  const end=()=>{on=false;el.classList.remove('drag')};el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end);
  if(!rm&&'IntersectionObserver' in window){const io=new IntersectionObserver(es=>{if(!es[0].isIntersecting)return;io.disconnect();
   const t0=performance.now(),D=1700,pts=[50,74,28,50];const step=t=>{const p=Math.min(1,(t-t0)/D),seg=Math.min(2,Math.floor(p*3)),q=p*3-seg,e2=q<.5?2*q*q:1-Math.pow(-2*q+2,2)/2;
    el.style.setProperty('--x',(pts[seg]+(pts[seg+1]-pts[seg])*e2)+'%');if(p<1&&!on)requestAnimationFrame(step)};requestAnimationFrame(step)},{threshold:.7});io.observe(el)}});
};
const vidClick=e=>{
 const w=e.target.closest('[data-vid]');if(!w)return false;const v=w.querySelector('video');w._user=true;
 if(e.target.closest('.vsnd')){v.muted=!v.muted;w.classList.toggle('sound',!v.muted);return true}
 if(e.detail>1){(v.webkitEnterFullscreen?()=>v.webkitEnterFullscreen():()=>v.requestFullscreen&&v.requestFullscreen())();return true}
 if(v.paused||v.muted){v.muted=false;w.classList.add('sound');v.play().catch(()=>{v.muted=true;w.classList.remove('sound');v.play()})}else v.pause();
 return true};
// ---------- отправка и обновление чата ----------
const keyOf=n=>n.dataset&&n.dataset.msg?n.dataset.msg:'s:'+n.className.replace(/\b(new|settle)\b/g,'').trim()+':'+n.textContent.trim();
const patch=(ob,nb)=>{
 const olds=new Map(),pend=[...ob.children].filter(c=>c.classList.contains('pending')),out=[],fresh=[];
 [...ob.children].forEach(c=>{if(c.dataset.gone||c.classList.contains('pending'))return;const k=keyOf(c);if(!olds.has(k))olds.set(k,c)});
 const anyOld=olds.size>0;
 [...nb.children].forEach(nc=>{
  const k=keyOf(nc),oc=olds.get(k);
  if(oc){olds.delete(k);
   if(nc.dataset.msg&&oc.dataset.v!==nc.dataset.v){const oi=oc.querySelector('img.pic'),ni=nc.querySelector('img.pic');
    if(oi&&ni&&oi.getAttribute('src')===ni.getAttribute('src'))ni.replaceWith(oi);fmtTimes(nc);oc.replaceWith(nc);out.push(nc)}
   else out.push(oc)}
  else{fmtTimes(nc);if(nc.dataset.own==='1'&&pend.length){const p=pend.shift();nc.classList.add('settle');p.replaceWith(nc)}else if(anyOld&&nc.dataset.msg)fresh.push(nc);out.push(nc)}
 });
 olds.forEach(c=>vanish(c));
 let cur=ob.firstElementChild;
 out.forEach(n=>{while(cur&&cur.dataset.gone)cur=cur.nextElementSibling;if(cur===n){cur=cur.nextElementSibling;return}ob.insertBefore(n,cur)});
 pend.forEach(p=>ob.appendChild(p));
 if(!rm)fresh.forEach(x=>{x.classList.add('new');setTimeout(()=>x.classList.remove('new'),520)});
 setTimeout(()=>$$('.settle',ob).forEach(x=>x.classList.remove('settle')),400);
};
const updateChat=(html,mode)=>{
 const d=new DOMParser().parseFromString(html,'text/html');
 $$('.flash',d).forEach(x=>toast(x.textContent.trim()));
 const nb=d.querySelector('[data-bottom]'),ob=$('[data-bottom]');
 if(!nb||!ob)return;
 const ns=d.querySelector('.cpill small'),os=$('.cpill small');if(ns&&os&&os.innerHTML!==ns.innerHTML)os.innerHTML=ns.innerHTML;
 if(mode==='poll'&&nb.dataset.sig===ob.dataset.sig)return;
 const s=sc(),wasBottom=stick||nearBottom(),top=s?s.scrollTop:0;
 patch(ob,nb);ob.dataset.sig=nb.dataset.sig;setupVids(ob);
 if(mode==='bottom'||(mode==='poll'&&wasBottom))toBottom(mode==='poll');else if(s){s.scrollTop=top;stick=nearBottom()}
};
const sendChat=async f=>{
 const ta=$('textarea',f),fi=$('input[type=file]',f),text=ta.value.trim(),file=fi&&fi.files.length;
 const csrf=(f.querySelector('[name=csrf]')||{}).value||'';
 if(f.dataset.edit){
  const id=f.dataset.edit;if(!text)return;
  const fd=new FormData();fd.append('csrf',csrf);fd.append('body',text);
  clearBar(f);ta.value='';autosize(ta);
  try{const res=await fetchPage('/msg/'+id+'/edit',{method:'POST',body:fd},true);updateChat(res.html,'keep')}catch(e){toast('Не удалось изменить')}
  return;
 }
 if(f.hasAttribute('data-media')&&f._files&&f._files.length){
  const sendBtn=f.querySelector('.send');sendBtn.disabled=true;
  try{const html=await sendMedia(f,text);f._files=[];renderTray(f);ta.value='';autosize(ta);updateChat(html,'bottom')}
  catch(e){toast(e===413?'Файл слишком большой':'Не удалось опубликовать')}
  finally{sendBtn.disabled=false}
  return}
 if(!text&&!file)return;
 const box=$('[data-bottom]');
 if(text&&!file&&box){const b=document.createElement('div');b.className='bubble me pending';const s=document.createElement('span');s.className='mt';s.textContent=text;const tt=document.createElement('span');tt.className='bt';tt.textContent=hm(new Date());b.append(s,tt);$$('.sys.empty',box).forEach(x=>x.remove());box.appendChild(b);toBottom(false)}
 else if(file)toast('Отправляю фото…');
 const fd=new FormData(f);ta.value='';autosize(ta);clearBar(f);if(fi)fi.value='';
 try{const res=await fetchPage(f.action||location.href,{method:'POST',body:fd},true);updateChat(res.html,'bottom')}
 catch(e){$$('.pending').forEach(x=>x.remove());ta.value=text;autosize(ta);toast('Не удалось отправить')}
};
let polling=false;
const liveInbox=html=>{const d=new DOMParser().parseFromString(html,'text/html'),nl=d.querySelector('.rcs[data-live]'),ol=$('.rcs[data-live]');if(!nl||!ol)return;
 const raw=nl.innerHTML;if(!liveRaw)liveRaw=raw;if(raw===liveRaw)return;liveRaw=raw;
 const was=new Set($$('.rc',ol).map(x=>x.getAttribute('href')+'|'+x.textContent.replace(/\s+/g,' ')));
 ol.innerHTML=raw;fmtTimes(ol);const on=$('[data-f].on');const k=on?on.dataset.f:'all';$$('.rc',ol).forEach(x=>x.hidden=!(k==='all'||x.dataset.kind===k));
 if(!rm)$$('.rc',ol).forEach(x=>{if(!was.has(x.getAttribute('href')+'|'+x.textContent.replace(/\s+/g,' ')))x.animate([{background:'var(--soft)'},{background:'transparent'}],{duration:1200,easing:'ease-out'})});
 const nf=d.querySelector('.folders'),of=$('.folders');if(nf&&of){$$('[data-f]',nf).forEach(b=>b.classList.toggle('on',b.dataset.f===k));of.innerHTML=nf.innerHTML}};
const poll=async()=>{
 if(document.hidden||ovl||polling)return;
 const chat=$('[data-poll]'),live=$('.rcs[data-live]');if(!chat&&!live)return;
 polling=true;const url=location.href;
 try{const res=await fetchPage(url,null,true);if(url===location.href){if(chat)updateChat(res.html,'poll');else liveInbox(res.html)}}catch(e){}
 polling=false;
};
setInterval(poll,4000);
const setCounts=c=>{lastC=c;
 const a=$$('.tabs4 a')[1];
 if(a){let b=a.querySelector('.tbadge');if(c.dm){const t=c.dm<100?String(c.dm):'99+';if(!b){b=document.createElement('i');b.className='tbadge';a.appendChild(b)}
  if(b.textContent!==t){b.textContent=t;if(!rm)b.animate([{transform:'scale(.4)'},{transform:'scale(1)'}],{duration:380,easing:'cubic-bezier(.34,1.56,.64,1)'})}}else if(b)b.remove()}
 const bell=$('a.bell');if(bell){let b=bell.querySelector('b');if(c.n){if(!b){b=document.createElement('b');bell.appendChild(b)}b.textContent=c.n<100?c.n:'99+'}else if(b)b.remove()}
 document.title=(c.dm?'('+(c.dm<100?c.dm:'99+')+') ':'')+'Thenks'};
const counts=async()=>{if(document.hidden||!$('.tabbar'))return;try{const res=await fetch('/counts',{credentials:'same-origin'});if(res.ok)setCounts(await res.json())}catch(e){}};
setInterval(counts,10000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){poll();counts()}});
document.addEventListener('input',e=>{const ta=e.target;if(ta.matches&&ta.matches('.composer textarea'))autosize(ta)});
// не даём клавиатуре закрыться при нажатии «Отправить»
document.addEventListener('mousedown',e=>{if(e.target.closest('.send'))e.preventDefault()});
document.addEventListener('touchend',e=>{const s=e.target.closest('.send');if(s&&s.form){e.preventDefault();s.form.requestSubmit()}},{passive:false});
document.addEventListener('keydown',e=>{
 if(e.key==='Escape'){if(sheetClose){sheetClose();return}closeAll();const f=composer();if(f)clearBar(f);return}
 const ta=e.target;
 if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&ta.matches&&ta.matches('.composer textarea')&&finePtr()){e.preventDefault();ta.closest('form').requestSubmit()}
});
document.addEventListener('scroll',close,{passive:true,capture:true});
// ---------- просмотр фото ----------
const openLB=img=>{
 const rc=img.getBoundingClientRect(),lb=document.createElement('div');lb.className='lb';const im=new Image();im.src=img.currentSrc||img.src;im.alt='';lb.appendChild(im);document.body.appendChild(lb);
 const fly=()=>{if(rm)return;const t=im.getBoundingClientRect();if(!t.width||!rc.width)return;const s=rc.width/t.width;
  im.animate([{transform:'translate('+(rc.left+rc.width/2-(t.left+t.width/2))+'px,'+(rc.top+rc.height/2-(t.top+t.height/2))+'px) scale('+s+')',opacity:.6},{transform:'none',opacity:1}],{duration:360,easing:'cubic-bezier(.2,.9,.25,1)'})};
 if(im.complete)fly();else im.onload=fly;
 lb.onclick=()=>{if(rm){lb.remove();return}lb.animate([{opacity:1},{opacity:0}],{duration:180,easing:'ease-in'}).onfinish=()=>lb.remove()};
};

// ---------- окно автоудаления с ползунком ----------
let sheetClose=null;
const STEPS=[0,1,7,30,90,180],SL=['Никогда','1 день','1 неделя','1 месяц','3 месяца','6 месяцев'],ST=['Никогда','1 д','1 нед','1 мес','3 мес','6 мес'];
const TIMER='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9 2h6"/></svg>';
const sheet=html=>{
 if(sheetClose)sheetClose(true);
 const bg=document.createElement('div');bg.className='sheet-bg';const sh=document.createElement('div');sh.className='sheet';sh.setAttribute('role','dialog');
 sh.innerHTML='<div class="grab"></div>'+html;document.body.append(bg,sh);
 const shut=now=>{if(sheetClose!==shut)return;sheetClose=null;if(rm||now===true){bg.remove();sh.remove();return}
  sh.style.transition='';bg.classList.add('out');sh.classList.add('out');setTimeout(()=>{bg.remove();sh.remove()},260)};
 sheetClose=shut;bg.onclick=shut;
 let y0=null,dy=0;
 sh.addEventListener('touchstart',e=>{if(e.target.closest('input'))return;y0=e.touches[0].clientY;dy=0;sh.style.transition='none'},{passive:true});
 sh.addEventListener('touchmove',e=>{if(y0===null)return;dy=Math.max(0,e.touches[0].clientY-y0);sh.style.transform='translate3d(0,'+dy+'px,0)'},{passive:true});
 sh.addEventListener('touchend',()=>{if(y0===null)return;y0=null;sh.style.transition='transform .3s cubic-bezier(.2,.85,.25,1)';if(dy>90){sh.style.transform='translate3d(0,100%,0)';bg.classList.add('out');sheetClose=null;setTimeout(()=>{bg.remove();sh.remove()},300)}else sh.style.transform=''});
 return {sh,shut};
};
const openTTL=a=>{
 $$('details.tile[open]').forEach(d=>d.open=false);
 const user=a.dataset.ttl;let i=Math.max(0,STEPS.indexOf(+a.dataset.cur||0));
 const {sh,shut}=sheet('<h3>Автоудаление сообщений</h3><p>Сообщения в этом чате будут удаляться у обоих собеседников через выбранное время.</p><div class="ttlval">'+TIMER+'<span></span></div><input class="range" type="range" min="0" max="5" step="1" aria-label="Срок"><div class="ticks">'+ST.map((t,k)=>'<span data-k="'+k+'" style="--k:'+k+'">'+t+'</span>').join('')+'</div><button type="button" class="main">Готово</button>');
 const rg=$('input',sh),val=$('.ttlval span',sh);
 const upd=anim=>{i=+rg.value;rg.style.setProperty('--pct',(i/5*100)+'%');if(val.textContent!==SL[i]){val.textContent=SL[i];if(anim&&!rm){val.classList.remove('flip');void val.offsetWidth;val.classList.add('flip')}}$$('.ticks span',sh).forEach((s,k)=>s.classList.toggle('on',k===i))};
 rg.value=i;upd(false);
 rg.addEventListener('input',()=>{upd(true);if(navigator.vibrate)try{navigator.vibrate(4)}catch(e){}});
 $$('.ticks span',sh).forEach(s=>s.onclick=()=>{rg.value=s.dataset.k;upd(true)});
 $('.main',sh).onclick=async()=>{const days=STEPS[i];shut();if(days===(+a.dataset.cur||0))return;
  const fd=new FormData($('#actform'));fd.set('days',days);
  try{const res=await fetch('/ttl/'+encodeURIComponent(user),{method:'POST',body:fd,headers:{'X-SPA':'1'},credentials:'same-origin'});const jj=await res.json();
   a.dataset.cur=days;const mv=a.querySelector('.mval');if(mv)mv.textContent=jj.label;pre.clear();toast(days?'Автоудаление: '+jj.label.toLowerCase():'Автоудаление выключено')}
  catch(e){toast('Не удалось сохранить')}};
};
// ---------- обрезка фото профиля ----------
const ZS='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>',ZB='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/></svg>';
const openCrop=input=>{
 const file=input.files&&input.files[0];if(!file)return;
 if(file.type&&!/^image\//.test(file.type)){toast('Выбери картинку');input.value='';return}
 const url=URL.createObjectURL(file),box=document.createElement('div');box.className='crop';
 box.innerHTML='<div class="crop-top"><button type="button" class="no">Отмена</button><b>Фото профиля</b><button type="button" class="ok">Готово</button></div><div class="crop-area"><img alt=""><div class="crop-hole"></div></div><p class="crop-note">Двигай фото пальцем, масштаб меняй ползунком или двумя пальцами</p><div class="crop-bot">'+ZS+'<input class="range" type="range" min="1" max="4" step="0.01" value="1" aria-label="Масштаб">'+ZB+'</div>';
 document.body.appendChild(box);
 const area=$('.crop-area',box),img=$('img',area),hole=$('.crop-hole',box),rg=$('input',box);
 let R=0,z=1,iw=0,ih=0,base=1,x=0,y=0;
 const layout=()=>{const w=area.clientWidth,h=area.clientHeight;R=Math.max(120,Math.min(w,h,460)-40);hole.style.width=hole.style.height=R+'px';hole.style.left=(w-R)/2+'px';hole.style.top=(h-R)/2+'px';base=Math.max(R/iw,R/ih)};
 const clamp=()=>{const w=area.clientWidth,h=area.clientHeight,s=base*z,hx=(w-R)/2,hy=(h-R)/2;x=Math.min(hx,Math.max(hx+R-iw*s,x));y=Math.min(hy,Math.max(hy+R-ih*s,y))};
 const draw=()=>{clamp();img.style.transform='translate3d('+x+'px,'+y+'px,0) scale('+(base*z)+')';rg.style.setProperty('--pct',((z-1)/3*100)+'%')};
 img.onload=()=>{iw=img.naturalWidth;ih=img.naturalHeight;layout();const s=base;x=(area.clientWidth-iw*s)/2;y=(area.clientHeight-ih*s)/2;draw()};
 img.onerror=()=>{toast('Не удалось открыть фото');shut()};img.src=url;
 const zoomAt=(nz,px,py)=>{nz=Math.min(4,Math.max(1,nz));const s0=base*z,s1=base*nz;x=px-(px-x)*s1/s0;y=py-(py-y)*s1/s0;z=nz;rg.value=z;draw()};
 rg.oninput=()=>zoomAt(+rg.value,area.clientWidth/2,area.clientHeight/2);
 const pts=new Map();let pd=0;
 area.addEventListener('pointerdown',e=>{try{area.setPointerCapture(e.pointerId)}catch(x){}pts.set(e.pointerId,{x:e.clientX,y:e.clientY});if(pts.size===2){const [p,q]=[...pts.values()];pd=Math.hypot(p.x-q.x,p.y-q.y)}});
 area.addEventListener('pointermove',e=>{const p=pts.get(e.pointerId);if(!p)return;const dx=e.clientX-p.x,dy=e.clientY-p.y;pts.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pts.size===1){x+=dx;y+=dy;draw()}else if(pts.size===2){const [a1,b1]=[...pts.values()],d=Math.hypot(a1.x-b1.x,a1.y-b1.y),rc=area.getBoundingClientRect();if(pd)zoomAt(z*d/pd,(a1.x+b1.x)/2-rc.left,(a1.y+b1.y)/2-rc.top);pd=d}});
 const up=e=>{pts.delete(e.pointerId);pd=0};area.addEventListener('pointerup',up);area.addEventListener('pointercancel',up);
 area.addEventListener('wheel',e=>{e.preventDefault();const rc=area.getBoundingClientRect();zoomAt(z*(e.deltaY<0?1.08:1/1.08),e.clientX-rc.left,e.clientY-rc.top)},{passive:false});
 const onR=()=>{if(iw){layout();draw()}};addEventListener('resize',onR);
 const shut=()=>{removeEventListener('resize',onR);URL.revokeObjectURL(url);if(rm){box.remove();return}box.animate([{opacity:1},{opacity:0}],{duration:180}).onfinish=()=>box.remove()};
 $('.no',box).onclick=()=>{input.value='';shut()};
 $('.ok',box).onclick=()=>{if(!iw)return;
  const w=area.clientWidth,h=area.clientHeight,s=base*z,hx=(w-R)/2,hy=(h-R)/2,cv=document.createElement('canvas');cv.width=cv.height=640;
  const g2=cv.getContext('2d');g2.imageSmoothingQuality='high';g2.drawImage(img,(hx-x)/s,(hy-y)/s,R/s,R/s,0,0,640,640);
  cv.toBlob(bl=>{if(!bl){toast('Не удалось обработать фото');return}const f=input.form;f._crop={name:input.name,blob:bl};
   const pv=f.querySelector('[data-avprev]');if(pv){const im=document.createElement('img');im.className='pava';im.setAttribute('data-avprev','');im.alt='';im.src=URL.createObjectURL(bl);pv.replaceWith(im);
    if(!rm)im.animate([{transform:'scale(.7)',opacity:.3},{transform:'none',opacity:1}],{duration:420,easing:'cubic-bezier(.34,1.36,.64,1)'})}
   input.value='';shut();toast('Фото готово, нажми «Сохранить»')},'image/jpeg',.9)};
};
// ---------- автосохранение настроек ----------
const autosave=async f=>{
 try{const res=await fetch(f.action||location.href,{method:'POST',body:new FormData(f),headers:{'X-SPA':'1'},credentials:'same-origin'});if(!res.ok)throw 0;pre.clear();toast('Сохранено')}
 catch(e){toast('Не удалось сохранить')}};
// ---------- уведомления на компьютере ----------
let pop=null;
const closePop=()=>{if(pop){pop.remove();pop=null}};
const openNotif=async a=>{
 closePop();
 try{const pg=await fetchPage(a.href),d=new DOMParser().parseFromString(pg.html,'text/html'),m=d.querySelector('main');
  pop=document.createElement('div');pop.className='pop';pop.innerHTML=m?m.innerHTML:'';document.body.appendChild(pop);
  const b=a.querySelector('b');if(b)b.remove();fmtTimes(pop)}
 catch(e){location.href=a.href}
};
const closeAll=()=>{close();closePop();closeOvl(true);if(sheetClose)sheetClose(true);$$('.lb').forEach(x=>x.remove());$$('.cmenu').forEach(x=>x.hidden=true);$$('details.tile[open]').forEach(d=>d.open=false)};
// ---------- клики ----------
document.addEventListener('click',e=>{
 if(suppress){suppress=false;e.preventDefault();e.stopPropagation();return}
 if(vidClick(e)){e.preventDefault();return}
 const vbb=e.target.closest('[data-vb]');if(vbb){e.preventDefault();toast(vbb.dataset.vb);return}
 const ct=e.target.closest('[data-copytext]');if(ct){if(navigator.clipboard)navigator.clipboard.writeText(ct.dataset.copytext).then(()=>toast('Скопировано'),()=>{});return}
 const ptb=e.target.closest('[data-pt]');if(ptb){selectPT(ptb);return}
 const tl=e.target.closest('a[data-ttl]');if(tl){e.preventDefault();openTTL(tl);return}
 const sa=e.target.closest('[data-set-accent]');if(sa){setLook('accent',sa.dataset.setAccent);return}
 const sbg=e.target.closest('[data-set-bg]');if(sbg){setLook('bg',sbg.dataset.setBg);return}
 const zi=e.target.closest('img.pic,img[data-zoom]');
 if(zi&&!zi.closest('.ovl,.pop')){e.preventDefault();openLB(zi);return}
 if(menu&&!e.target.closest('.ctx'))close();
 if(e.target.closest('[data-theme-toggle]')){setTheme(r.dataset.theme==='dark'?'light':'dark');return}
 const bell=e.target.closest('a.bell');
 if(bell&&desk()){e.preventDefault();pop?closePop():openNotif(bell);return}
 if(pop&&!e.target.closest('.pop'))closePop();
 if(pop&&e.target.closest('.pop a'))closePop();
 const more=e.target.closest('.chead a[aria-label="Ещё"]'),cm=more&&more.closest('.chead').querySelector('.cmenu');
 $$('.cmenu').forEach(x=>{if(x!==cm&&!x.contains(e.target))x.hidden=true});
 if(cm&&desk()){e.preventDefault();cm.hidden=!cm.hidden;return}
 const rc=e.target.closest('[data-rx]');
 if(rc){rc.classList.add('bump');const [k,i,em]=rc.dataset.rx.split('/');act('/react/'+k+'/'+i,'',em);return}
 const rp=e.target.closest('[data-react]');
 if(rp){openPicker(rp);return}
 const t=e.target.closest('[data-set-theme]');
 if(t){setTheme(t.dataset.setTheme);return}
 const c=e.target.closest('[data-copy]');
 if(c){const v=c.closest('.list').querySelector('[data-path]').textContent;if(navigator.clipboard)navigator.clipboard.writeText(v);c.textContent='Скопировано';return}
 const sh=e.target.closest('[data-share]');
 if(sh){const url=location.origin+sh.dataset.share;$$('details.tile[open]').forEach(d=>d.open=false);
  if(navigator.share&&!finePtr()){navigator.share({title:sh.dataset.name,url}).catch(()=>{})}else{if(navigator.clipboard)navigator.clipboard.writeText(url);toast('Ссылка скопирована')}return}
 const fl=e.target.closest('[data-f]');
 if(fl){const k=fl.dataset.f;$$('[data-f]').forEach(b=>b.classList.toggle('on',b===fl));
  $$('.rc').forEach(x=>x.hidden=!(k==='all'||x.dataset.kind===k));$$('[data-more]').forEach(x=>x.hidden=!(k==='all'||k==='ch'));
  if(!rm){const m=$('main');m.classList.remove('stagger');void m.offsetWidth;m.classList.add('stagger');setTimeout(()=>m.classList.remove('stagger'),700)}return}
 if(e.target.closest('[data-reply-cancel]')){clearBar(e.target.closest('form'));return}
 const gq=e.target.closest('[data-goto]');
 if(gq){const t2=$('[data-msg="'+gq.dataset.goto+'"]');if(t2){t2.scrollIntoView({block:'center',behavior:rm?'auto':'smooth'});t2.classList.remove('hl');void t2.offsetWidth;t2.classList.add('hl')}return}
 if(!e.target.closest('details.tile'))$$('details.tile[open]').forEach(d=>d.open=false);
 if(e.target.closest('[data-back]')){e.preventDefault();if(depth>0)history.back();else go('/messages',{anim:'back'});return}
 if(e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
 const a=e.target.closest('a[href]');if(!a||a.target||a.hasAttribute('download'))return;
 const u=local(a);if(!u)return;
 e.preventDefault();
 if(u.href===location.href){const s=sc();if(s)toBottom(true);else scrollTo({top:0,behavior:rm?'auto':'smooth'});return}
 const bar=a.closest('.tabbar');let o;
 if(bar){const ti=$$('.tabs4 a',bar).indexOf(a),ci=+bar.dataset.active;o={ti,anim:ci<0?'up':(ti>ci?'tabR':'tabL')}}
 else o={anim:(a.classList.contains('back')||a.matches('.chead>a.rbtn:first-child'))?'back':'fwd'};
 go(u.href,o);
});
document.addEventListener('submit',async e=>{
 const f=e.target,m=f.dataset.confirm;
 if(m&&!confirm(m)){e.preventDefault();return}
 if(f.hasAttribute('data-chat')){e.preventDefault();sendChat(f);return}
 if(f.method.toLowerCase()==='get'&&!f.target){const gu=new URL(f.action,location.href);new FormData(f).forEach((v,k)=>gu.searchParams.set(k,v));if(gu.origin===location.origin){e.preventDefault();go(gu.href,{anim:'fade'});return}}
 if(e.defaultPrevented||f.method.toLowerCase()!=='post'||f.target)return;
 const u=new URL(f.action,location.href);if(u.origin!==location.origin)return;
 e.preventDefault();pre.clear();
 if(f.hasAttribute('data-autosave')){e.preventDefault();autosave(f);return}
 const fd=new FormData(f);if(e.submitter&&e.submitter.name)fd.append(e.submitter.name,e.submitter.value);
 if(f._crop)fd.set(f._crop.name,f._crop.blob,'avatar.jpg');
 const btn=e.submitter||f.querySelector('button');if(btn)btn.disabled=true;
 const t=setTimeout(()=>prog(true),120);
 try{const page=await fetchPage(u.href,{method:'POST',body:fd});const same=page.url===location.href;swap(page,{push:!same,keep:same,anim:same?'none':'fwd'})}
 catch(x){location.reload()}
 finally{clearTimeout(t);prog(false);if(btn&&btn.isConnected)btn.disabled=false}
});
document.addEventListener('change',e=>{
 if(e.target.matches('input[data-crop]')){openCrop(e.target);return}
 if(e.target.matches('input[data-pick]')){const f=e.target.form;f._files=(f._files||[]).concat([...e.target.files]).slice(0,10);e.target.value='';renderTray(f);return}
 if(e.target.matches('input[data-ba]')){renderTray(e.target.form);return}
 const af=e.target.closest&&e.target.closest('form[data-autosave]');if(af){autosave(af);return}
 if(e.target.matches('[data-autosend]')){if(e.target.files.length)e.target.form.requestSubmit();return}
 const m=document.getElementById('ms');if(m&&e.target.name==='kind')m.hidden=e.target.value!=='milestone';
});
addEventListener('popstate',()=>{depth=Math.max(0,depth-1);go(location.href,{push:false,anim:'fade'})});
addEventListener('pageshow',e=>{if(e.persisted){$$('main,.chat').forEach(x=>x.getAnimations&&x.getAnimations().forEach(a=>a.cancel()));prog(false);closeAll()}});
if('scrollRestoration' in history)history.scrollRestoration='manual';
const b0=$('.tabbar');if(b0)pill(+b0.dataset.active);
init(true);
counts();
if(!rm&&!sc()){const m=$('main');if(m){m.classList.add('stagger');setTimeout(()=>m.classList.remove('stagger'),750)}}
})();
</script></body></html>"""

TAB_OF = {"privacy_item": "settings", "feed": "feed", "post_page": "feed", "tag": "feed", "milestones": "feed",
          "inbox": "chats", "chat": "chats", "channel_new": "chats", "channel_view": "chats",
          "create_menu": "chats", "group_new": "chats", "channel_settings": "chats", "channel_invite": "chats",
          "settings": "settings", "settings_look": "settings", "settings_account": "settings",
          "settings_help": "settings", "settings_privacy": "settings", "settings_blocked": "settings",
          "settings_notifications": "settings", "chat_ttl": "profile", "compose": "feed", "channel_info": "chats", "group_info": "chats", "profile": "profile", "edit_profile": "profile"}

RXM = """{% macro rx(it, kind) %}{% if it.rx %}<div class="rxs">{% for e, c, mine in it.rx %}<button type="button" class="rxc{{ ' mine' if mine }}" data-e="{{ e }}" data-rx="{{ kind }}/{{ it.id }}/{{ e }}"><span class="em">{{ RX_EMOJI[e] }}</span>{{ c }}</button>{% endfor %}</div>{% endif %}{% endmacro %}"""

POST_CARD = RXM + """{% macro post_card(p) %}
<article class="post{{ ' is-ms' if p.kind == 'milestone' }}">
 <header class="phead">
  <a class="pa" href="{{ url_for('profile', username=p.username) }}">{% if p.avatar %}<img class="ava" src="{{ url_for('media', name=p.avatar) }}" alt="" loading="lazy">{% else %}<span class="ava" style="background:hsl({{ p.username|hue }} 55% 48%)">{{ p.username[0]|upper }}</span>{% endif %}</a>
  <div class="pwho"><span class="pn"><a class="who" href="{{ url_for('profile', username=p.username) }}">{{ p.display_name or p.username }}</a>{{ p.username|vb }}</span><small><i class="at">@{{ p.username }}</i> · <time data-ut="{{ p.created }}">{{ p.created[11:16] }}</time></small></div>
  {% if p.kind == 'learned' %}<span class="tag">Изучил сегодня</span>{% elif p.kind == 'milestone' %}<span class="tag ms">{{ p.milestone }}</span>{% endif %}
 </header>
 <p>{{ p.body|linkify }}</p>{% if p.image %}<img class="pic" src="{{ url_for('media', name=p.image) }}" alt="" loading="lazy" decoding="async">{% endif %}{{ rx(p, 'post') }}
 <div class="pact">
  <button type="button" class="act" data-react="post/{{ p.id }}" aria-label="Реакция"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0M9 9.5h.01M15 9.5h.01"/></svg></button>
  <a class="act" href="{{ url_for('post_page', post_id=p.id) }}" aria-label="Комментарии"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg><span>{{ p.comments or '' }}</span></a>
  {% if g.user and g.user.id == p.user_id %}<form class="ml" method="post" action="{{ url_for('delete_post', post_id=p.id) }}" data-confirm="Удалить пост?"><button class="act" aria-label="Удалить"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button></form>{% endif %}
 </div>
</article>
{% endmacro %}"""

FEED = POST_CARD + """
{% macro art() %}<svg class="wart" viewBox="0 0 600 260" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
<defs><linearGradient id="wa" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#03050a"/><stop offset="1" stop-color="#0c1d3d"/></linearGradient>
<radialGradient id="wb" cx=".68" cy=".3" r=".6"><stop offset="0" stop-color="#2b8af7" stop-opacity=".55"/><stop offset="1" stop-color="#2b8af7" stop-opacity="0"/></radialGradient></defs>
<rect width="600" height="260" fill="url(#wa)"/><rect width="600" height="260" fill="url(#wb)"/>
<g fill="none" stroke="#fff" stroke-opacity=".07"><circle cx="300" cy="130" r="88"/><circle cx="300" cy="130" r="138"/><circle cx="300" cy="130" r="190"/><circle cx="300" cy="130" r="245"/></g>
<g fill="#fff"><circle cx="60" cy="40" r="1.6" opacity=".6"/><circle cx="540" cy="60" r="1.4" opacity=".5"/><circle cx="470" cy="226" r="1.8" opacity=".5"/><circle cx="140" cy="210" r="1.2" opacity=".6"/><circle cx="380" cy="30" r="1.2" opacity=".5"/></g>
<g class="fl1"><rect x="78" y="74" width="132" height="38" rx="19" fill="#fff" fill-opacity=".1"/><rect x="96" y="89" width="70" height="7" rx="3.5" fill="#fff" fill-opacity=".55"/><rect x="172" y="89" width="22" height="7" rx="3.5" fill="#fff" fill-opacity=".3"/></g>
<g class="fl2"><rect x="392" y="156" width="140" height="40" rx="20" fill="#2b8af7"/><rect x="410" y="172" width="80" height="7" rx="3.5" fill="#fff" fill-opacity=".9"/></g>
<g class="fl3"><rect x="430" y="70" width="64" height="30" rx="15" fill="#fff" fill-opacity=".1"/><text x="462" y="91" text-anchor="middle" font-size="16">🔥</text></g>
<g class="lg"><rect x="250" y="80" width="100" height="100" rx="30" fill="#0b0b0c" stroke="#fff" stroke-opacity=".16"/><path d="M276 111h48M300 111v42" stroke="#fff" stroke-width="10" stroke-linecap="round"/><circle class="dot" cx="325" cy="152" r="7.5" fill="#3d9bff"/></g>
</svg>{% endmacro %}
<h1>Лента</h1>
{% if g.user %}<a class="cprompt" href="{{ url_for('compose') }}">{% if g.user.avatar %}<img class="ava" src="{{ url_for('media', name=g.user.avatar) }}" alt="" loading="lazy">{% else %}<span class="ava" style="background:hsl({{ g.user.username|hue }} 55% 48%)">{{ g.user.username[0]|upper }}</span>{% endif %}<span>Что нового, {{ g.user.display_name or g.user.username }}?</span>{% if st %}<b class="schip" title="Серия «Изучил сегодня»">🔥 {{ st }}</b>{% endif %}<i class="cplus"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></i></a>{% endif %}
<div class="tabs">
 <a class="{{ 'here' if tab == 'community' }}" href="{{ url_for('feed') }}">Сообщество</a>
 {% if g.user %}<a class="{{ 'here' if tab == 'following' }}" href="{{ url_for('feed', tab='following') }}">Подписки</a>{% endif %}
</div>
{% if tab == 'community' %}
{% for n in news %}
<article class="card news">
 <div class="art">{{ art() }}</div>
 <div class="nbody">
  <div class="by"><span class="mk-s"><svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#0b0b0c"/><path d="M10 11h12M16 11v11" stroke="#fff" stroke-width="3" stroke-linecap="round"/><circle cx="22.3" cy="21.3" r="2.3" fill="#3d9bff"/></svg></span><b>Команда Thenks</b><span class="tag">официально</span></div>
  <h3>{{ n.title }}</h3>
  <p>{{ n.body }}</p>
  {% if official %}<a class="btn" href="{{ url_for('channel_view', slug=OFFICIAL_SLUG) }}">Открыть канал Thenks</a>{% endif %}
 </div>
</article>
{% endfor %}
{% else %}
{% for p in posts %}{{ post_card(p) }}
{% else %}<div class="empty"><b>Здесь пока пусто</b><span>Подпишись на людей или нажми «+», чтобы опубликовать своё.</span></div>{% endfor %}
{% endif %}"""

COMPOSE = """
<a class="rbtn back" href="{{ url_for('feed', tab='following') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Новая публикация</h1>
<form class="card" method="post" enctype="multipart/form-data" action="{{ url_for('new_post') }}">
 {% if st %}<p class="streak"><strong>Серия: {{ st }} {{ st|days }}.</strong> {{ 'Сегодня уже отмечено.' if done_today else 'Отметься сегодня, чтобы не потерять серию.' }}</p>{% endif %}
 <textarea name="body" maxlength="280" placeholder="Что нового?" required></textarea>
 <label for="img">Фото (необязательно)</label>
 <input id="img" type="file" name="image" accept="image/*">
 <div class="row">
  <label class="opt"><input type="radio" name="kind" value="post" checked> Публикация</label>
  <label class="opt"><input type="radio" name="kind" value="learned"> Что изучил сегодня</label>
  <label class="opt"><input type="radio" name="kind" value="milestone"> Веха</label>
  <select name="milestone" id="ms" hidden aria-label="Тип вехи">
   {% for k, label in MILESTONES.items() %}<option value="{{ k }}">{{ label }}</option>{% endfor %}
  </select>
 </div>
 <div class="row"><button>Опубликовать</button></div>
</form>"""

PROFILE = POST_CARD + """
<div class="pbar">
 {% if g.user and g.user.id != u.id %}<button type="button" class="rbtn" data-back aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></button>{% else %}<span></span>{% endif %}
 {% if own %}<a class="pillbtn" href="{{ url_for('edit_profile') }}">Изм.</a>{% endif %}
</div>
<div class="hero">
 <div class="avring">{% if ava %}<img class="pava" data-zoom src="{{ url_for('media', name=ava) }}" alt="">{% else %}<span class="pava" style="background:hsl({{ u.username|hue }} 55% 48%)">{{ u.username[0]|upper }}</span>{% endif %}</div>
 <h1>{{ u.display_name or u.username }}{{ u.username|vb }}</h1>
 {% if role %}<div class="role">{{ role }}</div>{% endif %}
 <small class="{{ 'online' if status == 'в сети' }}">{{ status|atmark }}</small>
</div>
{% if g.user and not own %}
<div class="tiles t3">
 <a class="tile" href="{{ url_for('chat', username=u.username) }}"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg><span>Чат</span></a>
 <form method="post" action="{{ url_for('toggle_mute', username=u.username) }}"><button class="tile{{ ' off' if is_muted }}" aria-label="Звук уведомлений">{% if is_muted %}<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.7 21a2 2 0 0 1-3.4 0M17.6 13.6C17.9 11.5 18 8 18 8a6 6 0 0 0-9.3-5M6.3 6.3A6 6 0 0 0 6 8c0 7-3 9-3 9h13M2 2l20 20"/></svg><span>Без звука</span>{% else %}<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg><span>Звук</span>{% endif %}</button></form>
 <details class="tile"><summary><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg><span>Ещё</span></summary>
  <div class="menu">
   <form method="post" action="{{ url_for('follow', username=u.username) }}"><button>{{ 'Отписаться' if is_following else 'Подписаться' }}</button></form>
   <button type="button" data-share="{{ url_for('profile', username=u.username) }}" data-name="{{ u.display_name or u.username }}">Поделиться контактом</button>
   <a href="{{ url_for('chat_ttl', username=u.username) }}" data-ttl="{{ u.username }}" data-cur="{{ ttl }}">Автоудаление сообщений<span class="mval">{{ TTL_LABEL[ttl] }}</span></a>
   <form method="post" action="{{ url_for('toggle_block', username=u.username) }}"><input type="hidden" name="back" value="profile"><button class="danger">{{ 'Разблокировать' if is_blocked else 'Заблокировать' }}</button></form>
  </div>
 </details>
</div>
{% endif %}
{% if chan %}
<div class="sech"><span>Канал</span><span>{{ chan.members }} подписч.</span></div>
<a class="chcard" href="{{ url_for('channel_view', slug=chan.slug) }}"><span class="cava" style="background:hsl({{ chan.title|hue }} 60% 50%)">{{ chan.title[0]|upper }}</span><span class="rc-b"><span class="rc-t"><b>{{ chan.title }}{{ ('#' ~ chan.slug)|vb }}</b>{% if chan.ts %}<time data-ut="{{ chan.ts }}"></time>{% endif %}</span><span class="rc-x">{{ chan.last or chan.descr or ('@' ~ chan.slug) }}</span></span></a>
{% endif %}
<div class="list info">
 <div class="kvrow"><div class="kv"><small>Имя пользователя</small><a class="kv-a" href="{{ url_for('profile', username=u.username) }}"><i class="at">@{{ u.username }}</i></a></div><button type="button" class="cpy" data-copytext="@{{ u.username }}" aria-label="Скопировать"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg></button></div>
 {% if bio %}<div class="kv"><small>О себе</small><span class="kvt">{{ bio }}</span></div>{% endif %}
 {% if u.goal %}<div class="kv"><small>Цель</small><span class="kvt">{{ u.goal }}</span></div>{% endif %}
 <div class="kv"><small>В Thenks с</small><span class="kvt">{{ joined }}</span></div>
</div>
<div class="ptabs" role="tablist"><button type="button" class="on" data-pt="posts">Публикации<i>{{ posts|length }}</i></button><button type="button" data-pt="ms">Вехи<i>{{ miles|length }}</i></button><span class="ul"></span></div>
<div class="ptab" data-ptab="posts">{% for p in posts %}{{ post_card(p) }}{% else %}<div class="empty"><b>Публикаций пока нет</b><span>{{ 'Нажми «+» в ленте, чтобы рассказать, что изучил.' if own else 'Здесь появятся публикации.' }}</span></div>{% endfor %}</div>
<div class="ptab" data-ptab="ms" hidden>{% for p in miles %}{{ post_card(p) }}{% else %}<div class="empty"><b>Вех пока нет</b><span>Первый проект, первое собеседование, оффер: отмечай важные шаги.</span></div>{% endfor %}</div>"""

TTL_PAGE = """
<a class="rbtn back" href="{{ url_for('profile', username=other.username) }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Автоудаление</h1>
<p class="note" style="margin-top:0">Сообщения в чате с <i class="at">@{{ other.username }}</i> будут удаляться у обоих собеседников через выбранное время.</p>
<form method="post">
 <div class="list">{% for d in TTL_STEPS %}<label><span>{{ TTL_LABEL[d] }}</span><input class="rd" type="radio" name="days" value="{{ d }}" {{ 'checked' if d == cur }}></label>{% endfor %}</div>
 <div class="row"><button>Сохранить</button></div>
</form>"""

EDIT_PROFILE = """
<a class="rbtn back" href="{{ url_for('profile', username=u.username) }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Изменить профиль</h1>
<form class="card" method="post" enctype="multipart/form-data">
 <label class="avedit">
  <span class="avwrap">{% if u.avatar %}<img class="pava" data-avprev src="{{ url_for('media', name=u.avatar) }}" alt="">{% else %}<span class="pava" data-avprev style="background:hsl({{ u.username|hue }} 55% 48%)">{{ u.username[0]|upper }}</span>{% endif %}<span class="avcam"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg></span></span>
  <span class="avhint">Выбрать фото</span>
  <input id="avatar" type="file" name="avatar" accept="image/*" data-crop hidden>
 </label>
 <label for="display_name">Имя</label>
 <input id="display_name" type="text" name="display_name" maxlength="40" value="{{ u.display_name }}">
 <label for="bio">О себе</label>
 <textarea id="bio" name="bio" maxlength="160">{{ u.bio }}</textarea>
 <label for="goal">Цель</label>
 <input id="goal" type="text" name="goal" maxlength="80" value="{{ u.goal }}" placeholder="Например: Python и удалёнка">
 <div class="row"><button>Сохранить</button><a class="btn ghost" href="{{ url_for('profile', username=u.username) }}">Отмена</a></div>
</form>"""

SETTINGS_HUB = """
<h1>Настройки</h1>
<a class="mecard" href="{{ url_for('profile', username=g.user.username) }}">{% if g.user.avatar %}<img class="ava" src="{{ url_for('media', name=g.user.avatar) }}" alt="" loading="lazy">{% else %}<span class="ava" style="background:hsl({{ g.user.username|hue }} 55% 48%)">{{ g.user.username[0]|upper }}</span>{% endif %}<span class="rc-b"><b>{{ g.user.display_name or g.user.username }}{{ g.user.username|vb }}</b><small><i class="at">@{{ g.user.username }}</i></small></span><svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></a>
<div class="list ic">
 <a href="{{ url_for('settings_look') }}"><span class="li"><i class="si" style="--c:#2b8af7"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a9 9 0 1 0 0 18c1.2 0 2-.8 2-1.8 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4c0-4.4-4-8-9-8z"/><circle cx="7.5" cy="11.5" r="1"/><circle cx="10.5" cy="7.5" r="1"/><circle cx="15" cy="8" r="1"/></svg></i><span>Оформление</span></span><span class="val"><svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>
 <a href="{{ url_for('settings_notifications') }}"><span class="li"><i class="si" style="--c:#ff453a"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg></i><span>Уведомления</span></span><span class="val"><svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>
 <a href="{{ url_for('settings_privacy') }}"><span class="li"><i class="si" style="--c:#30b158"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg></i><span>Конфиденциальность</span></span><span class="val"><svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>
 <a href="{{ url_for('settings_account') }}"><span class="li"><i class="si" style="--c:#ff9f0a"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2"/></svg></i><span>Аккаунт и пароль</span></span><span class="val"><svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>
</div>
<div class="list ic">
 <a href="{{ url_for('settings_help') }}"><span class="li"><i class="si" style="--c:#7c5cff"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17h.01"/></svg></i><span>Помощь и о Thenks</span></span><span class="val"><svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>
</div>
<h2 class="sec">Скоро</h2>
<div class="list">
 {% for t in ['Язык','Папки с чатами','Данные и память','Ключи доступа','Прокси'] %}
 <div><span>{{ t }}</span><span class="soon">Скоро</span></div>
 {% endfor %}
</div>
<div class="list ic">
 <a href="{{ url_for('settings_blocked') }}"><span class="li"><i class="si" style="--c:#8e8e93"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg></i><span>Чёрный список</span></span><span class="val">{% if nblocked %}<b class="cnt">{{ nblocked }}</b>{% endif %}<svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>
</div>
<form method="post" action="{{ url_for('logout') }}"><button class="ghost wide-btn">Выйти</button></form>"""

SETTINGS_LOOK = """
<a class="rbtn back" href="{{ url_for('settings') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Оформление</h1>
<div class="lookprev">
 <div class="bubble">Привет! Как тебе новый цвет?<span class="bt">12:30</span></div>
 <div class="bubble me">Выглядит отлично <span class="bt">12:31</span></div>
</div>
<h2 class="sec">Тема</h2>
<div class="card"><div class="seg">
 <button type="button" data-set-theme="light">Светлая</button>
 <button type="button" data-set-theme="dark">Тёмная</button>
 <button type="button" data-set-theme="auto">Авто</button>
</div></div>
<h2 class="sec">Цвет</h2>
<div class="card sw-row">{% for k, n, c in ACCENTS %}<button type="button" class="swatch" data-set-accent="{{ k }}" style="--sw:{{ c }}" aria-label="{{ n }}"><i></i><span>{{ n }}</span></button>{% endfor %}</div>
<h2 class="sec">Фон</h2>
<div class="bgs">{% for k, n in BGS %}<button type="button" class="bgopt" data-set-bg="{{ k }}"><span class="bgprev" data-bgp="{{ k }}"></span><span>{{ n }}</span></button>{% endfor %}</div>
<p class="note">Цвет и фон запоминаются на этом устройстве.</p>"""

SETTINGS_ACCOUNT = """
<a class="rbtn back" href="{{ url_for('settings') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Аккаунт</h1>
<form class="card" method="post" action="{{ url_for('change_password') }}">
 <strong>Смена пароля</strong>
 <label for="old">Старый пароль</label>
 <input id="old" type="password" name="old" required autocomplete="current-password">
 <label for="new">Новый пароль (от 6 символов)</label>
 <input id="new" type="password" name="new" required autocomplete="new-password">
 <div class="row"><button>Сменить пароль</button></div>
</form>
<form class="card" method="post" action="{{ url_for('delete_account') }}" data-confirm="Удалить аккаунт навсегда? Это нельзя отменить.">
 <strong>Удаление аккаунта</strong>
 <p class="muted" style="margin:4px 0">Посты, комментарии, лайки, подписки, сообщения и фото будут стёрты.</p>
 <label for="pw">Пароль для подтверждения</label>
 <input id="pw" type="password" name="password" required autocomplete="current-password">
 <div class="row"><button class="ghost">Удалить аккаунт</button></div>
</form>"""

SETTINGS_HELP = """
<a class="rbtn back" href="{{ url_for('settings') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Помощь</h1>
<div class="card"><strong>Что такое Thenks?</strong><p class="muted" style="margin:6px 0 0">Мессенджер и лента для тех, кто учится и растёт в IT: делись тем, что изучил, отмечай вехи, общайся в чатах и каналах.</p></div>
<div class="card"><strong>Как создать канал?</strong><p class="muted" style="margin:6px 0 0">Вкладка «Директ» → «+» → «Новый канал». В канале публикуешь только ты, остальные подписываются.</p></div>
<div class="card"><strong>Что такое серия?</strong><p class="muted" style="margin:6px 0 0">Дни подряд, в которые ты публиковал пост типа «Что изучил сегодня».</p></div>
<div class="card"><strong>Забыл пароль</strong><p class="muted" style="margin:6px 0 0">Пока восстановления нет: почта не подключена.</p></div>"""

SETTINGS_PRIVACY = """
<a class="rbtn back" href="{{ url_for('settings') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Конфиденциальность</h1>
<h2 class="sec">Конфиденциальность</h2>
<div class="list">{% for key, label, val in rows %}<a href="{{ url_for('privacy_item', key=key) }}"><span>{{ label }}</span><span class="val">{{ val }}<svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a>{% endfor %}</div>
<h2 class="sec">Удалить мой аккаунт</h2>
<div class="list"><a href="{{ url_for('privacy_item', key='autodel') }}"><span>Если я не захожу</span><span class="val">{{ autodel }}<svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></a></div>
<p class="note">Если вы ни разу не заглянете в Thenks за это время, аккаунт будет удалён вместе со всеми сообщениями и публикациями.</p>"""

PRIV_ITEM = """
<a class="rbtn back" href="{{ url_for('settings_privacy') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>{{ title }}</h1>
<form method="post" data-autosave>
 <h2 class="sec">{{ question }}</h2>
 <div class="list">{% for v, l in opts %}<label><span>{{ l }}</span><input class="rd" type="radio" name="v" value="{{ v }}" {{ 'checked' if v|string == cur|string }}></label>{% endfor %}</div>
 {% if note %}<p class="note">{{ note }}</p>{% endif %}
 <noscript><div class="row"><button>Сохранить</button></div></noscript>
</form>"""

SETTINGS_BLOCKED = """
<a class="rbtn back" href="{{ url_for('settings') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Чёрный список</h1>
<p class="muted" style="margin:0 0 12px">Заблокированные не видны в твоей ленте и не могут писать, комментировать и подписываться на тебя.</p>
{% for b in blocked %}
<div class="card rowh"><a href="{{ url_for('profile', username=b.username) }}"><strong><i class="at">@{{ b.username }}</i></strong></a>
 <form method="post" action="{{ url_for('toggle_block', username=b.username) }}"><button class="ghost">Разблокировать</button></form></div>
{% else %}<p class="muted" style="margin:0 4px">Чёрный список пуст. Заблокировать можно на странице профиля человека.</p>{% endfor %}"""

NOTIFS = """
<div class="rowh"><h1>Уведомления</h1><a class="muted" style="margin:0" href="{{ url_for('settings_notifications') }}">Настроить</a></div>
{% for n in items %}
<a class="card" style="display:block;text-decoration:none{% if not n.seen %};border-color:var(--accent){% endif %}" href="{{ url_for('chat', username=n.username) if n.kind == 'message' else (url_for('profile', username=n.username) if n.kind == 'follow' else url_for('post_page', post_id=n.post_id)) }}">
 <strong><i class="at">@{{ n.username }}</i></strong> {{ texts[n.kind] }}
 {% if n.snippet %}<br><span class="muted" style="margin:0">«{{ n.snippet }}»</span>{% endif %}
 <br><span class="muted" style="margin:0;font-size:13px" data-ut="{{ n.created }}">{{ n.created[:16] }}</span>
</a>
{% else %}<p class="muted" style="margin:0 4px">Пока тихо. Здесь появятся лайки, комментарии, подписки и сообщения.</p>{% endfor %}"""

SETTINGS_NOTIFS = """
<a class="rbtn back" href="{{ url_for('settings') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Уведомления</h1>
<form method="post" data-autosave>
 <h2 class="sec">Уведомления в колокольчике</h2>
 <div class="list">{% for k, (t, d) in labels.items() %}<label><span class="lt"><b>{{ t }}</b><small>{{ d }}</small></span><input class="sw" type="checkbox" name="{{ k }}" {{ 'checked' if k not in g.user.notif_off.split(',') }}></label>{% endfor %}</div>
 <p class="note">О новых сообщениях уведомлений нет: их видно по счётчику на вкладке «Директ».</p>
 <noscript><div class="row"><button>Сохранить</button></div></noscript>
</form>"""

CHEAD = RXM + """{% macro chead(back, title, sub, hue, more='', avatar='', link='', vbu='') %}
<header class="chead">
 <a class="rbtn" href="{{ back }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
 <{{ 'a' if link else 'div' }} class="cpill"{% if link %} href="{{ link }}"{% endif %}>{% if avatar %}<img class="cava" src="{{ avatar }}" alt="">{% else %}<span class="cava" style="background:hsl({{ hue }} 60% 48%)">{{ title[0]|upper }}</span>{% endif %}<div style="min-width:0"><b>{{ title }}{{ vbu|vb }}</b><small>{{ sub|atmark }}</small></div></{{ 'a' if link else 'div' }}>
 {% if more %}<a class="rbtn" href="{{ more }}" aria-label="Ещё"><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg></a>{% endif %}
 {% if caller is defined %}<div class="menu cmenu" hidden>{{ caller() }}</div>{% endif %}
</header>
{% endmacro %}"""

INBOX = """
{% macro row(it) %}
<a class="rc{{ ' unread' if it.unread and not it.muted }}" data-kind="{{ it.kind }}" href="{{ it.href }}">
 {% if it.img %}<img class="rc-ava" src="{{ url_for('media', name=it.img) }}" alt="">{% else %}<span class="rc-ava" style="background:hsl({{ it.hue }} 58% 40%)">{{ it.title[0]|upper }}</span>{% endif %}
 <span class="rc-b">
  <span class="rc-t"><b>{{ it.title }}{{ it.vbu|vb }}</b>{% if it.ts %}<time data-ut="{{ it.ts }}">{{ it.ts[11:16] }}</time>{% endif %}</span>
  <span class="rc-s"><span class="rc-x">{{ it.sub|atmark }}</span>{% if it.unread %}<i class="badge{{ ' mute' if it.muted }}">{{ it.unread if it.unread < 100 else '99+' }}</i>{% endif %}</span>
 </span>
</a>{% endmacro %}
<h1>Директ</h1>
<div class="folders">
 <button type="button" data-f="all" class="on">Все</button>
 {% for k, label in [('dm','Личные'), ('grp','Группы'), ('ch','Каналы')] %}
 {% set n = items|selectattr('kind', 'equalto', k)|sum(attribute='unread') %}
 <button type="button" data-f="{{ k }}">{{ label }}{% if n %}<i>{{ n }}</i>{% endif %}</button>
 {% endfor %}
</div>
<div class="rcs" data-live>{% for it in items %}{{ row(it) }}{% else %}<p class="note" style="padding:16px;margin:0">Пока пусто. Открой профиль человека и нажми «Чат».</p>{% endfor %}</div>
{% if more %}<h2 class="sec" data-more>Каналы для вас</h2>
<div class="rcs" data-more>{% for it in more %}{{ row(it) }}{% endfor %}</div>{% endif %}"""

CREATE_MENU = """
<a class="rbtn back" href="{{ url_for('inbox') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Создать</h1>
<div class="list">
 <a href="{{ url_for('group_new') }}"><span>Новая группа<br><span class="soon">Чат для нескольких людей</span></span><span class="soon">&rsaquo;</span></a>
 <a href="{{ url_for('channel_new') }}"><span>Новый канал<br><span class="soon">Публикуешь ты, остальные читают</span></span><span class="soon">&rsaquo;</span></a>
</div>"""

CHANNEL_NEW = """
<a class="rbtn back" href="{{ url_for('create_menu') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Новый канал</h1>
{% if err %}<div class="flash err">{{ err }}</div>{% endif %}
<form method="post">
 <div class="card">
  <label for="title" style="margin-top:0">Название</label>
  <input id="title" type="text" name="title" maxlength="50" required value="{{ f.title }}" autocomplete="off">
  <label for="slug">Адрес канала</label>
  <div class="slugin"><span>@</span><input id="slug" type="text" name="slug" maxlength="24" value="{{ f.slug }}" placeholder="придумаем из названия" autocapitalize="none" autocorrect="off" spellcheck="false" autocomplete="off"></div>
  <p class="hint">Латиница, цифры и _. Можно оставить пустым: адрес получится из названия.</p>
  <label for="descr">Описание</label>
  <textarea id="descr" name="descr" maxlength="200">{{ f.descr }}</textarea>
 </div>
 <h2 class="sec">Тип канала</h2>
 <div class="list">
  <label><span>Публичный</span><input class="rd" type="radio" name="private" value="0" {{ 'checked' if f.private != '1' }}></label>
  <label><span>Частный</span><input class="rd" type="radio" name="private" value="1" {{ 'checked' if f.private == '1' }}></label>
 </div>
 <p class="note">На частные каналы можно подписаться только по ссылке-приглашению.</p>
 <div class="list"><label><span>Запретить копирование</span><input class="sw" type="checkbox" name="nocopy" {{ 'checked' if f.nocopy }}></label></div>
 <p class="note">Подписчики не смогут выделять и копировать текст публикаций.</p>
 <div class="row"><button>Создать канал</button></div>
</form>"""

CHANNEL = CHEAD + """
<section class="chat">
{% call chead(url_for('inbox'), ch.title, members ~ ' подписч.' ~ (' · частный' if ch.private else ''), ch.title|hue, url_for('channel_settings', slug=ch.slug) if owner else '', '', url_for('channel_info', slug=ch.slug), '#' ~ ch.slug) %}<a href="{{ url_for('channel_info', slug=ch.slug) }}">Информация о канале</a>{% if owner %}<a href="{{ url_for('channel_settings', slug=ch.slug) }}">Настройки канала</a>{% endif %}{% endcall %}
<div class="cscroll"><div data-bottom data-poll data-sig="{{ sig }}" {{ 'data-nocopy' if ch.nocopy and not owner }}>
 <div class="sys">Канал создан · {{ ch.created[:10] }}</div>
 {% if ch.descr %}<div class="sys">{{ ch.descr }}</div>{% endif %}
 {% for p in posts %}
 {% if loop.changed(p.created[:10]) %}<div class="sys">{{ 'Сегодня' if p.created[:10] == today else p.created[:10] }}</div>{% endif %}
 <div class="cmsg{{ ' mpost' if media.get(p.id) }}" data-v="{{ p.v }}" data-msg="chp:{{ p.id }}" data-own="{{ 1 if owner else 0 }}" data-all="{{ 1 if owner else 0 }}" data-text="{{ p.body }}">{% set md = media.get(p.id, []) %}{% if p.layout == 'ba' and md|length == 2 %}<div class="ba" data-ba><img class="ba-a" src="{{ url_for('media', name=md[1].name) }}" alt="После" draggable="false"><div class="ba-b"><img src="{{ url_for('media', name=md[0].name) }}" alt="До" draggable="false"></div><span class="ba-l">До</span><span class="ba-r">После</span><i class="ba-h"><b></b></i></div>{% elif md %}<div class="gal g{{ md|length if md|length < 4 else 4 }}">{% for m in md %}{% if m.kind == 'video' %}<div class="vid" data-vid><video src="{{ url_for('media', name=m.name) }}#t=0.1" preload="metadata" playsinline muted loop></video><button type="button" class="vplay" aria-label="Смотреть"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg></button><button type="button" class="vsnd" aria-label="Звук"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5L6 9H3v6h3l5 4z"/><path class="on" d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/><path class="off" d="M16 9l6 6M22 9l-6 6"/></svg></button><i class="vbar"><b></b></i></div>{% else %}<img class="pic" src="{{ url_for('media', name=m.name) }}" alt="" loading="lazy" decoding="async">{% endif %}{% endfor %}</div>{% endif %}{% if p.image %}<img class="pic" src="{{ url_for('media', name=p.image) }}" alt="" loading="lazy" decoding="async">{% endif %}{% if p.body %}<p>{{ p.body|chpost }}</p>{% endif %}<time>{% if p.edited %}<i class="ed">изм.</i>{% endif %}<span data-utc="{{ p.created }}">{{ p.created[11:16] }}</span></time>{{ rx(p, 'chp') }}</div>
 {% endfor %}
</div></div>
{% if owner %}
<form class="composer" method="post" enctype="multipart/form-data" action="{{ url_for('channel_post', slug=ch.slug) }}" data-chat data-media>
 <input type="hidden" name="reply_to" value="">
 <input type="hidden" name="ba" value="">
 <div class="replybar" hidden><div><b></b><span></span></div><button type="button" class="x" data-reply-cancel aria-label="Отменить">&times;</button></div>
 <div class="tray" hidden><div class="tray-items"></div><label class="batog" hidden><input type="checkbox" class="sw" data-ba> <span>Показать как «До / После»</span></label><div class="upbar" hidden><b></b></div></div>
 <div class="cbox">
  <label class="cbtn" aria-label="Фото и видео"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7-7l9-9a3.3 3.3 0 0 1 4.7 4.7l-9 9a1.7 1.7 0 0 1-2.4-2.4l8-8"/></svg><input type="file" name="media" accept="image/*,video/mp4,video/quicktime,video/webm" multiple data-pick hidden></label>
  <textarea name="body" rows="1" maxlength="2000" placeholder="Публикация…" autocomplete="off" autocapitalize="sentences"></textarea>
  <button class="send" aria-label="Опубликовать"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
 </div>
</form>
{% elif g.user %}
<form class="composer" method="post" action="{{ url_for('channel_join', slug=ch.slug) }}">
 <button class="wide">{{ 'Отписаться' if joined else 'Подписаться' }}</button>
</form>
{% else %}<div class="composer"><a class="wide" href="{{ url_for('login') }}">Войти, чтобы подписаться</a></div>{% endif %}
</section>"""

CHANNEL_SETTINGS = """
<a class="rbtn back" href="{{ url_for('channel_view', slug=ch.slug) }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Канал</h1>
<form method="post">
 <h2 class="sec">Тип канала</h2>
 <div class="list">
  <label><span>Публичный</span><input class="rd" type="radio" name="private" value="0" {{ 'checked' if not ch.private }}></label>
  <label><span>Частный</span><input class="rd" type="radio" name="private" value="1" {{ 'checked' if ch.private }}></label>
 </div>
 <p class="note">На частные каналы можно подписаться только по ссылке-приглашению.</p>
 <h2 class="sec">Постоянная ссылка</h2>
 <div class="list"><div><span class="mono" data-path="/join/{{ ch.invite }}"></span><button type="button" class="ghost" data-copy>Копировать</button></div></div>
 <p class="note">По этой ссылке можно подписаться на канал. Вы можете сбросить её в любой момент.</p>
 <div class="list"><label><span>Запретить копирование</span><input class="sw" type="checkbox" name="nocopy" {{ 'checked' if ch.nocopy }}></label></div>
 <p class="note">Подписчики не смогут выделять и копировать текст. Это защита от случайного копирования, не от скриншотов.</p>
 <div class="row"><button>Готово</button></div>
</form>
<form method="post" action="{{ url_for('channel_reset', slug=ch.slug) }}" data-confirm="Старая ссылка перестанет работать. Сбросить?">
 <div class="row"><button class="ghost">Сбросить ссылку</button></div>
</form>"""

CHANNEL_JOIN = """
<h1>Приглашение</h1>
<form class="card" method="post">
 <strong>{{ ch.title }}</strong>
 <p class="muted" style="margin:4px 0 12px">{{ ch.descr or 'Закрытый канал' }}</p>
 <button>Подписаться на канал</button>
</form>"""

GROUP_NEW = """
<a class="rbtn back" href="{{ url_for('create_menu') }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<h1>Новая группа</h1>
<form class="card" method="post">
 <label for="title" style="margin-top:0">Название</label>
 <input id="title" type="text" name="title" maxlength="50" required>
 <label for="members">Участники (юзернеймы через запятую)</label>
 <input id="members" type="text" name="members" placeholder="alice, bob" required>
 <div class="row"><button>Создать группу</button></div>
</form>"""

GROUP = CHEAD + """
{% set rk = 'grp' %}
<section class="chat">
{% call chead(url_for('inbox'), grp.title, n ~ ' участн.', grp.title|hue, url_for('group_info', gid=grp.id), '', url_for('group_info', gid=grp.id)) %}<a href="{{ url_for('group_info', gid=grp.id) }}">Информация о группе</a><form method="post" action="{{ url_for('group_leave', gid=grp.id) }}" data-confirm="Выйти из группы?"><button class="danger">Выйти из группы</button></form>{% endcall %}
<div class="cscroll"><div data-bottom data-poll data-sig="{{ sig }}">
 <div class="sys">Группа создана · {{ grp.created[:10] }}</div>
 {% for m in msgs %}
 {% set mine = m.sender_id == g.user.id %}
 {% if loop.changed(m.created[:10]) %}<div class="sys">{{ 'Сегодня' if m.created[:10] == today else m.created[:10] }}</div>{% endif %}
 <div class="bubble{{ ' me' if mine }}{{ ' media' if m.image and not m.body and not m.reply_to and mine }}" data-v="{{ m.v }}" data-msg="grp:{{ m.id }}" data-own="{{ 1 if mine else 0 }}" data-all="{{ 1 if (mine or grp.owner_id == g.user.id) else 0 }}" data-text="{{ m.body }}" data-who="{{ 'Вы' if mine else m.username }}">
  {% if not mine %}<a class="who2" href="{{ url_for('profile', username=m.username) }}"><i class="at">@{{ m.username }}</i>{{ m.username|vb }}</a>{% endif %}
  {% if m.reply_to %}<div class="quote" {% if m.rbody is not none %}data-goto="grp:{{ m.reply_to }}"{% endif %}><b>{{ m.rname or '' }}</b><span>{{ ((m.rbody or 'Фото') if m.rbody is not none else 'Сообщение удалено') }}</span></div>{% endif %}
  {% if m.image %}<img class="pic" src="{{ url_for('media', name=m.image) }}" alt="" loading="lazy" decoding="async">{% endif %}
  {% if m.body %}<span class="mt">{{ m.body|linkify }}</span>{% endif %}
  <span class="bt">{% if m.edited %}<i class="ed">изм.</i>{% endif %}<time data-utc="{{ m.created }}">{{ m.created[11:16] }}</time></span>{{ rx(m, rk) }}</div>
 {% endfor %}
</div></div>
<form class="composer" method="post" enctype="multipart/form-data" data-chat>
 <input type="hidden" name="reply_to" value="">
 <div class="replybar" hidden><div><b></b><span></span></div><button type="button" class="x" data-reply-cancel aria-label="Отменить ответ">&times;</button></div>
 <div class="cbox">
  <label class="cbtn" aria-label="Фото"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7-7l9-9a3.3 3.3 0 0 1 4.7 4.7l-9 9a1.7 1.7 0 0 1-2.4-2.4l8-8"/></svg><input type="file" name="image" accept="image/*" data-autosend hidden></label>
  <textarea name="body" rows="1" maxlength="1000" placeholder="Сообщение…" autocomplete="off" autocapitalize="sentences"></textarea>
  <button class="send" aria-label="Отправить"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
 </div>
</form>
</section>"""


GROUP_INFO = """
<a class="rbtn back" href="{{ url_for('group_view', gid=grp.id) }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<div class="hero"><span class="cava" style="background:hsl({{ grp.title|hue }} 60% 48%)">{{ grp.title[0]|upper }}</span><h1>{{ grp.title }}</h1><small>{{ members|length }} участн.</small></div>
<div class="tiles"{% if grp.owner_id != g.user.id %} style="grid-template-columns:1fr"{% endif %}>
 {% if grp.owner_id == g.user.id %}<a class="tile" href="#add"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="10" cy="8" r="3.5"/><path d="M3 20c0-3.5 3-6 7-6M18 14v6M15 17h6"/></svg><span>Добавить</span></a>{% endif %}
 <details class="tile"><summary><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" stroke="currentColor" stroke-width="0" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg><span>Ещё</span></summary>
  <div class="menu"><form method="post" action="{{ url_for('group_leave', gid=grp.id) }}" data-confirm="Выйти из группы?"><button class="danger">Выйти из группы</button></form></div>
 </details>
</div>
<h2 class="sec">Участники</h2>
<div class="list">
{% for m in members %}
 <a href="{{ url_for('profile', username=m.username) }}"><span style="display:flex;align-items:center">
  {% if m.avatar %}<img class="ava" src="{{ url_for('media', name=m.avatar) }}" alt="" style="width:40px;height:40px">{% else %}<span class="ava" style="width:40px;height:40px;background:hsl({{ m.username|hue }} 55% 42%)">{{ m.username[0]|upper }}</span>{% endif %}
  <span><b>{{ m.display_name or m.username }}</b>{{ m.username|vb }}<br><span class="soon">{{ m.status }}</span></span></span>
  {% if m.is_owner %}<span class="tag">владелец</span>{% endif %}</a>
{% endfor %}
</div>
{% if grp.owner_id == g.user.id %}
<form id="add" class="card" method="post" action="{{ url_for('group_add', gid=grp.id) }}">
 <label for="un" style="margin-top:0">Добавить участника</label>
 <input id="un" type="text" name="username" placeholder="юзернейм" required autocomplete="off">
 <div class="row"><button>Добавить</button></div>
</form>
{% endif %}"""

CHANNEL_INFO = """
<a class="rbtn back" href="{{ url_for('channel_view', slug=ch.slug) }}" aria-label="Назад"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></a>
<div class="hero"><span class="cava" style="background:hsl({{ ch.title|hue }} 60% 48%)">{{ ch.title[0]|upper }}</span><h1>{{ ch.title }}</h1><small>{{ members }} подписч.{{ ' · частный' if ch.private }}</small></div>
<div class="tiles">
 <a class="tile" href="{{ url_for('channel_view', slug=ch.slug) }}"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1zM15 9a4 4 0 0 1 0 6"/></svg><span>Перейти</span></a>
 <details class="tile"><summary><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" stroke="currentColor" stroke-width="0" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg><span>Ещё</span></summary>
  <div class="menu">
   {% if owner %}<a href="{{ url_for('channel_settings', slug=ch.slug) }}">Настройки канала</a>
   {% elif joined %}<form method="post" action="{{ url_for('channel_join', slug=ch.slug) }}"><button class="danger">Отписаться</button></form>
   {% else %}<form method="post" action="{{ url_for('channel_join', slug=ch.slug) }}"><button>Подписаться</button></form>{% endif %}
  </div>
 </details>
</div>
<div class="list">
 <div class="kv"><small>Адрес</small><span><i class="at">@{{ ch.slug }}</i></span></div>
 {% if ch.descr %}<div class="kv"><small>Описание</small><span style="color:var(--ink)">{{ ch.descr }}</span></div>{% endif %}
 {% if owner and ch.invite %}<div><span class="mono" data-path="/join/{{ ch.invite }}"></span><button type="button" class="ghost" data-copy>Копировать</button></div>{% endif %}
</div>
<div class="list">
 <div><span>Подписчики</span><span class="soon">{{ members }}</span></div>
 {% if owner %}<a href="{{ url_for('channel_settings', slug=ch.slug) }}"><span>Настройки канала</span><span class="soon">&rsaquo;</span></a>{% endif %}
</div>"""

AUTH = """
<form class="card" method="post">
 <h1>{{ title }}</h1>
 <label for="username">Имя пользователя</label>
 <input id="username" type="text" name="username" required autocomplete="username">
 <label for="password">Пароль</label>
 <input id="password" type="password" name="password" required autocomplete="{{ 'new-password' if reg else 'current-password' }}">
 <div class="row"><button>{{ title }}</button></div>
</form>"""

CHAT = CHEAD + """
{% set rk = 'dm' %}
<section class="chat">
{{ chead(url_for('inbox'), other.display_name or other.username, status, other.username|hue, '', url_for('media', name=ava) if ava else '', url_for('profile', username=other.username), other.username) }}
<div class="cscroll"><div data-bottom data-poll data-sig="{{ sig }}">
 {% if ttl %}<div class="sys">Автоудаление сообщений: {{ TTL_LABEL[ttl]|lower }}</div>{% endif %}
 {% for m in msgs %}
 {% set mine = m.sender_id == g.user.id %}
 {% if loop.changed(m.created[:10]) %}<div class="sys">{{ 'Сегодня' if m.created[:10] == today else m.created[:10] }}</div>{% endif %}
 {% if m.kind == 'ttl' %}{% set d = m.body|int %}<div class="sys tsys"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9 2h6"/></svg> {{ 'Вы' if mine else (other.display_name or other.username) }} {% if d %}{{ 'включили' if mine else 'включил(а)' }} автоудаление: {{ TTL_LABEL[d]|lower }}{% else %}{{ 'выключили' if mine else 'выключил(а)' }} автоудаление{% endif %}</div>{% else %}
 <div class="bubble{{ ' me' if mine }}{{ ' media' if m.image and not m.body and not m.reply_to }}" data-v="{{ m.v }}" data-msg="dm:{{ m.id }}" data-own="{{ 1 if mine else 0 }}" data-all="{{ 1 if mine else 0 }}" data-text="{{ m.body }}" data-who="{{ 'Вы' if mine else (other.display_name or other.username) }}">
  {% if m.reply_to %}<div class="quote" {% if m.rbody is not none %}data-goto="dm:{{ m.reply_to }}"{% endif %}><b>{{ 'Вы' if m.rsender == g.user.id else (other.display_name or other.username) }}</b><span>{{ ((m.rbody or 'Фото') if m.rbody is not none else 'Сообщение удалено') }}</span></div>{% endif %}
  {% if m.image %}<img class="pic" src="{{ url_for('media', name=m.image) }}" alt="" loading="lazy" decoding="async">{% endif %}
  {% if m.body %}<span class="mt">{{ m.body|linkify }}</span>{% endif %}
  <span class="bt">{% if m.edited %}<i class="ed">изм.</i>{% endif %}<time data-utc="{{ m.created }}">{{ m.created[11:16] }}</time></span>{{ rx(m, rk) }}</div>{% endif %}
 {% else %}<div class="sys empty">Напиши первое сообщение</div>{% endfor %}
</div></div>
<form class="composer" method="post" enctype="multipart/form-data" data-chat>
 <input type="hidden" name="reply_to" value="">
 <div class="replybar" hidden><div><b></b><span></span></div><button type="button" class="x" data-reply-cancel aria-label="Отменить ответ">&times;</button></div>
 <div class="cbox">
  <label class="cbtn" aria-label="Фото"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7-7l9-9a3.3 3.3 0 0 1 4.7 4.7l-9 9a1.7 1.7 0 0 1-2.4-2.4l8-8"/></svg><input type="file" name="image" accept="image/*" data-autosend hidden></label>
  <textarea name="body" rows="1" maxlength="1000" placeholder="Сообщение…" autocomplete="off" autocapitalize="sentences"></textarea>
  <button class="send" aria-label="Отправить"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
 </div>
</form>
</section>"""

POST_PAGE = POST_CARD + """
{{ post_card(p) }}
{% for c in comments %}
<div class="card" style="margin-left:24px">
 <a href="{{ url_for('profile', username=c.username) }}"><strong><i class="at">@{{ c.username }}</i></strong></a>{{ c.username|vb }}
 <span class="muted" data-ut="{{ c.created }}">{{ c.created[:16] }}</span>
 {% if g.user and (g.user.id == c.user_id or g.user.id == p.user_id) %}
 <form method="post" action="{{ url_for('delete_comment', comment_id=c.id) }}" data-confirm="Удалить комментарий?" style="display:inline"><button class="like">Удалить</button></form>
 {% endif %}
 <p style="margin:4px 0 0;white-space:pre-wrap;overflow-wrap:anywhere">{{ c.body|linkify }}</p>
</div>
{% else %}<p class="muted">Комментариев пока нет.</p>{% endfor %}
{% if g.user %}
<form class="card" method="post" action="{{ url_for('add_comment', post_id=p.id) }}">
 <textarea name="body" maxlength="500" placeholder="Ответить" required></textarea>
 <div class="row"><button>Отправить</button></div>
</form>
{% endif %}"""

TAG = POST_CARD + """
<h1>#{{ name }}</h1>
{% for p in posts %}{{ post_card(p) }}
{% else %}<p class="muted">Постов с этим тегом пока нет.</p>{% endfor %}"""


MS_PAGE = POST_CARD + """
<h1>Вехи</h1>
<p class="muted" style="margin:0 0 12px">Кто что уже прошёл на пути в IT</p>
{% for p in posts %}{{ post_card(p) }}
{% else %}<p class="muted">Вех пока нет. Отметь свою первую в ленте: выбери «Веха» под полем поста.</p>{% endfor %}"""

app.jinja_env.globals["MILESTONES"] = MILESTONES
TTL_LABEL = {0: "Никогда", 1: "1 день", 7: "1 неделя", 30: "1 месяц", 90: "3 месяца", 180: "6 месяцев"}
TTL_STEPS = list(TTL_LABEL)
ACCENTS = [("sky", "Голубой", "#2b8af7"), ("blue", "Синий", "#3a5bff"), ("violet", "Фиолетовый", "#7c5cff"),
           ("mint", "Мятный", "#12b886"), ("orange", "Оранжевый", "#ff7a1a"), ("pink", "Розовый", "#f2508f"),
           ("graphite", "Графит", "#48484a")]
BGS = [("pattern", "Узор"), ("dots", "Точки"), ("gradient", "Градиент"), ("plain", "Однотонный")]
app.jinja_env.globals.update(TTL_LABEL=TTL_LABEL, TTL_STEPS=TTL_STEPS, ACCENTS=ACCENTS, BGS=BGS, OFFICIAL_SLUG=OFFICIAL_SLUG)


def page(tpl, **ctx):
    add_reactions(ctx)
    inner = Markup(render_template_string(tpl, **ctx))
    if request.headers.get("X-Frag"):  # только содержимое, без каркаса: быстрее для опроса чата
        return "".join('<div class="flash">%s</div>' % escape(m) for m in get_flashed_messages()) + str(inner)
    html = render_template_string(BASE, content=inner, nonce=g.nonce, tab=TAB_OF.get(request.endpoint, ""),
                                  full=ctx.get("full"), gear=ctx.get("gear"), plus=ctx.get("plus"), bare=ctx.get("bare"),
                                  side=side_data() if g.user else None)
    token = session.setdefault("csrf", secrets.token_hex(16))
    return re.sub(r'(<form[^>]*method="post"[^>]*>)',
                  r'\1<input type="hidden" name="csrf" value="%s">' % token, html)


POSTS_SQL = """
SELECT p.id, p.body, p.created, p.user_id, p.image, p.kind, p.milestone, u.username, u.display_name, CASE WHEN vis(u.avatar_policy, u.id) THEN u.avatar ELSE '' END AS avatar,
  (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS likes,
  EXISTS(SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = ?) AS liked,
  (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comments
FROM posts p JOIN users u ON u.id = p.user_id
  AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id = ?1)
{where}
ORDER BY p.id DESC LIMIT 40
"""


def streak(user_id):
    """Сколько дней подряд были посты «Что изучил сегодня». Возвращает (серия, отмечен ли сегодня)."""
    rows = db().execute(
        "SELECT DISTINCT date(created, 'localtime') AS d FROM posts "
        "WHERE user_id=? AND kind='learned'", (user_id,)).fetchall()
    days = {date.fromisoformat(r["d"]) for r in rows}
    today = date.today()
    cur = today if today in days else today - timedelta(days=1)
    n = 0
    while cur in days:
        n += 1
        cur -= timedelta(days=1)
    return n, today in days


@app.template_filter("days")
def plural_days(n):
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def blocked_between(a, b):
    return bool(db().execute(
        "SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",
        (a, b, b, a)).fetchone())


def send_block_reason(actor_id, owner, policy_field):
    """Почему actor не может писать/комментировать у owner (или None, если можно)."""
    if actor_id == owner["id"]:
        return None
    if blocked_between(actor_id, owner["id"]):
        return "Действие недоступно: один из вас в чёрном списке."
    if owner[policy_field] == "nobody":
        return "Пользователь закрыл это действие для всех."
    if owner[policy_field] == "following" and not db().execute(
            "SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?",
            (owner["id"], actor_id)).fetchone():
        return "Пользователь ограничил это действие: оно доступно только тем, на кого он подписан."
    return None


NOTIF_TEXT = {"like": "оценил(а) ваш пост", "follow": "подписался(лась) на вас",
              "comment": "прокомментировал(а) ваш пост", "message": "написал(а) вам"}
NOTIF_LABELS = {"like": ("Реакции и лайки", "Когда кто-то отреагировал на вашу публикацию"),
                "follow": ("Новые подписчики", "Когда на вас подписались"),
                "comment": ("Комментарии", "Когда комментируют ваши публикации")}


def notify(recipient_id, kind, post_id=None):
    """Создаёт уведомление (коммит делает вызывающий маршрут)."""
    actor = g.user["id"]
    if recipient_id == actor or blocked_between(actor, recipient_id):
        return
    c = db()
    if c.execute("SELECT 1 FROM mutes WHERE user_id=? AND muted_id=?", (recipient_id, actor)).fetchone():
        return
    r = c.execute("SELECT notif_off FROM users WHERE id=?", (recipient_id,)).fetchone()
    if r is None or kind in r["notif_off"].split(","):
        return
    if c.execute("SELECT 1 FROM notifications WHERE user_id=? AND actor_id=? AND kind=? "
                 "AND COALESCE(post_id, 0)=? AND seen=0", (recipient_id, actor, kind, post_id or 0)).fetchone():
        return  # без спама: одно непрочитанное такого же вида
    c.execute("INSERT INTO notifications (user_id, actor_id, kind, post_id) VALUES (?, ?, ?, ?)",
              (recipient_id, actor, kind, post_id))


def get_ttl(a, b):
    lo, hi = sorted((a, b))
    r = db().execute("SELECT days FROM chat_ttl WHERE a=? AND b=?", (lo, hi)).fetchone()
    return r["days"] if r else 0


def purge_dm(a, b):
    """Автоудаление: стираем сообщения диалога старше выбранного срока."""
    days = get_ttl(a, b)
    if days:
        db().execute("DELETE FROM messages WHERE ((sender_id=?1 AND recipient_id=?2) OR (sender_id=?2 AND recipient_id=?1)) "
                     "AND created < datetime('now', ?3)", (a, b, "-%d days" % days))
        db().commit()


def seen_text(u):
    """Статус под именем в личном чате. Если человек скрыл его, показываем юзернейм."""
    if g.user and g.user["seen_policy"] == "nobody" and g.user["id"] != u["id"]:
        return "@" + u["username"]  # как в Telegram: скрыл свой, не видишь чужие
    if not u["last_seen"] or not visible(u["seen_policy"], u["id"]):
        return "@" + u["username"]
    try:
        ago = datetime.utcnow() - datetime.strptime(u["last_seen"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "@" + u["username"]
    if ago < timedelta(minutes=3):
        return "в сети"
    return "был(а) недавно" if ago < timedelta(days=3) else "был(а) давно"


_tags_cache = [0, []]


def top_tags():
    if time.time() - _tags_cache[0] > 60:  # популярные теги, обновляются раз в минуту
        cnt = {}
        for r in db().execute("SELECT body FROM posts ORDER BY id DESC LIMIT 200").fetchall():
            for t in set(re.findall(r"(?<![&\w])#(\w{2,30})", r["body"].lower())):
                cnt[t] = cnt.get(t, 0) + 1
        _tags_cache[:] = [time.time(), sorted(cnt.items(), key=lambda x: -x[1])[:8]]
    return _tags_cache[1]


def side_data():
    c, me = db(), g.user["id"]
    news = c.execute("SELECT p.body, p.created FROM channel_posts p JOIN channels ch ON ch.id = p.channel_id "
                     "WHERE ch.slug = ? ORDER BY p.id DESC LIMIT 3", (OFFICIAL_SLUG,)).fetchall()
    news = [{"title": r["body"].split("\n")[0][:60], "created": r["created"]} for r in news]
    people = c.execute("SELECT u.username, u.display_name, CASE WHEN vis(u.avatar_policy, u.id) THEN u.avatar ELSE '' END AS avatar FROM users u WHERE u.id != ?1 "
                       "AND u.id NOT IN (SELECT followed_id FROM follows WHERE follower_id = ?1) "
                       "AND u.id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id = ?1) ORDER BY u.id DESC LIMIT 5", (me,)).fetchall()
    return {"tags": top_tags(), "news": news, "people": people}


RX_EMOJI = {"like": "👍", "dislike": "👎", "heart": "❤️", "fire": "🔥", "stone": "🗿"}
app.jinja_env.globals["RX_EMOJI"] = RX_EMOJI


def add_reactions(ctx):
    """Добавляет к постам и сообщениям в контексте шаблона список реакций rx = [(эмодзи, число, моя)]."""
    me = g.user["id"] if g.user else 0
    if "ch" in ctx and "posts" in ctx:
        kind, key = "chp", "posts"
    elif "posts" in ctx:
        kind, key = "post", "posts"
    elif "msgs" in ctx:
        kind, key = ("grp" if "grp" in ctx else "dm"), "msgs"
    elif isinstance(ctx.get("p"), sqlite3.Row):
        kind, key = "post", "p"
    else:
        return
    single = key == "p"
    rows = [dict(r) for r in ([ctx[key]] if single else ctx[key])]
    if rows:
        ids = [r["id"] for r in rows]
        found = db().execute(
            "SELECT target_id, emoji, COUNT(*) AS c, MAX(user_id = ?) AS mine FROM reactions "
            "WHERE kind = ? AND target_id IN (%s) GROUP BY target_id, emoji ORDER BY MIN(rowid)" % ",".join("?" * len(ids)),
            [me, kind] + ids).fetchall()
        by = {}
        for r in found:
            by.setdefault(r["target_id"], []).append((r["emoji"], r["c"], bool(r["mine"])))
        for r in rows:
            r["rx"] = by.get(r["id"], [])
            r["v"] = hashlib.md5(repr((r.get("body"), r.get("edited"), r["rx"], r.get("image"), r.get("layout"))).encode()).hexdigest()[:8]
    if kind in ("dm", "grp", "chp"):
        ctx["sig"] = hashlib.md5(repr([(r["id"], r.get("edited", 0), r.get("body"), r["rx"]) for r in rows]).encode()).hexdigest()[:10]
    ctx[key] = rows[0] if single else rows


# ---------- страницы ----------
@app.route("/")
def feed():
    me = g.user["id"] if g.user else 0
    tab = request.args.get("tab", "community")
    posts = news = ()
    if tab == "following" and g.user:
        sql = POSTS_SQL.format(where="WHERE p.user_id = ? OR p.user_id IN "
                                     "(SELECT followed_id FROM follows WHERE follower_id = ?)")
        posts = db().execute(sql, (me, me, me)).fetchall()
    else:
        tab = "community"
        news = db().execute("SELECT * FROM community_posts ORDER BY id LIMIT 5").fetchall()
    st = streak(g.user["id"])[0] if g.user else 0
    official = bool(db().execute("SELECT 1 FROM channels WHERE slug=?", (OFFICIAL_SLUG,)).fetchone())
    return page(FEED, posts=posts, news=news, tab=tab, st=st, official=official, plus=url_for("compose") if g.user else None)


@app.route("/counts")
def counts():
    """Счётчики для вкладок: непрочитанные личные сообщения и уведомления."""
    if not g.user:
        return {"dm": 0, "n": 0}
    return {"dm": g.unread_dm, "n": g.unread}


@app.route("/compose")
@login_required
def compose():
    st, done_today = streak(g.user["id"])
    return page(COMPOSE, st=st, done_today=done_today)


@app.post("/post")
@login_required
def new_post():
    body = request.form.get("body", "").strip()
    if not 1 <= len(body) <= 280:
        flash("Пост должен быть от 1 до 280 символов.")
    else:
        kind = request.form.get("kind")
        kind = kind if kind in ("learned", "milestone") else "post"
        ms = MILESTONES.get(request.form.get("milestone"), "") if kind == "milestone" else ""
        if kind == "milestone" and not ms:
            kind = "post"
        img = save_image("image") or ""
        db().execute("INSERT INTO posts (user_id, body, kind, milestone, image) VALUES (?, ?, ?, ?, ?)",
                     (g.user["id"], body, kind, ms, img))
        _tags_cache[0] = 0  # новые теги сразу попадают в правую колонку
        db().commit()
    return redirect(url_for("feed", tab="following"))


@app.post("/like/<int:post_id>")
@login_required
def like(post_id):
    c = db()
    if not c.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
        abort(404)
    key = (g.user["id"], post_id)
    if c.execute("SELECT 1 FROM likes WHERE user_id=? AND post_id=?", key).fetchone():
        c.execute("DELETE FROM likes WHERE user_id=? AND post_id=?", key)
    else:
        c.execute("INSERT INTO likes (user_id, post_id) VALUES (?, ?)", key)
        notify(c.execute("SELECT user_id FROM posts WHERE id=?", (post_id,)).fetchone()[0], "like", post_id)
    c.commit()
    return redirect(request.referrer or url_for("feed"))


def get_user_or_404(username):
    u = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if u is None:
        abort(404)
    return u


@app.route("/u/<username>")
def profile(username):
    u = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if u is None:  # упоминание несуществующего @имени
        flash("Пользователь @%s не найден." % username)
        return redirect(url_for("feed"))
    is_following = bool(g.user and db().execute(
        "SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?",
        (g.user["id"], u["id"])).fetchone())
    is_blocked = bool(g.user and db().execute(
        "SELECT 1 FROM blocks WHERE blocker_id=? AND blocked_id=?", (g.user["id"], u["id"])).fetchone())
    own = bool(g.user and g.user["id"] == u["id"])
    is_muted = bool(g.user and db().execute("SELECT 1 FROM mutes WHERE user_id=? AND muted_id=?",
                                            (g.user["id"], u["id"])).fetchone())
    c, me = db(), (g.user["id"] if g.user else 0)
    posts = [] if (g.user and blocked_between(me, u["id"])) else c.execute(POSTS_SQL.format(where="WHERE p.user_id = ?"), (me, u["id"])).fetchall()
    chan = c.execute("""SELECT ch.slug, ch.title, ch.descr,
          (SELECT COUNT(*) FROM channel_members m WHERE m.channel_id = ch.id) AS members,
          (SELECT body FROM channel_posts WHERE channel_id = ch.id ORDER BY id DESC LIMIT 1) AS last,
          (SELECT created FROM channel_posts WHERE channel_id = ch.id ORDER BY id DESC LIMIT 1) AS ts
        FROM channels ch WHERE ch.owner_id = ? AND ch.private = 0 ORDER BY (ch.slug = ?) DESC, ch.id LIMIT 1""",
                     (u["id"], OFFICIAL_SLUG)).fetchone()
    months = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
    try:
        d = date.fromisoformat(u["created"][:10])
        joined = "%d %s %d" % (d.day, months[d.month - 1], d.year)
    except ValueError:
        joined = u["created"][:10]
    return page(PROFILE, u=u, is_following=is_following, is_blocked=is_blocked, is_muted=is_muted, own=own,
                status="в сети" if own else seen_text(u), ava=u["avatar"] if visible(u["avatar_policy"], u["id"]) else "",
                bio=u["bio"] if visible(u["bio_policy"], u["id"]) else "", role=FOUNDERS.get(u["username"].lower(), ""),
                posts=posts, miles=[p for p in posts if p["kind"] == "milestone"], chan=chan, joined=joined,
                ttl=get_ttl(me, u["id"]) if g.user and not own else 0, bare=True)


@app.post("/follow/<username>")
@login_required
def follow(username):
    u = get_user_or_404(username)
    if u["id"] != g.user["id"] and not blocked_between(g.user["id"], u["id"]):
        c = db()
        key = (g.user["id"], u["id"])
        if c.execute("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", key).fetchone():
            c.execute("DELETE FROM follows WHERE follower_id=? AND followed_id=?", key)
        else:
            c.execute("INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)", key)
            notify(u["id"], "follow")
        c.commit()
    return redirect(url_for("profile", username=u["username"]))


def clean_image(stream, ext):
    """Перекодирует картинку: метаданные (EXIF, GPS) не переносятся, размер ≤ 2000 px."""
    img = ImageOps.exif_transpose(Image.open(stream))
    img.thumbnail((2000, 2000))
    fmt = {"jpg": "JPEG", "png": "PNG", "gif": "GIF", "webp": "WEBP"}[ext]
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


def sniff_image(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


@app.after_request
def secure_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; media-src 'self' blob:; "
        "script-src 'self' 'nonce-%s'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'"
        % g.get("nonce", ""))
    return resp


def save_image(field):
    return save_image_file(request.files.get(field))


def sniff_video(head):
    if head[4:8] == b"ftyp":
        return "mov" if head[8:10] == b"qt" else "mp4"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return None


def save_video_file(f):
    """Видео сохраняется как есть: проверяем формат по первым байтам и размер."""
    head = f.stream.read(16)
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    ext = sniff_video(head)
    if ext is None:
        flash("Видео: только mp4, mov или webm.")
        return None
    if size > VIDEO_MAX:
        flash("Видео больше 40 МБ. Сожми его или обрежь.")
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = secrets.token_hex(12) + "." + ext
    f.save(os.path.join(UPLOAD_DIR, name))
    return name


def save_image_file(f):
    if not f or not f.filename:
        return None
    head = f.stream.read(12)
    f.stream.seek(0)
    ext = sniff_image(head)
    if ext is None:
        flash("Фото и обложка: только настоящие png, jpg, webp или gif.")
        return None
    try:
        data = clean_image(f.stream, ext)
    except Exception:
        flash("Не удалось обработать изображение.")
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = secrets.token_hex(12) + "." + ext
    with open(os.path.join(UPLOAD_DIR, name), "wb") as fh:
        fh.write(data)
    return name


@app.route("/media/<name>")
def media(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=31536000)  # имена файлов уникальны


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    u = g.user
    if request.method == "POST":
        f = request.form
        started = f.get("started", "").strip()
        try:
            if started:
                date.fromisoformat(started)
        except ValueError:
            started = ""
        avatar = save_image("avatar") or u["avatar"]
        cover = u["cover"]
        db().execute(  # «Что изучаю» и «Дата старта» заморожены: если полей нет в форме, значения не трогаем
            "UPDATE users SET display_name=?, bio=?, goal=?, stack=?, started=?, avatar=?, cover=? WHERE id=?",
            (f.get("display_name", "").strip()[:40], f.get("bio", "").strip()[:160],
             f.get("goal", "").strip()[:80], f.get("stack", u["stack"]).strip()[:80],
             started if "started" in f else u["started"], avatar, cover, u["id"]))
        db().commit()
        return redirect(url_for("profile", username=u["username"]))
    return page(EDIT_PROFILE, u=u)


@app.route("/messages")
@login_required
def inbox():
    me, c = g.user["id"], db()
    for t in c.execute("SELECT a, b FROM chat_ttl WHERE a=?1 OR b=?1", (me,)).fetchall():
        purge_dm(t["a"], t["b"])
    chats = c.execute("""
        SELECT u.username, u.display_name, CASE WHEN vis(u.avatar_policy, u.id) THEN u.avatar ELSE '' END AS avatar, m.body, m.kind, m.sender_id, m.created AS ts, MAX(m.id) AS last_id,
          (SELECT COUNT(*) FROM messages x WHERE x.sender_id = u.id AND x.recipient_id = ?1 AND x.seen = 0) AS unread,
          EXISTS(SELECT 1 FROM mutes WHERE user_id = ?1 AND muted_id = u.id) AS muted
        FROM messages m
        JOIN users u ON u.id = CASE WHEN m.sender_id = ?1 THEN m.recipient_id ELSE m.sender_id END
        WHERE m.sender_id = ?1 OR m.recipient_id = ?1
        GROUP BY u.id""", (me,)).fetchall()
    groups = c.execute("""
        SELECT gr.id, gr.title, gr.created,
          (SELECT created FROM chat_group_messages WHERE group_id = gr.id ORDER BY id DESC LIMIT 1) AS ts,
          (SELECT COALESCE(NULLIF(body, ''), 'Фото') FROM chat_group_messages WHERE group_id = gr.id ORDER BY id DESC LIMIT 1) AS last_body,
          (SELECT COUNT(*) FROM chat_group_members x WHERE x.group_id = gr.id) AS members,
          (SELECT COUNT(*) FROM chat_group_messages x WHERE x.group_id = gr.id AND x.id > gm.last_read AND x.sender_id != ?1) AS unread
        FROM chat_groups gr JOIN chat_group_members gm ON gm.group_id = gr.id AND gm.user_id = ?1""", (me,)).fetchall()
    channels = c.execute("""
        SELECT ch.slug, ch.title, ch.private, cm.user_id IS NOT NULL AS joined,
          COALESCE((SELECT created FROM channel_posts WHERE channel_id = ch.id ORDER BY id DESC LIMIT 1), ch.created) AS ts,
          (SELECT COALESCE(NULLIF(body, ''), 'Фото') FROM channel_posts WHERE channel_id = ch.id ORDER BY id DESC LIMIT 1) AS last_body,
          (SELECT COUNT(*) FROM channel_members m WHERE m.channel_id = ch.id) AS members,
          CASE WHEN cm.user_id IS NOT NULL AND ch.owner_id != ?1
               THEN (SELECT COUNT(*) FROM channel_posts p WHERE p.channel_id = ch.id AND p.id > cm.last_read) ELSE 0 END AS unread
        FROM channels ch LEFT JOIN channel_members cm ON cm.channel_id = ch.id AND cm.user_id = ?1
        WHERE ch.private = 0 OR cm.user_id IS NOT NULL LIMIT 60""", (me,)).fetchall()
    items, more = [], []
    for r in chats:
        if r["kind"] == "ttl":
            sub = ("Автоудаление: " + TTL_LABEL.get(int(r["body"] or 0), "").lower()) if r["body"] not in ("", "0") else "Автоудаление выключено"
        else:
            sub = ("Вы: " if r["sender_id"] == me else "") + (r["body"] or "Фото")
        items.append(dict(kind="dm", href=url_for("chat", username=r["username"]), title=r["display_name"] or r["username"],
                          sub=sub, hue=hue(r["username"]), vbu=r["username"],
                          img=r["avatar"], unread=r["unread"], muted=bool(r["muted"]), ts=r["ts"] or ""))
    for r in groups:
        items.append(dict(kind="grp", href=url_for("group_view", gid=r["id"]), title=r["title"],
                          sub=r["last_body"] or ("%d участн." % r["members"]), hue=hue(r["title"]), img="",
                          unread=r["unread"], muted=False, ts=r["ts"] or r["created"], vbu=""))
    for r in channels:
        it = dict(kind="ch", href=url_for("channel_view", slug=r["slug"]), title=r["title"],
                  sub=r["last_body"] or ("@%s · %d подписч." % (r["slug"], r["members"])), hue=hue(r["title"]), img="",
                  unread=r["unread"], muted=False, ts=r["ts"] or "", vbu="#" + r["slug"])
        (items if r["joined"] else more).append(it)
    items.sort(key=lambda i: i["ts"], reverse=True)
    return page(INBOX, items=items, more=more, plus=url_for("create_menu"))


@app.post("/mute/<username>")
@login_required
def toggle_mute(username):
    u, c = get_user_or_404(username), db()
    key = (g.user["id"], u["id"])
    if u["id"] != g.user["id"]:
        if c.execute("SELECT 1 FROM mutes WHERE user_id=? AND muted_id=?", key).fetchone():
            c.execute("DELETE FROM mutes WHERE user_id=? AND muted_id=?", key)
            flash("Звук уведомлений включён.")
        else:
            c.execute("INSERT INTO mutes (user_id, muted_id) VALUES (?, ?)", key)
            flash("Уведомления от @%s отключены." % u["username"])
        c.commit()
    return redirect(url_for("profile", username=u["username"]))


@app.route("/ttl/<username>", methods=["GET", "POST"])
@login_required
def chat_ttl(username):
    other, me = get_user_or_404(username), g.user["id"]
    if other["id"] == me:
        abort(404)
    if request.method == "POST":
        days = request.form.get("days", type=int)
        if days not in TTL_LABEL:
            abort(400)
        lo, hi = sorted((me, other["id"]))
        if days != get_ttl(me, other["id"]):
            if days:
                db().execute("INSERT OR REPLACE INTO chat_ttl (a, b, days) VALUES (?, ?, ?)", (lo, hi, days))
            else:
                db().execute("DELETE FROM chat_ttl WHERE a=? AND b=?", (lo, hi))
            # системное сообщение в переписке: видят оба собеседника
            db().execute("INSERT INTO messages (sender_id, recipient_id, body, seen, kind) VALUES (?, ?, ?, 0, 'ttl')",
                         (me, other["id"], str(days)))
            db().commit()
            purge_dm(me, other["id"])
        if request.headers.get("X-SPA"):
            return {"ok": True, "label": TTL_LABEL[days]}
        flash("Автоудаление: " + TTL_LABEL[days].lower() + ".")
        return redirect(url_for("profile", username=other["username"]))
    return page(TTL_PAGE, other=other, cur=get_ttl(me, other["id"]))


@app.route("/create")
@login_required
def create_menu():
    return page(CREATE_MENU)


@app.route("/groups/new", methods=["GET", "POST"])
@login_required
def group_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:50]
        names = [n.lstrip("@").lower() for n in re.split(r"[,\s]+", request.form.get("members", "")) if n.strip()]
        c, me, ids, missing, closed = db(), g.user["id"], [], [], []
        for n in dict.fromkeys(names):
            u = c.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()
            if u is None:
                missing.append(n)
            elif u["id"] != me and not blocked_between(me, u["id"]):
                if group_allowed(u["id"]):
                    ids.append(u["id"])
                else:
                    closed.append(n)
        if not title:
            flash("Введи название группы.")
        elif missing:
            flash("Не найдены: " + ", ".join("@" + m for m in missing))
        elif closed:
            flash("Нельзя добавить в группу: " + ", ".join("@" + m for m in closed) + " (ограничили приглашения).")
        elif not ids:
            flash("Добавь хотя бы одного участника.")
        else:
            cur = c.execute("INSERT INTO chat_groups (owner_id, title) VALUES (?, ?)", (me, title))
            for uid in [me] + ids:
                c.execute("INSERT INTO chat_group_members (group_id, user_id) VALUES (?, ?)", (cur.lastrowid, uid))
            c.commit()
            return redirect(url_for("group_view", gid=cur.lastrowid))
    return page(GROUP_NEW)


def group_allowed(uid):
    """Можно ли мне добавить человека в группу (его настройка «Группы и каналы»)."""
    r = db().execute("SELECT group_policy FROM users WHERE id=?", (uid,)).fetchone()
    if r is None or r["group_policy"] == "all":
        return True
    if r["group_policy"] == "nobody":
        return False
    return bool(db().execute("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (uid, g.user["id"])).fetchone())


def my_group(gid):
    c, me = db(), g.user["id"]
    grp = c.execute("SELECT * FROM chat_groups WHERE id=?", (gid,)).fetchone()
    if grp is None or not c.execute("SELECT 1 FROM chat_group_members WHERE group_id=? AND user_id=?",
                                    (gid, me)).fetchone():
        abort(404)
    return grp


@app.route("/g/<int:gid>/info")
@login_required
def group_info(gid):
    grp = my_group(gid)
    rows = db().execute(
        "SELECT u.id, u.username, u.display_name, CASE WHEN vis(u.avatar_policy, u.id) THEN u.avatar ELSE '' END AS avatar, u.last_seen, u.seen_policy, (u.id = ?) AS is_owner "
        "FROM chat_group_members gm JOIN users u ON u.id = gm.user_id WHERE gm.group_id = ? "
        "ORDER BY is_owner DESC, u.username", (grp["owner_id"], gid)).fetchall()
    members = [dict(r, status=seen_text(r)) for r in rows]
    return page(GROUP_INFO, grp=grp, members=members)


@app.post("/g/<int:gid>/add")
@login_required
def group_add(gid):
    grp, c = my_group(gid), db()
    if grp["owner_id"] != g.user["id"]:
        abort(403)
    u = c.execute("SELECT id FROM users WHERE username=?", (request.form.get("username", "").strip().lstrip("@"),)).fetchone()
    if u is None:
        flash("Пользователь не найден.")
    elif blocked_between(g.user["id"], u["id"]) or not group_allowed(u["id"]):
        flash("Нельзя добавить этого пользователя.")
    else:
        c.execute("INSERT OR IGNORE INTO chat_group_members (group_id, user_id) VALUES (?, ?)", (gid, u["id"]))
        c.commit()
    return redirect(url_for("group_info", gid=gid))


@app.post("/g/<int:gid>/leave")
@login_required
def group_leave(gid):
    grp, c, me = my_group(gid), db(), g.user["id"]
    c.execute("DELETE FROM chat_group_members WHERE group_id=? AND user_id=?", (gid, me))
    left = c.execute("SELECT user_id FROM chat_group_members WHERE group_id=? ORDER BY rowid LIMIT 1", (gid,)).fetchone()
    if left is None:
        c.execute("DELETE FROM chat_group_messages WHERE group_id=?", (gid,))
        c.execute("DELETE FROM chat_groups WHERE id=?", (gid,))
    elif grp["owner_id"] == me:
        c.execute("UPDATE chat_groups SET owner_id=? WHERE id=?", (left["user_id"], gid))
    c.commit()
    return redirect(url_for("inbox"))


@app.route("/g/<int:gid>", methods=["GET", "POST"])
@login_required
def group_view(gid):
    c, me = db(), g.user["id"]
    grp = my_group(gid)
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        img = save_image("image") or ""
        if body or img:
            rt = request.form.get("reply_to", type=int)
            if rt and not c.execute("SELECT 1 FROM chat_group_messages WHERE id=? AND group_id=?", (rt, gid)).fetchone():
                rt = None
            c.execute("INSERT INTO chat_group_messages (group_id, sender_id, body, image, reply_to) VALUES (?, ?, ?, ?, ?)",
                      (gid, me, body[:1000], img, rt))
            c.commit()
        return redirect(url_for("group_view", gid=gid))
    c.execute("UPDATE chat_group_members SET last_read = (SELECT COALESCE(MAX(id), 0) FROM chat_group_messages WHERE group_id=?) "
              "WHERE group_id=? AND user_id=?", (gid, gid, me))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM chat_group_members WHERE group_id=?", (gid,)).fetchone()[0]
    msgs = c.execute("SELECT m.*, u.username, (SELECT body FROM chat_group_messages r WHERE r.id = m.reply_to) AS rbody, "
                     "(SELECT u2.username FROM chat_group_messages r JOIN users u2 ON u2.id = r.sender_id WHERE r.id = m.reply_to) AS rname "
                     "FROM chat_group_messages m JOIN users u ON u.id = m.sender_id "
                     "WHERE m.group_id=? AND m.id NOT IN (SELECT msg_id FROM hidden_msgs WHERE user_id=? AND kind='grp') "
                     "ORDER BY m.id DESC LIMIT 200", (gid, me)).fetchall()[::-1]
    return page(GROUP, grp=grp, n=n, msgs=msgs, today=date.today().isoformat(), full=True)


@app.route("/settings")
@login_required
def settings():
    nb = db().execute("SELECT COUNT(*) FROM blocks WHERE blocker_id=?", (g.user["id"],)).fetchone()[0]
    return page(SETTINGS_HUB, nblocked=nb)


@app.route("/settings/look")
@login_required
def settings_look():
    return page(SETTINGS_LOOK)


@app.route("/settings/account")
@login_required
def settings_account():
    return page(SETTINGS_ACCOUNT)


@app.route("/settings/help")
@login_required
def settings_help():
    return page(SETTINGS_HELP)




@app.route("/channels/new", methods=["GET", "POST"])
@login_required
def channel_new():
    f, err = request.form, ""
    if request.method == "POST":
        title, typed = f.get("title", "").strip()[:50], f.get("slug", "").strip()
        slug = make_slug(typed or title)
        c = db()
        if not title:
            err = "Введи название канала."
        elif typed and len(slug) < 3:
            err = "Адрес: от 3 символов, латиница, цифры и _."
        else:
            if len(slug) < 3:
                slug = ("channel_" + slug)[:24]
            base, n = slug, 1
            while c.execute("SELECT 1 FROM channels WHERE slug=?", (slug,)).fetchone():
                if typed:
                    err = "Адрес @%s уже занят. Попробуй другой." % slug
                    break
                n += 1
                slug = (base[:20] + "_" + str(n))[:24]
            if not err:
                cur = c.execute(
                    "INSERT INTO channels (owner_id, slug, title, descr, private, invite, nocopy) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (g.user["id"], slug, title, f.get("descr", "").strip()[:200],
                     1 if f.get("private") == "1" else 0, secrets.token_urlsafe(9),
                     1 if f.get("nocopy") == "on" else 0))
                c.execute("INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)", (cur.lastrowid, g.user["id"]))
                c.commit()
                return redirect(url_for("channel_view", slug=slug))
    return page(CHANNEL_NEW, err=err, f={"title": f.get("title", ""), "slug": f.get("slug", ""), "descr": f.get("descr", ""),
                                         "private": f.get("private", "0"), "nocopy": f.get("nocopy") == "on"})


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
                     "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def make_slug(text):
    """Адрес канала: латиница, цифры и _ (кириллица переводится в латиницу)."""
    s = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    s = re.sub(r"[^a-z0-9_]+", "_", s).strip("_")
    return re.sub(r"_+", "_", s)[:24]


def get_channel(slug):
    ch = db().execute("SELECT * FROM channels WHERE slug=?", (slug,)).fetchone()
    if ch is None:
        abort(404)
    return ch


@app.route("/c/<slug>")
def channel_view(slug):
    ch, c = get_channel(slug), db()
    me = g.user["id"] if g.user else 0
    joined = bool(c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                            (ch["id"], me)).fetchone())
    if ch["private"] and not joined:
        abort(404)
    members = c.execute("SELECT COUNT(*) FROM channel_members WHERE channel_id=?", (ch["id"],)).fetchone()[0]
    if joined:
        c.execute("UPDATE channel_members SET last_read = (SELECT COALESCE(MAX(id), 0) FROM channel_posts WHERE channel_id=?) "
                  "WHERE channel_id=? AND user_id=?", (ch["id"], ch["id"], me))
        c.commit()
    posts = c.execute("SELECT * FROM channel_posts WHERE channel_id=? AND id NOT IN "
                      "(SELECT msg_id FROM hidden_msgs WHERE user_id=? AND kind='chp') "
                      "ORDER BY id DESC LIMIT 100", (ch["id"], me)).fetchall()[::-1]
    media = {}
    if posts:
        ids = [p["id"] for p in posts]
        for m in c.execute("SELECT post_id, kind, name FROM channel_media WHERE post_id IN (%s) ORDER BY pos" % ",".join("?" * len(ids)), ids):
            media.setdefault(m["post_id"], []).append(m)
    return page(CHANNEL, ch=ch, members=members, joined=joined, posts=posts, media=media,
                owner=ch["owner_id"] == me, today=date.today().isoformat(), full=True)


def owned_channel(slug):
    ch = get_channel(slug)
    if ch["owner_id"] != g.user["id"]:
        abort(403)
    return ch


@app.route("/c/<slug>/settings", methods=["GET", "POST"])
@login_required
def channel_settings(slug):
    ch, c = owned_channel(slug), db()
    if not ch["invite"]:
        c.execute("UPDATE channels SET invite=? WHERE id=?", (secrets.token_urlsafe(9), ch["id"]))
        c.commit()
        ch = get_channel(slug)
    if request.method == "POST":
        f = request.form
        c.execute("UPDATE channels SET private=?, nocopy=? WHERE id=?",
                  (1 if f.get("private") == "1" else 0, 1 if f.get("nocopy") == "on" else 0, ch["id"]))
        c.commit()
        return redirect(url_for("channel_view", slug=slug))
    return page(CHANNEL_SETTINGS, ch=ch)


@app.post("/c/<slug>/reset")
@login_required
def channel_reset(slug):
    ch = owned_channel(slug)
    db().execute("UPDATE channels SET invite=? WHERE id=?", (secrets.token_urlsafe(9), ch["id"]))
    db().commit()
    return redirect(url_for("channel_settings", slug=slug))


@app.route("/join/<token>", methods=["GET", "POST"])
@login_required
def channel_invite(token):
    ch = db().execute("SELECT * FROM channels WHERE invite=? AND invite != ''", (token,)).fetchone()
    if ch is None:
        abort(404)
    if request.method == "POST":
        db().execute("INSERT OR IGNORE INTO channel_members (channel_id, user_id) VALUES (?, ?)",
                     (ch["id"], g.user["id"]))
        db().commit()
        return redirect(url_for("channel_view", slug=ch["slug"]))
    return page(CHANNEL_JOIN, ch=ch)


@app.route("/c/<slug>/info")
def channel_info(slug):
    ch, c = get_channel(slug), db()
    me = g.user["id"] if g.user else 0
    joined = bool(c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?", (ch["id"], me)).fetchone())
    if ch["private"] and not joined:
        abort(404)
    members = c.execute("SELECT COUNT(*) FROM channel_members WHERE channel_id=?", (ch["id"],)).fetchone()[0]
    return page(CHANNEL_INFO, ch=ch, members=members, joined=joined, owner=ch["owner_id"] == me)


@app.post("/c/<slug>/join")
@login_required
def channel_join(slug):
    ch, c = get_channel(slug), db()
    is_member = c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                          (ch["id"], g.user["id"])).fetchone()
    if ch["private"] and not is_member:
        abort(404)  # в частный канал — только по ссылке-приглашению
    if ch["owner_id"] != g.user["id"]:
        key = (ch["id"], g.user["id"])
        if c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?", key).fetchone():
            c.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?", key)
        else:
            c.execute("INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)", key)
        c.commit()
    return redirect(url_for("channel_view", slug=slug))


@app.post("/c/<slug>/post")
@login_required
def channel_post(slug):
    ch = get_channel(slug)
    if ch["owner_id"] != g.user["id"]:
        abort(403)
    body = request.form.get("body", "").strip()
    img = save_image("image") or ""
    saved = []
    for f in request.files.getlist("media")[:10]:
        if not f or not f.filename:
            continue
        head = f.stream.read(16)
        f.stream.seek(0)
        if sniff_video(head):
            name = save_video_file(f)
            if name:
                saved.append(("video", name))
        else:
            name = save_image_file(f)
            if name:
                saved.append(("img", name))
    if body or img or saved:
        imgs = [s for s in saved if s[0] == "img"]
        layout = "ba" if request.form.get("ba") == "1" and len(saved) == 2 and len(imgs) == 2 else ""
        c = db()
        cur = c.execute("INSERT INTO channel_posts (channel_id, body, image, layout) VALUES (?, ?, ?, ?)",
                        (ch["id"], body[:2000], img, layout))
        for i, (kind, name) in enumerate(saved):
            c.execute("INSERT INTO channel_media (post_id, kind, name, pos) VALUES (?, ?, ?, ?)", (cur.lastrowid, kind, name, i))
        c.commit()
    return redirect(url_for("channel_view", slug=slug))


@app.route("/messages/<username>", methods=["GET", "POST"])
@login_required
def chat(username):
    other = get_user_or_404(username)
    me = g.user["id"]
    if other["id"] == me:
        return redirect(url_for("inbox"))
    c = db()
    purge_dm(me, other["id"])
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        reason = send_block_reason(me, other, "msg_policy")
        if reason:
            flash(reason)
        elif body or request.files.get("image"):
            img = save_image("image") or ""
            if body or img:
                rt = request.form.get("reply_to", type=int)
                if rt and not c.execute("SELECT 1 FROM messages WHERE id=? AND ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))",
                                        (rt, me, other["id"], other["id"], me)).fetchone():
                    rt = None
                c.execute("INSERT INTO messages (sender_id, recipient_id, body, image, seen, reply_to) VALUES (?, ?, ?, ?, 0, ?)",
                          (me, other["id"], body[:1000], img, rt))
            c.commit()
        return redirect(url_for("chat", username=other["username"]))
    c.execute("UPDATE messages SET seen=1 WHERE recipient_id=? AND sender_id=? AND seen=0", (me, other["id"]))
    c.commit()
    msgs = c.execute("""SELECT m.*, (SELECT body FROM messages r WHERE r.id = m.reply_to) AS rbody,
          (SELECT sender_id FROM messages r WHERE r.id = m.reply_to) AS rsender
        FROM messages m
        WHERE ((m.sender_id=? AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=?))
          AND m.id NOT IN (SELECT msg_id FROM hidden_msgs WHERE user_id=? AND kind='dm')
        ORDER BY m.id""", (me, other["id"], other["id"], me, me)).fetchall()
    return page(CHAT, other=other, msgs=msgs, status=seen_text(other), ttl=get_ttl(me, other["id"]),
                ava=other["avatar"] if visible(other["avatar_policy"], other["id"]) else "",
                today=date.today().isoformat(), full=True)


@app.template_filter("hue")
def hue(name):
    return (212, 252, 160, 24, 334, 190, 232, 280)[sum(map(ord, name)) % 8]  # спокойные цвета из палитры


@app.template_filter("vb")
def verified_badge(username):
    """Галочка рядом с именем (сейчас: основатель проекта)."""
    title = BADGES.get((username or "").lower())
    if not title:
        return ""
    return Markup('<span class="vb" role="img" data-vb="%s" aria-label="%s"><svg viewBox="0 0 24 24" aria-hidden="true">'
                  '<path d="M12 2l2.4 1.8 3-.2 1 2.8 2.5 1.7-.8 2.9.8 2.9-2.5 1.7-1 2.8-3-.2L12 22l-2.4-1.8-3 .2-1-2.8L3.1 15.9l.8-2.9-.8-2.9'
                  'L5.6 8.4l1-2.8 3 .2z" fill="currentColor"/><path d="M8.3 12.2l2.4 2.4 5-5" fill="none" stroke="#fff" stroke-width="2" '
                  'stroke-linecap="round" stroke-linejoin="round"/></svg></span>' % (escape(title), escape(title)))


@app.template_filter("atmark")
def atmark(text):
    """Любое @имя в обычном тексте (статусы, подписи, сообщения) получает подчёркивание."""
    return Markup(re.sub(r"(@[A-Za-z0-9_]{3,20})", r'<span class="at">\1</span>', str(escape(text))))


@app.template_filter("chpost")
def chpost(text):
    """Пост канала: первая строка перед пустой строкой становится заголовком."""
    head, sep, rest = text.replace("\r\n", "\n").partition("\n\n")
    if sep and len(head) <= 80 and "\n" not in head:
        return Markup('<b class="cht">%s</b>' % escape(head)) + linkify(rest)
    return linkify(text)


@app.template_filter("linkify")
def linkify(text):
    """#теги и @упоминания становятся ссылками внутри сайта."""
    out = str(escape(text))
    out = re.sub(r"(?<![&\w])#(\w{2,30})",
                 lambda m: '<a class="hash" href="%s">#%s</a>' % (url_for("tag", name=m.group(1).lower()), m.group(1)), out)
    out = re.sub(r"(?<![\w@.])@([A-Za-z0-9_]{3,20})",
                 lambda m: '<a class="hash at" href="%s">@%s</a>' % (url_for("profile", username=m.group(1)), m.group(1)), out)
    return Markup(out)


@app.route("/p/<int:post_id>")
def post_page(post_id):
    me = g.user["id"] if g.user else 0
    post = db().execute(POSTS_SQL.format(where="WHERE p.id = ?"), (me, post_id)).fetchone()
    if post is None:
        abort(404)
    comments = db().execute(
        "SELECT c.*, u.username FROM comments c JOIN users u ON u.id = c.user_id "
        "WHERE c.post_id = ? AND c.user_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id = ?) "
        "ORDER BY c.id", (post_id, me)).fetchall()
    return page(POST_PAGE, p=post, comments=comments)


@app.post("/p/<int:post_id>/comment")
@login_required
def add_comment(post_id):
    post = db().execute("SELECT user_id FROM posts WHERE id=?", (post_id,)).fetchone()
    if post is None:
        abort(404)
    owner = db().execute("SELECT * FROM users WHERE id=?", (post["user_id"],)).fetchone()
    reason = send_block_reason(g.user["id"], owner, "comment_policy")
    if reason:
        flash(reason)
        return redirect(url_for("post_page", post_id=post_id))
    body = request.form.get("body", "").strip()
    if 1 <= len(body) <= 500:
        db().execute("INSERT INTO comments (post_id, user_id, body) VALUES (?, ?, ?)",
                     (post_id, g.user["id"], body))
        notify(post["user_id"], "comment", post_id)
        db().commit()
    return redirect(url_for("post_page", post_id=post_id))


@app.route("/milestones")
def milestones():
    me = g.user["id"] if g.user else 0
    posts = db().execute(POSTS_SQL.format(where="WHERE p.kind = 'milestone'"), (me,)).fetchall()
    return page(MS_PAGE, posts=posts)


@app.route("/tag/<name>")
def tag(name):
    me = g.user["id"] if g.user else 0
    rows = db().execute(POSTS_SQL.format(where="WHERE pylower(p.body) LIKE ?"),
                        (me, "%#" + name.lower() + "%")).fetchall()
    exact = re.compile(r"#" + re.escape(name) + r"(?!\w)", re.I)
    posts = [r for r in rows if exact.search(r["body"])]
    return page(TAG, name=name, posts=posts)


@app.route("/notifications")
@login_required
def notifications():
    c, me = db(), g.user["id"]
    rows = c.execute(
        "SELECT n.*, u.username, SUBSTR(p.body, 1, 60) AS snippet FROM notifications n "
        "JOIN users u ON u.id = n.actor_id LEFT JOIN posts p ON p.id = n.post_id "
        "WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 50", (me,)).fetchall()
    c.execute("UPDATE notifications SET seen=1 WHERE user_id=?", (me,))
    c.commit()
    g.unread = 0
    return page(NOTIFS, items=rows, texts=NOTIF_TEXT)


@app.route("/settings/notifications", methods=["GET", "POST"])
@login_required
def settings_notifications():
    if request.method == "POST":
        off = ",".join(k for k in NOTIF_LABELS if request.form.get(k) != "on")
        db().execute("UPDATE users SET notif_off=? WHERE id=?", (off, g.user["id"]))
        db().commit()
        return redirect(url_for("settings_notifications"))
    return page(SETTINGS_NOTIFS, labels=NOTIF_LABELS)


@app.post("/block/<username>")
@login_required
def toggle_block(username):
    u, c, me = get_user_or_404(username), db(), g.user["id"]
    if u["id"] != me:
        key = (me, u["id"])
        if c.execute("SELECT 1 FROM blocks WHERE blocker_id=? AND blocked_id=?", key).fetchone():
            c.execute("DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?", key)
        else:
            c.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", key)
            c.execute("DELETE FROM follows WHERE (follower_id=? AND followed_id=?) OR (follower_id=? AND followed_id=?)",
                      (me, u["id"], u["id"], me))
        c.commit()
    if request.form.get("back") == "profile":
        return redirect(url_for("profile", username=u["username"]))
    return redirect(url_for("settings_blocked"))


POLICY_LABEL = {"all": "Все", "following": "Мои подписки", "nobody": "Никто"}
PRIVACY = {  # ключ: (колонка, заголовок, вопрос, варианты, пояснение)
    "seen": ("seen_policy", "Время захода", "Кто видит время моего последнего захода", ("all", "following", "nobody"),
             "Если скрыть время захода от всех, вы тоже не будете видеть его у других."),
    "avatar": ("avatar_policy", "Фотография профиля", "Кто видит мою фотографию", ("all", "following", "nobody"),
               "Остальные увидят первую букву имени вместо фото."),
    "bio": ("bio_policy", "О себе", "Кто видит раздел «О себе»", ("all", "following", "nobody"), ""),
    "groups": ("group_policy", "Группы", "Кто может добавлять меня в группы", ("all", "following", "nobody"), ""),
    "messages": ("msg_policy", "Сообщения", "Кто может писать мне", ("all", "following"),
                 "«Мои подписки»: писать смогут только те, на кого вы подписаны."),
    "comments": ("comment_policy", "Комментарии", "Кто может комментировать мои публикации", ("all", "following", "nobody"), ""),
}
AUTODEL = {0: "Никогда", 1: "1 месяц", 3: "3 месяца", 6: "6 месяцев", 12: "1 год"}


@app.route("/settings/privacy")
@login_required
def settings_privacy():
    rows = [(k, v[1], POLICY_LABEL.get(g.user[v[0]], "Все")) for k, v in PRIVACY.items()]
    return page(SETTINGS_PRIVACY, rows=rows, autodel=AUTODEL.get(g.user["autodel"], "Никогда"))


@app.route("/settings/privacy/<key>", methods=["GET", "POST"])
@login_required
def privacy_item(key):
    if key == "autodel":
        col, title, question, note = "autodel", "Автоудаление аккаунта", "Удалить мой аккаунт, если я не захожу", \
            "Аккаунт удалится вместе со всеми сообщениями и публикациями, если за это время вы ни разу не зайдёте."
        opts = list(AUTODEL.items())
    elif key in PRIVACY:
        col, title, question, values, note = PRIVACY[key]
        opts = [(v, POLICY_LABEL[v]) for v in values]
    else:
        abort(404)
    if request.method == "POST":
        v = request.form.get("v", "")
        allowed = {str(o[0]): o[0] for o in opts}
        if v not in allowed:
            abort(400)
        db().execute("UPDATE users SET %s=? WHERE id=?" % col, (allowed[v], g.user["id"]))
        db().commit()
        return redirect(url_for("privacy_item", key=key))
    return page(PRIV_ITEM, title=title, question=question, opts=opts, cur=g.user[col], note=note)


@app.route("/settings/blocked")
@login_required
def settings_blocked():
    rows = db().execute("SELECT u.username FROM blocks b JOIN users u ON u.id = b.blocked_id "
                        "WHERE b.blocker_id = ? ORDER BY u.username", (g.user["id"],)).fetchall()
    return page(SETTINGS_BLOCKED, blocked=rows)


@app.post("/react/<kind>/<int:tid>")
@login_required
def react(kind, tid):
    key = request.form.get("mode", "")
    if key not in RX_EMOJI:
        abort(400)
    c, me = db(), g.user["id"]
    if kind == "post":
        owner = c.execute("SELECT user_id FROM posts WHERE id=?", (tid,)).fetchone()
        if owner is None:
            abort(404)
    else:
        msg_ctx(kind, tid)  # проверка доступа: чужие чаты недоступны
    cur = c.execute("SELECT emoji FROM reactions WHERE kind=? AND target_id=? AND user_id=?", (kind, tid, me)).fetchone()
    if cur and cur["emoji"] == key:
        c.execute("DELETE FROM reactions WHERE kind=? AND target_id=? AND user_id=?", (kind, tid, me))
    else:
        c.execute("INSERT OR REPLACE INTO reactions (kind, target_id, user_id, emoji) VALUES (?, ?, ?, ?)", (kind, tid, me, key))
        if kind == "post":
            notify(owner["user_id"], "like", tid)
    c.commit()
    ref = request.referrer or ""
    return redirect(ref if ref.startswith(request.host_url) else url_for("feed"))


MSG_TABLE = {"dm": "messages", "grp": "chat_group_messages", "chp": "channel_posts"}


def msg_ctx(kind, mid):
    """Находит сообщение и проверяет доступ. Возвращает (строка, автор, админ, куда вернуться)."""
    c, me = db(), g.user["id"]
    if kind == "dm":
        r = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if r is None or me not in (r["sender_id"], r["recipient_id"]):
            abort(404)
        other = r["recipient_id"] if r["sender_id"] == me else r["sender_id"]
        un = c.execute("SELECT username FROM users WHERE id=?", (other,)).fetchone()["username"]
        return r, r["sender_id"], r["sender_id"], url_for("chat", username=un)
    if kind == "grp":
        r = c.execute("SELECT * FROM chat_group_messages WHERE id=?", (mid,)).fetchone()
        if r is None:
            abort(404)
        gr = c.execute("SELECT * FROM chat_groups WHERE id=?", (r["group_id"],)).fetchone()
        if gr is None or not c.execute("SELECT 1 FROM chat_group_members WHERE group_id=? AND user_id=?",
                                       (gr["id"], me)).fetchone():
            abort(404)
        return r, r["sender_id"], gr["owner_id"], url_for("group_view", gid=gr["id"])
    if kind == "chp":
        r = c.execute("SELECT * FROM channel_posts WHERE id=?", (mid,)).fetchone()
        if r is None:
            abort(404)
        ch = c.execute("SELECT * FROM channels WHERE id=?", (r["channel_id"],)).fetchone()
        if ch["owner_id"] != me and not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                                                  (ch["id"], me)).fetchone():
            abort(404)
        return r, ch["owner_id"], ch["owner_id"], url_for("channel_view", slug=ch["slug"])
    abort(404)


@app.post("/msg/<kind>/<int:mid>/delete")
@login_required
def msg_delete(kind, mid):
    row, author, admin, back = msg_ctx(kind, mid)
    c, me = db(), g.user["id"]
    if request.form.get("mode") == "all":
        if me not in (author, admin):
            abort(403)
        c.execute("DELETE FROM %s WHERE id=?" % MSG_TABLE[kind], (mid,))
        c.execute("DELETE FROM hidden_msgs WHERE kind=? AND msg_id=?", (kind, mid))
        c.execute("DELETE FROM reactions WHERE kind=? AND target_id=?", (kind, mid))
        if kind == "chp":
            c.execute("DELETE FROM channel_media WHERE post_id=?", (mid,))
    else:
        c.execute("INSERT OR IGNORE INTO hidden_msgs (user_id, kind, msg_id) VALUES (?, ?, ?)", (me, kind, mid))
    c.commit()
    return redirect(back)


@app.post("/msg/<kind>/<int:mid>/edit")
@login_required
def msg_edit(kind, mid):
    row, author, admin, back = msg_ctx(kind, mid)
    if g.user["id"] != author:
        abort(403)
    body = request.form.get("body", "").strip()
    if 1 <= len(body) <= 1000:
        db().execute("UPDATE %s SET body=?, edited=1 WHERE id=?" % MSG_TABLE[kind], (body, mid))
        db().commit()
    return redirect(back)


@app.post("/post/<int:post_id>/delete")
@login_required
def delete_post(post_id):
    c = db()
    post = c.execute("SELECT user_id FROM posts WHERE id=?", (post_id,)).fetchone()
    if post is None:
        abort(404)
    if post["user_id"] != g.user["id"]:
        abort(403)
    c.execute("DELETE FROM likes WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM comments WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM notifications WHERE post_id=?", (post_id,))
    c.execute("DELETE FROM reactions WHERE kind='post' AND target_id=?", (post_id,))
    c.execute("DELETE FROM posts WHERE id=?", (post_id,))
    c.commit()
    return redirect(url_for("feed"))


@app.post("/comment/<int:comment_id>/delete")
@login_required
def delete_comment(comment_id):
    c = db()
    row = c.execute("SELECT c.post_id, c.user_id, p.user_id AS author FROM comments c "
                    "JOIN posts p ON p.id = c.post_id WHERE c.id=?", (comment_id,)).fetchone()
    if row is None:
        abort(404)
    if g.user["id"] not in (row["user_id"], row["author"]):
        abort(403)
    c.execute("DELETE FROM comments WHERE id=?", (comment_id,))
    c.commit()
    return redirect(url_for("post_page", post_id=row["post_id"]))


@app.post("/settings/password")
@login_required
def change_password():
    old, new = request.form.get("old", ""), request.form.get("new", "")
    if not check_password_hash(g.user["pw"], old):
        flash("Старый пароль неверный.")
    elif len(new) < 6:
        flash("Новый пароль: минимум 6 символов.")
    else:
        db().execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
        db().commit()
        flash("Пароль изменён.")
    return redirect(url_for("settings_account"))


def erase_user(c, uid):
    """Стирает аккаунт со всеми данными (ручное удаление и автоудаление неактивных)."""
    row = c.execute("SELECT avatar, cover FROM users WHERE id=?", (uid,)).fetchone()
    c.execute("DELETE FROM likes WHERE user_id=? OR post_id IN (SELECT id FROM posts WHERE user_id=?)", (uid, uid))
    c.execute("DELETE FROM comments WHERE user_id=? OR post_id IN (SELECT id FROM posts WHERE user_id=?)", (uid, uid))
    c.execute("DELETE FROM posts WHERE user_id=?", (uid,))
    c.execute("DELETE FROM follows WHERE follower_id=? OR followed_id=?", (uid, uid))
    c.execute("DELETE FROM messages WHERE sender_id=? OR recipient_id=?", (uid, uid))
    c.execute("DELETE FROM notifications WHERE user_id=? OR actor_id=?", (uid, uid))
    c.execute("DELETE FROM reactions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM mutes WHERE user_id=? OR muted_id=?", (uid, uid))
    c.execute("DELETE FROM blocks WHERE blocker_id=? OR blocked_id=?", (uid, uid))
    c.execute("DELETE FROM chat_group_members WHERE user_id=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    for name in (row or ()):
        if name:
            try:
                os.remove(os.path.join(UPLOAD_DIR, name))
            except OSError:
                pass


@app.post("/settings/delete")
@login_required
def delete_account():
    if not check_password_hash(g.user["pw"], request.form.get("password", "")):
        flash("Пароль неверный, аккаунт не удалён.")
        return redirect(url_for("settings_account"))
    erase_user(db(), g.user["id"])
    db().commit()
    session.clear()
    flash("Аккаунт удалён.")
    return redirect(url_for("feed"))




# ---------- регистрация и вход ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
            flash("Имя: 3–20 символов, только латиница, цифры и _.")
        elif len(password) < 6:
            flash("Пароль должен быть не короче 6 символов.")
        else:
            try:
                c = db()
                cur = c.execute("INSERT INTO users (username, pw) VALUES (?, ?)",
                                (username, generate_password_hash(password)))
                c.commit()
                ensure_official(c)
                c.commit()
                session.clear()
                session["uid"] = cur.lastrowid
                return redirect(url_for("feed"))
            except sqlite3.IntegrityError:
                flash("Это имя уже занято.")
    return page(AUTH, title="Регистрация", reg=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        key, now = name.lower(), time.time()
        FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 600]
        if len(FAILS[key]) >= 8:
            flash("Слишком много неудачных попыток. Подожди 10 минут.")
            return page(AUTH, title="Войти", reg=False)
        u = db().execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        if u and check_password_hash(u["pw"], request.form.get("password", "")):
            FAILS.pop(key, None)
            session.clear()
            session["uid"] = u["id"]
            return redirect(url_for("feed"))
        FAILS[key].append(now)
        flash("Неверное имя или пароль.")
    return page(AUTH, title="Войти", reg=False)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("feed"))


init_db()

if __name__ == "__main__":
    app.run(debug=True)
