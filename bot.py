#!/usr/bin/env python3
"""Nextelo Shop Bot — payments, withdraw, stock, Railway-ready"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ═══════════════════════════════════════════════════════════
# Settings — env vars override these (Railway-friendly)
# ═══════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN", "এখানে_টোকেন_বসান")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6363115803") or 0)

CURRENCY = os.getenv("CURRENCY", "BDT")
DEFAULT_PRICE = float(os.getenv("DEFAULT_PRICE", "50"))

BKASH_NUMBER = os.getenv("BKASH_NUMBER", "01406595163")
BKASH_TYPE = os.getenv("BKASH_TYPE", "Personal")
BINANCE_ID = os.getenv("BINANCE_ID", "906064151")
BINANCE_NOTE = os.getenv("BINANCE_NOTE", "শুধু Binance Pay / UID তে পাঠান")
SHOP_NAME = os.getenv("SHOP_NAME", "Nextelo Shop")
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "50"))

# ═══════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "shop.db")))

ST_STOCK = "stock"
ST_PRICE = "price"
ST_ADDBAL = "addbal"
ST_BROADCAST = "broadcast"
ST_TXN_AMOUNT = "txn_amount"
ST_TXN_ID = "txn_id"
ST_DEL_ID = "del_id"
ST_DEL_N = "del_n"
ST_WD_AMOUNT = "wd_amount"
ST_WD_METHOD = "wd_method"
ST_WD_DETAIL = "wd_detail"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("shopbot")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_money(v: float) -> str:
    s = f"{float(v):.2f}"
    if s.endswith(".00"):
        return s[:-3]
    if s.endswith("0") and "." in s:
        return s[:-1]
    return s


def parse_amount(text: str) -> float | None:
    try:
        v = float(text.strip().replace(",", ""))
        if v <= 0:
            return None
        return round(v, 2)
    except ValueError:
        return None


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT,
                balance REAL NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                added_at TEXT NOT NULL,
                sold INTEGER NOT NULL DEFAULT 0,
                sold_to INTEGER, sold_at TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                price REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                amount REAL NOT NULL,
                txn_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS withdraws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_stock_sold ON stock(sold);
            CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_wd_status ON withdraws(status);
            """
        )
        if conn.execute("SELECT 1 FROM settings WHERE key='price'").fetchone() is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('price', ?)",
                (str(DEFAULT_PRICE),),
            )


def get_price() -> float:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='price'").fetchone()
        return float(row["value"]) if row else DEFAULT_PRICE


def set_price(value: float) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('price', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(round(float(value), 2)),),
        )


def upsert_user(user) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user.id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(user_id, username, first_name, balance, banned, created_at) "
                "VALUES(?,?,?,?,0,?)",
                (user.id, user.username, user.first_name, 0.0, utcnow()),
            )
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user.id,)).fetchone()
        else:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (user.username, user.first_name, user.id),
            )
        return dict(row)


def stock_count() -> int:
    with db() as conn:
        return int(conn.execute("SELECT COUNT(*) c FROM stock WHERE sold=0").fetchone()["c"])


def list_stock(limit: int = 15, offset: int = 0) -> list[dict]:
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, payload FROM stock WHERE sold=0 ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        ]


def add_stock_lines(lines: list[str]) -> int:
    n = 0
    with db() as conn:
        for raw in lines:
            item = raw.strip()
            if not item or item.startswith("#"):
                continue
            conn.execute(
                "INSERT INTO stock(payload, added_at, sold) VALUES(?,?,0)",
                (item, utcnow()),
            )
            n += 1
    return n


def delete_stock_ids(ids: list[int]) -> int:
    if not ids:
        return 0
    with db() as conn:
        q = ",".join("?" * len(ids))
        return conn.execute(
            f"DELETE FROM stock WHERE sold=0 AND id IN ({q})", ids
        ).rowcount


def delete_stock_first(n: int) -> int:
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM stock WHERE sold=0 ORDER BY id ASC LIMIT ?", (n,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        return conn.execute(f"DELETE FROM stock WHERE id IN ({q})", ids).rowcount


def delete_stock_last(n: int) -> int:
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM stock WHERE sold=0 ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        return conn.execute(f"DELETE FROM stock WHERE id IN ({q})", ids).rowcount


def clear_unsold() -> int:
    with db() as conn:
        return conn.execute("DELETE FROM stock WHERE sold=0").rowcount


def sell_items(user_id: int, qty: int) -> list[str]:
    price = get_price()
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            raise ValueError("ইউজার নেই। /start দিন।")
        if user["banned"]:
            raise ValueError("⛔ ব্যানড।")
        items = conn.execute(
            "SELECT id, payload FROM stock WHERE sold=0 ORDER BY id LIMIT ?",
            (qty,),
        ).fetchall()
        if len(items) < qty:
            raise ValueError(f"স্টকে মাত্র {len(items)} টা।")
        total = round(price * qty, 2)
        if user["balance"] < total - 1e-9:
            raise ValueError(
                f"ব্যালেন্স কম। দরকার *{fmt_money(total)}* | আছে *{fmt_money(user['balance'])}*"
            )
        payloads = []
        for it in items:
            conn.execute(
                "UPDATE stock SET sold=1, sold_to=?, sold_at=? WHERE id=?",
                (user_id, utcnow(), it["id"]),
            )
            conn.execute(
                "INSERT INTO orders(user_id, payload, price, created_at) VALUES(?,?,?,?)",
                (user_id, it["payload"], price, utcnow()),
            )
            payloads.append(it["payload"])
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=?",
            (total, user_id),
        )
        return payloads


def change_balance(user_id: int, amount: float, absolute: bool = False) -> float:
    amount = round(float(amount), 2)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
            conn.execute(
                "INSERT INTO users(user_id, username, first_name, balance, banned, created_at) "
                "VALUES(?,?,?,?,0,?)",
                (user_id, None, None, 0.0, utcnow()),
            )
        if absolute:
            conn.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
        else:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (amount, user_id),
            )
        return round(
            float(
                conn.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ).fetchone()["balance"]
            ),
            2,
        )


def stats() -> dict:
    with db() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
            "sold": conn.execute("SELECT COUNT(*) c FROM stock WHERE sold=1").fetchone()["c"],
            "avail": conn.execute("SELECT COUNT(*) c FROM stock WHERE sold=0").fetchone()["c"],
            "revenue": conn.execute(
                "SELECT COALESCE(SUM(price),0) s FROM orders"
            ).fetchone()["s"],
            "pending_pay": conn.execute(
                "SELECT COUNT(*) c FROM payments WHERE status='pending'"
            ).fetchone()["c"],
            "pending_wd": conn.execute(
                "SELECT COUNT(*) c FROM withdraws WHERE status='pending'"
            ).fetchone()["c"],
        }


def user_orders(user_id: int, limit: int = 20) -> list[dict]:
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        ]


def all_user_ids() -> list[int]:
    with db() as conn:
        return [
            int(r["user_id"])
            for r in conn.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
        ]


def create_payment(user_id: int, method: str, amount: float, txn_id: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO payments(user_id, method, amount, txn_id, status, created_at) "
            "VALUES(?,?,?,?,'pending',?)",
            (user_id, method, round(amount, 2), txn_id.strip(), utcnow()),
        )
        return int(cur.lastrowid)


def pending_payments(limit: int = 15) -> list[dict]:
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT p.*, u.username, u.first_name FROM payments p "
                "LEFT JOIN users u ON u.user_id=p.user_id "
                "WHERE p.status='pending' ORDER BY p.id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        ]


def get_payment(pid: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def set_payment_status(pid: int, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE payments SET status=?, reviewed_at=? WHERE id=?",
            (status, utcnow(), pid),
        )


def create_withdraw(user_id: int, amount: float, method: str, detail: str) -> int:
    """Deduct balance immediately; refund on reject."""
    amount = round(amount, 2)
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            raise ValueError("ইউজার নেই।")
        if user["banned"]:
            raise ValueError("⛔ ব্যানড।")
        if user["balance"] < amount - 1e-9:
            raise ValueError(
                f"ব্যালেন্স কম। আছে *{fmt_money(user['balance'])}* {CURRENCY}"
            )
        if amount < MIN_WITHDRAW:
            raise ValueError(f"মিনিমাম উইথড্রো *{fmt_money(MIN_WITHDRAW)}* {CURRENCY}")
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=?",
            (amount, user_id),
        )
        cur = conn.execute(
            "INSERT INTO withdraws(user_id, amount, method, detail, status, created_at) "
            "VALUES(?,?,?,?,'pending',?)",
            (user_id, amount, method, detail.strip(), utcnow()),
        )
        return int(cur.lastrowid)


def pending_withdraws(limit: int = 15) -> list[dict]:
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT w.*, u.username, u.first_name FROM withdraws w "
                "LEFT JOIN users u ON u.user_id=w.user_id "
                "WHERE w.status='pending' ORDER BY w.id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        ]


def get_withdraw(wid: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM withdraws WHERE id=?", (wid,)).fetchone()
        return dict(row) if row else None


def set_withdraw_status(wid: int, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE withdraws SET status=?, reviewed_at=? WHERE id=?",
            (status, utcnow(), wid),
        )


def is_admin(uid: int) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID


# ── UI ──

def ikb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def reply_kb(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("🛒 কিনুন"), KeyboardButton("💰 ব্যালেন্স")],
        [KeyboardButton("💳 রিচার্জ"), KeyboardButton("💸 উইথড্রো")],
        [KeyboardButton("🧾 অর্ডার")],
    ]
    if admin:
        rows.append([KeyboardButton("🛠 অ্যাডমিন")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def member_kb(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🛒 কিনুন", callback_data="buy_menu"),
            InlineKeyboardButton("📦 স্টক", callback_data="stock"),
        ],
        [
            InlineKeyboardButton("💰 ব্যালেন্স", callback_data="balance"),
            InlineKeyboardButton("💳 রিচার্জ", callback_data="pay"),
        ],
        [
            InlineKeyboardButton("💸 উইথড্রো", callback_data="wd_menu"),
            InlineKeyboardButton("🧾 অর্ডার", callback_data="orders"),
        ],
        [InlineKeyboardButton("ℹ️ হেল্প", callback_data="help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("🛠 অ্যাডমিন", callback_data="admin_home")])
    return ikb(rows)


def buy_kb() -> InlineKeyboardMarkup:
    return ikb(
        [
            [
                InlineKeyboardButton("1", callback_data="buy_1"),
                InlineKeyboardButton("2", callback_data="buy_2"),
                InlineKeyboardButton("3", callback_data="buy_3"),
                InlineKeyboardButton("5", callback_data="buy_5"),
                InlineKeyboardButton("10", callback_data="buy_10"),
            ],
            [InlineKeyboardButton("« মেনু", callback_data="home")],
        ]
    )


def pay_kb() -> InlineKeyboardMarkup:
    return ikb(
        [
            [InlineKeyboardButton("📱 bKash", callback_data="pay_bkash")],
            [InlineKeyboardButton("🟡 Binance", callback_data="pay_binance")],
            [InlineKeyboardButton("« মেনু", callback_data="home")],
        ]
    )


def wd_method_kb() -> InlineKeyboardMarkup:
    return ikb(
        [
            [InlineKeyboardButton("📱 bKash-এ তুলব", callback_data="wd_bkash")],
            [InlineKeyboardButton("🟡 Binance-এ তুলব", callback_data="wd_binance")],
            [InlineKeyboardButton("« মেনু", callback_data="home")],
        ]
    )


def admin_kb() -> InlineKeyboardMarkup:
    s = stats()
    return ikb(
        [
            [
                InlineKeyboardButton("📊 স্ট্যাটস", callback_data="adm_stats"),
                InlineKeyboardButton(
                    f"⏳ পেমেন্ট ({s['pending_pay']})", callback_data="adm_pending"
                ),
            ],
            [
                InlineKeyboardButton(
                    f"💸 উইথড্রো ({s['pending_wd']})", callback_data="adm_wd"
                ),
            ],
            [
                InlineKeyboardButton("📦 স্টক যোগ", callback_data="adm_addstock"),
                InlineKeyboardButton("🗑 স্টক ডিলিট", callback_data="adm_stockdel"),
            ],
            [
                InlineKeyboardButton("💵 দাম", callback_data="adm_price"),
                InlineKeyboardButton("➕ ব্যালেন্স", callback_data="adm_addbal"),
            ],
            [
                InlineKeyboardButton("📢 ব্রডকাস্ট", callback_data="adm_broadcast"),
                InlineKeyboardButton("📤 এক্সপোর্ট", callback_data="adm_export"),
            ],
            [InlineKeyboardButton("« মেনু", callback_data="home")],
        ]
    )


def stockdel_kb() -> InlineKeyboardMarkup:
    return ikb(
        [
            [InlineKeyboardButton("📋 লিস্ট", callback_data="adm_stocklist")],
            [InlineKeyboardButton("🆔 ID ডিলিট", callback_data="adm_del_id")],
            [
                InlineKeyboardButton("⬆️ আগের N", callback_data="adm_del_first"),
                InlineKeyboardButton("⬇️ শেষের N", callback_data="adm_del_last"),
            ],
            [InlineKeyboardButton("⚠️ সব আনসোল্ড", callback_data="adm_del_all")],
            [InlineKeyboardButton("« অ্যাডমিন", callback_data="admin_home")],
        ]
    )


def pending_list_kb(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for p in items[:8]:
        rows.append(
            [
                InlineKeyboardButton(
                    f"#{p['id']} {p['method']} {fmt_money(p['amount'])}",
                    callback_data=f"payview_{p['id']}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("✅", callback_data=f"payok_{p['id']}"),
                InlineKeyboardButton("❌", callback_data=f"payno_{p['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton("« অ্যাডমিন", callback_data="admin_home")])
    return ikb(rows)


def wd_list_kb(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for w in items[:8]:
        name = w.get("first_name") or "User"
        rows.append(
            [
                InlineKeyboardButton(
                    f"#{w['id']} {name} · {fmt_money(w['amount'])}",
                    callback_data=f"wdview_{w['id']}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ কনফার্ম", callback_data=f"wdok_{w['id']}"
                ),
                InlineKeyboardButton(
                    "🗑 ডিলিট", callback_data=f"wdno_{w['id']}"
                ),
            ]
        )
    rows.append([InlineKeyboardButton("« অ্যাডমিন", callback_data="admin_home")])
    return ikb(rows)


def wd_detail_kb(w: dict) -> InlineKeyboardMarkup:
    return ikb(
        [
            [
                InlineKeyboardButton("✅ কনফার্ম পেমেন্ট", callback_data=f"wdok_{w['id']}"),
                InlineKeyboardButton("🗑 ডিলিট / রিজেক্ট", callback_data=f"wdno_{w['id']}"),
            ],
            [InlineKeyboardButton("« উইথড্রো লিস্ট", callback_data="adm_wd")],
        ]
    )


def back_home() -> InlineKeyboardMarkup:
    return ikb([[InlineKeyboardButton("« মেনু", callback_data="home")]])


def dash(user: dict) -> str:
    return (
        f"*{SHOP_NAME}*\n"
        f"──────────────\n"
        f"👤 {user.get('first_name') or 'Member'}\n"
        f"💰 *{fmt_money(user['balance'])} {CURRENCY}*\n"
        f"🆔 `{user['user_id']}`\n"
        f"──────────────\n"
        f"📦 স্টক *{stock_count()}* | 🏷 *{fmt_money(get_price())} {CURRENCY}*"
    )


def admin_dash() -> str:
    s = stats()
    return (
        f"🛠 *অ্যাডমিন — {SHOP_NAME}*\n"
        f"──────────────\n"
        f"👥 {s['users']} | 📦 {s['avail']} | ✅ {s['sold']}\n"
        f"💵 *{fmt_money(s['revenue'])} {CURRENCY}*\n"
        f"⏳ পেমেন্ট: *{s['pending_pay']}* | 💸 উইথড্রো: *{s['pending_wd']}*\n"
        f"🏷 দাম: *{fmt_money(get_price())}*"
    )


def format_wd_card(w: dict) -> str:
    name = w.get("first_name") or "—"
    uname = w.get("username")
    uname_s = f"@{uname}" if uname else "—"
    return (
        f"💸 *উইথড্রো রিকোয়েস্ট #{w['id']}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 নাম: *{name}*\n"
        f"🔗 ইউজারনেম: {uname_s}\n"
        f"🆔 টেলিগ্রাম আইডি: `{w['user_id']}`\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 পরিমাণ: *{fmt_money(w['amount'])} {CURRENCY}*\n"
        f"📤 মেথড: *{w['method'].upper()}*\n"
        f"📋 ডিটেইল: `{w['detail']}`\n"
        f"🕐 সময়: `{w['created_at']}`\n"
        f"📌 স্ট্যাটাস: *{w['status']}*\n"
        f"━━━━━━━━━━━━━━━━"
    )


async def safe_edit(q, text: str, reply_markup=None) -> None:
    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await q.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            log.exception("edit failed")


# ── commands ──

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user = update.effective_user
    u = upsert_user(user)
    if u["banned"]:
        await update.message.reply_text("⛔ ব্যানড।")
        return
    admin = is_admin(user.id)
    await update.message.reply_text(
        dash(u), parse_mode="Markdown", reply_markup=reply_kb(admin)
    )
    await update.message.reply_text(
        dash(u), parse_mode="Markdown", reply_markup=member_kb(admin)
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    await update.message.reply_text(
        admin_dash(), parse_mode="Markdown", reply_markup=admin_kb()
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ বাতিল।", reply_markup=reply_kb(is_admin(update.effective_user.id))
    )


# ── callbacks ──

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user = upsert_user(q.from_user)
    admin = is_admin(q.from_user.id)

    if user["banned"] and not admin:
        await safe_edit(q, "⛔ ব্যানড।")
        return

    if data == "home":
        await safe_edit(q, dash(user), member_kb(admin))
        return

    if data == "balance":
        await safe_edit(
            q,
            f"💰 *ব্যালেন্স*\n\n*{fmt_money(user['balance'])} {CURRENCY}*\n`{user['user_id']}`",
            back_home(),
        )
        return

    if data == "stock":
        await safe_edit(
            q,
            f"📦 স্টক: *{stock_count()}*\n🏷 *{fmt_money(get_price())} {CURRENCY}*",
            back_home(),
        )
        return

    if data == "pay":
        await safe_edit(q, "💳 *রিচার্জ* — মেথড:", pay_kb())
        return

    if data == "pay_bkash":
        context.user_data["pay_method"] = "bkash"
        context.user_data["state"] = ST_TXN_AMOUNT
        await safe_edit(
            q,
            f"📱 *bKash*\n`{BKASH_NUMBER}` ({BKASH_TYPE})\n\nকত টাকা? `50` / `50.50`\n/cancel",
        )
        return

    if data == "pay_binance":
        context.user_data["pay_method"] = "binance"
        context.user_data["state"] = ST_TXN_AMOUNT
        await safe_edit(
            q,
            f"🟡 *Binance*\n`{BINANCE_ID}`\n{BINANCE_NOTE}\n\nকত? `5` / `2.50`\n/cancel",
        )
        return

    # ── withdraw user ──
    if data == "wd_menu":
        await safe_edit(
            q,
            f"💸 *উইথড্রো*\n\n"
            f"ব্যালেন্স: *{fmt_money(user['balance'])} {CURRENCY}*\n"
            f"মিনিমাম: *{fmt_money(MIN_WITHDRAW)} {CURRENCY}*\n\n"
            f"কোন মেথডে তুলবেন?",
            wd_method_kb(),
        )
        return

    if data == "wd_bkash":
        context.user_data["wd_method"] = "bkash"
        context.user_data["state"] = ST_WD_AMOUNT
        await safe_edit(
            q,
            f"📱 bKash উইথড্রো\nকত টাকা তুলবেন?\nমিনিমাম {fmt_money(MIN_WITHDRAW)}\n/cancel",
        )
        return

    if data == "wd_binance":
        context.user_data["wd_method"] = "binance"
        context.user_data["state"] = ST_WD_AMOUNT
        await safe_edit(
            q,
            f"🟡 Binance উইথড্রো\nকত তুলবেন?\nমিনিমাম {fmt_money(MIN_WITHDRAW)}\n/cancel",
        )
        return

    if data == "orders":
        rows = user_orders(user["user_id"])
        if not rows:
            body = "কোনো অর্ডার নেই।"
        else:
            # email + date time same line
            body = "\n".join(
                f"`{r['payload']}` · `{r['created_at']}`" for r in rows
            )
        await safe_edit(q, f"🧾 *আপনার অর্ডার*\n\n{body}", back_home())
        return

    if data == "help":
        await safe_edit(
            q,
            "ℹ️ *হেল্প*\n"
            "• রিচার্জ → bKash/Binance → TXN\n"
            "• অ্যাডমিন অ্যাপ্রুভ → ব্যালেন্স\n"
            "• কিনুন → ইমেইল + সময় অর্ডারে\n"
            "• উইথড্রো → অ্যাডমিন কনফার্ম",
            back_home(),
        )
        return

    if data == "buy_menu":
        await safe_edit(
            q,
            f"🛒 স্টক *{stock_count()}* | *{fmt_money(get_price())}*\n"
            f"ব্যালেন্স *{fmt_money(user['balance'])}*\nকতটা?",
            buy_kb(),
        )
        return

    if data.startswith("buy_"):
        try:
            qty = int(data.split("_")[1])
        except ValueError:
            return
        try:
            items = sell_items(user["user_id"], qty)
        except ValueError as e:
            await safe_edit(q, f"❌ {e}", back_home())
            return
        body = "\n".join(f"`{x}`" for x in items)
        await safe_edit(q, f"✅ *{qty} টা*\n\n{body}", back_home())
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    ADMIN_ID, f"🛒 `{user['user_id']}` ×{qty}", parse_mode="Markdown"
                )
            except Exception:
                pass
        return

    if not admin:
        await q.answer("অ্যাডমিন নয়", show_alert=True)
        return

    if data in ("admin_home", "adm_stats"):
        await safe_edit(q, admin_dash(), admin_kb())
        return

    if data == "adm_pending":
        items = pending_payments()
        if not items:
            await safe_edit(q, "⏳ পেন্ডিং পেমেন্ট নেই।", admin_kb())
            return
        lines = ["⏳ *পেন্ডিং পেমেন্ট*\n"]
        for p in items:
            un = p.get("username") or p.get("first_name") or "-"
            lines.append(
                f"*{p['id']}*. {p['method'].upper()} *{fmt_money(p['amount'])}* "
                f"— `{p['user_id']}` @{un}\nTXN `{p['txn_id']}`"
            )
        await safe_edit(q, "\n".join(lines), pending_list_kb(items))
        return

    if data.startswith("payview_"):
        pid = int(data.split("_")[1])
        p = get_payment(pid)
        if not p:
            await safe_edit(q, "নেই।", admin_kb())
            return
        await safe_edit(
            q,
            f"#{p['id']} {p['method']} *{fmt_money(p['amount'])}*\n"
            f"`{p['user_id']}` TXN `{p['txn_id']}`\n{p['status']}",
            ikb(
                [
                    [
                        InlineKeyboardButton("✅", callback_data=f"payok_{pid}"),
                        InlineKeyboardButton("❌", callback_data=f"payno_{pid}"),
                    ],
                    [InlineKeyboardButton("«", callback_data="adm_pending")],
                ]
            )
            if p["status"] == "pending"
            else admin_kb(),
        )
        return

    if data.startswith("payok_"):
        pid = int(data.split("_")[1])
        p = get_payment(pid)
        if not p or p["status"] != "pending":
            await safe_edit(q, "প্রসেসড।", admin_kb())
            return
        set_payment_status(pid, "approved")
        new_bal = change_balance(p["user_id"], p["amount"])
        await safe_edit(
            q,
            f"✅ পেমেন্ট #{pid}\n+{fmt_money(p['amount'])} → `{p['user_id']}`\n"
            f"ব্যালেন্স {fmt_money(new_bal)}",
            admin_kb(),
        )
        try:
            await context.bot.send_message(
                p["user_id"],
                f"✅ পেমেন্ট অ্যাপ্রুভ\n+*{fmt_money(p['amount'])} {CURRENCY}*\n"
                f"ব্যালেন্স *{fmt_money(new_bal)}*",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if data.startswith("payno_"):
        pid = int(data.split("_")[1])
        p = get_payment(pid)
        if not p or p["status"] != "pending":
            await safe_edit(q, "প্রসেসড।", admin_kb())
            return
        set_payment_status(pid, "rejected")
        await safe_edit(q, f"❌ পেমেন্ট #{pid} রিজেক্ট", admin_kb())
        try:
            await context.bot.send_message(
                p["user_id"],
                f"❌ পেমেন্ট রিজেক্ট\nTXN `{p['txn_id']}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # ── admin withdraw ──
    if data == "adm_wd":
        items = pending_withdraws()
        if not items:
            await safe_edit(q, "💸 পেন্ডিং উইথড্রো নেই।", admin_kb())
            return
        lines = ["💸 *পেন্ডিং উইথড্রো*\nপ্রতিটিতে কনফার্ম / ডিলিট:\n"]
        for w in items:
            name = w.get("first_name") or "—"
            un = w.get("username") or "—"
            lines.append(
                f"*{w['id']}*. {name} (@{un})\n"
                f"ID `{w['user_id']}` | *{fmt_money(w['amount'])}* | {w['method']}\n"
                f"`{w['detail']}`"
            )
        await safe_edit(q, "\n".join(lines), wd_list_kb(items))
        return

    if data.startswith("wdview_"):
        wid = int(data.split("_")[1])
        w = get_withdraw(wid)
        if not w:
            await safe_edit(q, "নেই।", admin_kb())
            return
        # attach name from users
        with db() as conn:
            urow = conn.execute(
                "SELECT first_name, username FROM users WHERE user_id=?",
                (w["user_id"],),
            ).fetchone()
        if urow:
            w = {**w, "first_name": urow["first_name"], "username": urow["username"]}
        await safe_edit(
            q,
            format_wd_card(w),
            wd_detail_kb(w) if w["status"] == "pending" else admin_kb(),
        )
        return

    if data.startswith("wdok_"):
        wid = int(data.split("_")[1])
        w = get_withdraw(wid)
        if not w or w["status"] != "pending":
            await safe_edit(q, "ইতিমধ্যে প্রসেসড।", admin_kb())
            return
        set_withdraw_status(wid, "approved")
        with db() as conn:
            urow = conn.execute(
                "SELECT first_name, username FROM users WHERE user_id=?",
                (w["user_id"],),
            ).fetchone()
        name = (urow["first_name"] if urow else None) or "—"
        un = (urow["username"] if urow else None) or "—"
        await safe_edit(
            q,
            f"✅ *উইথড্রো কনফার্ম #{wid}*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👤 {name} | @{un}\n"
            f"🆔 `{w['user_id']}`\n"
            f"💰 *{fmt_money(w['amount'])} {CURRENCY}*\n"
            f"📤 {w['method'].upper()} → `{w['detail']}`\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"_পেমেন্ট পাঠানো হয়েছে বলে মার্ক করা হলো_",
            admin_kb(),
        )
        try:
            await context.bot.send_message(
                w["user_id"],
                f"✅ *উইথড্রো সম্পন্ন*\n"
                f"*{fmt_money(w['amount'])} {CURRENCY}* পাঠানো হয়েছে\n"
                f"{w['method'].upper()}: `{w['detail']}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if data.startswith("wdno_"):
        wid = int(data.split("_")[1])
        w = get_withdraw(wid)
        if not w or w["status"] != "pending":
            await safe_edit(q, "ইতিমধ্যে প্রসেসড।", admin_kb())
            return
        set_withdraw_status(wid, "rejected")
        # refund
        new_bal = change_balance(w["user_id"], w["amount"])
        await safe_edit(
            q,
            f"🗑 *উইথড্রো ডিলিট/রিজেক্ট #{wid}*\n"
            f"`{w['user_id']}` — {fmt_money(w['amount'])} রিফান্ড\n"
            f"নতুন ব্যালেন্স: {fmt_money(new_bal)}",
            admin_kb(),
        )
        try:
            await context.bot.send_message(
                w["user_id"],
                f"❌ উইথড্রো রিজেক্ট\n"
                f"*{fmt_money(w['amount'])} {CURRENCY}* ব্যালেন্সে ফেরত\n"
                f"এখন: *{fmt_money(new_bal)}*",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if data == "adm_stockdel":
        await safe_edit(
            q, f"🗑 স্টক ডিলিট\nআনসোল্ড: *{stock_count()}*", stockdel_kb()
        )
        return

    if data == "adm_stocklist":
        items = list_stock(20)
        if not items:
            await safe_edit(q, "খালি।", stockdel_kb())
            return
        lines = [f"`#{i['id']}` {i['payload'][:40]}" for i in items]
        await safe_edit(q, "📋\n" + "\n".join(lines), stockdel_kb())
        return

    if data == "adm_del_id":
        context.user_data["state"] = ST_DEL_ID
        await safe_edit(q, "ID লিখুন: `12` বা `12,15`\n/cancel")
        return

    if data == "adm_del_first":
        context.user_data["state"] = ST_DEL_N
        context.user_data["del_mode"] = "first"
        await safe_edit(q, "আগের কয়টা? সংখ্যা /cancel")
        return

    if data == "adm_del_last":
        context.user_data["state"] = ST_DEL_N
        context.user_data["del_mode"] = "last"
        await safe_edit(q, "শেষের কয়টা? সংখ্যা /cancel")
        return

    if data == "adm_del_all":
        n = clear_unsold()
        await safe_edit(q, f"⚠️ {n} মুছেছে।", admin_kb())
        return

    if data == "adm_addstock":
        context.user_data["state"] = ST_STOCK
        await safe_edit(q, "📦 লাইন বাই লাইন ইমেইল /cancel")
        return

    if data == "adm_price":
        context.user_data["state"] = ST_PRICE
        await safe_edit(q, f"দাম এখন {fmt_money(get_price())}\nনতুন দাম /cancel")
        return

    if data == "adm_addbal":
        context.user_data["state"] = ST_ADDBAL
        await safe_edit(q, "`USER_ID AMOUNT` যেমন `123 50.50` /cancel")
        return

    if data == "adm_broadcast":
        context.user_data["state"] = ST_BROADCAST
        await safe_edit(q, "📢 টেক্সট /cancel")
        return

    if data == "adm_export":
        with db() as conn:
            rows = conn.execute(
                "SELECT id, payload, sold, sold_to, added_at, sold_at FROM stock ORDER BY id"
            ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "payload", "sold", "sold_to", "added_at", "sold_at"])
        for r in rows:
            w.writerow(
                [r["id"], r["payload"], r["sold"], r["sold_to"], r["added_at"], r["sold_at"]]
            )
        f = io.BytesIO(buf.getvalue().encode("utf-8"))
        f.name = "stock.csv"
        await context.bot.send_document(q.message.chat_id, document=f)
        await q.answer("CSV")
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")
    admin = is_admin(user.id)

    if text == "🛒 কিনুন":
        u = upsert_user(user)
        await update.message.reply_text(
            f"🛒 *{stock_count()}* | {fmt_money(get_price())}\n"
            f"ব্যালেন্স {fmt_money(u['balance'])}\nকতটা?",
            parse_mode="Markdown",
            reply_markup=buy_kb(),
        )
        return

    if text == "💰 ব্যালেন্স":
        u = upsert_user(user)
        await update.message.reply_text(
            f"💰 *{fmt_money(u['balance'])} {CURRENCY}*",
            parse_mode="Markdown",
            reply_markup=member_kb(admin),
        )
        return

    if text == "💳 রিচার্জ":
        await update.message.reply_text("💳 মেথড:", reply_markup=pay_kb())
        return

    if text == "💸 উইথড্রো":
        u = upsert_user(user)
        await update.message.reply_text(
            f"💸 ব্যালেন্স *{fmt_money(u['balance'])}*\nমিনিমাম {fmt_money(MIN_WITHDRAW)}\nমেথড:",
            parse_mode="Markdown",
            reply_markup=wd_method_kb(),
        )
        return

    if text == "🧾 অর্ডার":
        rows = user_orders(user.id)
        if not rows:
            body = "কোনো অর্ডার নেই।"
        else:
            body = "\n".join(
                f"`{r['payload']}` · `{r['created_at']}`" for r in rows
            )
        await update.message.reply_text(
            f"🧾 *অর্ডার*\n\n{body}",
            parse_mode="Markdown",
            reply_markup=member_kb(admin),
        )
        return

    if text == "🛠 অ্যাডমিন" and admin:
        await update.message.reply_text(
            admin_dash(), parse_mode="Markdown", reply_markup=admin_kb()
        )
        return

    # deposit amount → txn
    if state == ST_TXN_AMOUNT:
        amt = parse_amount(text)
        if amt is None:
            await update.message.reply_text("সঠিক পরিমাণ: 50 বা 12.50")
            return
        context.user_data["pay_amount"] = amt
        context.user_data["state"] = ST_TXN_ID
        await update.message.reply_text(
            f"পরিমাণ *{fmt_money(amt)}*\nএখন *TXN নম্বর* পাঠান।",
            parse_mode="Markdown",
        )
        return

    if state == ST_TXN_ID:
        txn = text.strip()
        if len(txn) < 4:
            await update.message.reply_text("TXN ঠিকমতো লিখুন।")
            return
        method = context.user_data.get("pay_method", "bkash")
        amount = float(context.user_data.get("pay_amount", 0))
        pid = create_payment(user.id, method, amount, txn)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ রিকোয়েস্ট `#{pid}`\n{method.upper()} *{fmt_money(amount)}*\n"
            f"TXN `{txn}`",
            parse_mode="Markdown",
            reply_markup=reply_kb(admin),
        )
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⏳ পেমেন্ট #{pid}\n`{user.id}` @{user.username}\n"
                    f"{method.upper()} *{fmt_money(amount)}*\nTXN `{txn}`",
                    parse_mode="Markdown",
                    reply_markup=ikb(
                        [
                            [
                                InlineKeyboardButton("✅", callback_data=f"payok_{pid}"),
                                InlineKeyboardButton("❌", callback_data=f"payno_{pid}"),
                            ]
                        ]
                    ),
                )
            except Exception:
                log.exception("admin pay notify")
        return

    # withdraw amount → detail
    if state == ST_WD_AMOUNT:
        amt = parse_amount(text)
        if amt is None:
            await update.message.reply_text("সঠিক পরিমাণ দিন।")
            return
        context.user_data["wd_amount"] = amt
        context.user_data["state"] = ST_WD_DETAIL
        method = context.user_data.get("wd_method", "bkash")
        hint = "bKash নম্বর" if method == "bkash" else "Binance Pay ID / UID"
        await update.message.reply_text(
            f"পরিমাণ *{fmt_money(amt)}*\nআপনার *{hint}* লিখুন।",
            parse_mode="Markdown",
        )
        return

    if state == ST_WD_DETAIL:
        detail = text.strip()
        if len(detail) < 5:
            await update.message.reply_text("সঠিক নম্বর/ID দিন।")
            return
        method = context.user_data.get("wd_method", "bkash")
        amount = float(context.user_data.get("wd_amount", 0))
        try:
            wid = create_withdraw(user.id, amount, method, detail)
        except ValueError as e:
            context.user_data.clear()
            await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
            return
        context.user_data.clear()
        u = upsert_user(user)
        await update.message.reply_text(
            f"✅ উইথড্রো রিকোয়েস্ট `#{wid}`\n"
            f"*{fmt_money(amount)} {CURRENCY}* · {method.upper()}\n"
            f"`{detail}`\n\nঅ্যাডমিন কনফার্মের অপেক্ষা।\n"
            f"ব্যালেন্স এখন: *{fmt_money(u['balance'])}*",
            parse_mode="Markdown",
            reply_markup=reply_kb(admin),
        )
        if ADMIN_ID:
            try:
                card = (
                    f"💸 *নতুন উইথড্রো #{wid}*\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"👤 নাম: *{user.first_name or '—'}*\n"
                    f"🔗 @{user.username or '—'}\n"
                    f"🆔 `{user.id}`\n"
                    f"💰 *{fmt_money(amount)} {CURRENCY}*\n"
                    f"📤 {method.upper()} → `{detail}`\n"
                    f"━━━━━━━━━━━━━━━━"
                )
                await context.bot.send_message(
                    ADMIN_ID,
                    card,
                    parse_mode="Markdown",
                    reply_markup=ikb(
                        [
                            [
                                InlineKeyboardButton(
                                    "✅ কনফার্ম", callback_data=f"wdok_{wid}"
                                ),
                                InlineKeyboardButton(
                                    "🗑 ডিলিট", callback_data=f"wdno_{wid}"
                                ),
                            ]
                        ]
                    ),
                )
            except Exception:
                log.exception("admin wd notify")
        return

    if admin and state == ST_DEL_ID:
        try:
            ids = [int(x) for x in text.replace(" ", "").split(",") if x]
        except ValueError:
            await update.message.reply_text("12 বা 12,15")
            return
        n = delete_stock_ids(ids)
        context.user_data.clear()
        await update.message.reply_text(
            f"🗑 {n} ডিলিট। স্টক {stock_count()}", reply_markup=admin_kb()
        )
        return

    if admin and state == ST_DEL_N:
        try:
            n = int(text.strip())
            if n < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("সংখ্যা দিন")
            return
        mode = context.user_data.get("del_mode", "first")
        deleted = delete_stock_first(n) if mode == "first" else delete_stock_last(n)
        context.user_data.clear()
        await update.message.reply_text(
            f"🗑 {deleted} ডিলিট। স্টক {stock_count()}", reply_markup=admin_kb()
        )
        return

    if admin and state == ST_STOCK:
        n = add_stock_lines(text.splitlines())
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ {n} যোগ। স্টক {stock_count()}", reply_markup=admin_kb()
        )
        return

    if admin and state == ST_PRICE:
        amt = parse_amount(text)
        if amt is None:
            await update.message.reply_text("50.50")
            return
        set_price(amt)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ দাম {fmt_money(amt)}", reply_markup=admin_kb()
        )
        return

    if admin and state == ST_ADDBAL:
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("USER_ID AMOUNT")
            return
        try:
            uid = int(parts[0])
            amt = parse_amount(parts[1])
            if amt is None:
                raise ValueError
        except ValueError:
            await update.message.reply_text("123 50.50")
            return
        new_bal = change_balance(uid, amt)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ `{uid}` → {fmt_money(new_bal)}",
            parse_mode="Markdown",
            reply_markup=admin_kb(),
        )
        try:
            await context.bot.send_message(
                uid,
                f"✅ +*{fmt_money(amt)}*\nব্যালেন্স *{fmt_money(new_bal)}*",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if admin and state == ST_BROADCAST:
        ids = all_user_ids()
        ok = fail = 0
        for uid in ids:
            try:
                await context.bot.send_message(uid, text)
                ok += 1
            except Exception:
                fail += 1
        context.user_data.clear()
        await update.message.reply_text(
            f"📢 {ok}/{fail}", reply_markup=admin_kb()
        )
        return


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if is_admin(user.id) or not ADMIN_ID:
        return
    try:
        await context.bot.forward_message(
            ADMIN_ID, update.effective_chat.id, update.message.message_id
        )
        await context.bot.send_message(
            ADMIN_ID, f"⬆️ `{user.id}` @{user.username}", parse_mode="Markdown"
        )
        await update.message.reply_text("✅ পাঠানো।")
    except Exception:
        pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error: %s", context.error)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN.startswith("এখানে_") or "XXXX" in BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env or in bot.py")
    if not ADMIN_ID:
        raise SystemExit("Set ADMIN_ID")

    init_db()
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Bot starting…")
    print(f"✅ {SHOP_NAME} | Admin {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
