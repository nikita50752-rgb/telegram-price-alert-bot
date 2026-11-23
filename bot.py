"""
Price Alert Bot — Telegram bot that watches crypto prices and pings you when it matters.

Features:
  * any coin listed on CoinGecko (BTC, ETH, TON, SOL, ... ~15 000 coins) — found by ticker or name
  * three alert types: price goes ABOVE a level, BELOW a level, or MOVES by ±N % (repeating)
  * one batched price request per minute for all users (stays within free API limits)
  * "My alerts" list with one-tap delete, quick "Prices" view of the coins you watch
  * Russian / English interface (auto-detected from Telegram, switchable)
  * no API key needed; data is kept in SQLite

Run:  python bot.py   (settings are read from .env, see .env.example)
"""

import asyncio
import html
import logging
import os
import sqlite3
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BASE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- settings
def load_env(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE per line), so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


load_env(BASE / ".env")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHECK_SECONDS = max(30, int(os.environ.get("CHECK_SECONDS", "60")))
MAX_ALERTS = int(os.environ.get("MAX_ALERTS_PER_USER", "20"))
CURRENCY = os.environ.get("CURRENCY", "usd").lower()
CG = "https://api.coingecko.com/api/v3"

# ---------------------------------------------------------------- texts (RU / EN)
T = {
    "start": {"ru": "👋 Я слежу за ценами криптовалют и напишу, когда цена дойдёт до нужного уровня.\n\nВыберите действие:",
              "en": "👋 I watch crypto prices and message you when a price reaches your level.\n\nChoose an action:"},
    "btn_new": {"ru": "➕ Новое оповещение", "en": "➕ New alert"},
    "btn_list": {"ru": "📋 Мои оповещения", "en": "📋 My alerts"},
    "btn_prices": {"ru": "💹 Цены", "en": "💹 Prices"},
    "btn_lang": {"ru": "🌐 English", "en": "🌐 Русский"},
    "btn_menu": {"ru": "🏠 Меню", "en": "🏠 Menu"},
    "ask_coin": {"ru": "Напишите монету: тикер или название (например: <b>TON</b>, <b>BTC</b>, <b>solana</b>).",
                 "en": "Send a coin: ticker or name (e.g. <b>TON</b>, <b>BTC</b>, <b>solana</b>)."},
    "not_found": {"ru": "😕 Не нашёл такую монету. Попробуйте другой тикер или полное название.",
                  "en": "😕 Coin not found. Try another ticker or the full name."},
    "pick_coin": {"ru": "Нашлось несколько, выберите:", "en": "Several matches, pick one:"},
    "api_error": {"ru": "⚠️ Сервис цен сейчас не отвечает. Попробуйте через минуту.",
                  "en": "⚠️ The price service is not responding. Try again in a minute."},
    "coin_info": {"ru": "<b>{name}</b> ({sym})\nСейчас: <b>{price}</b>  ({chg} за 24 ч)\n\nКогда сообщить?",
                  "en": "<b>{name}</b> ({sym})\nNow: <b>{price}</b>  ({chg} in 24h)\n\nWhen should I alert you?"},
    "btn_above": {"ru": "📈 Цена выше…", "en": "📈 Price above…"},
    "btn_below": {"ru": "📉 Цена ниже…", "en": "📉 Price below…"},
    "btn_move": {"ru": "⚡ Изменение на ±%", "en": "⚡ Move by ±%"},
    "ask_level": {"ru": "Введите цену в {cur} (сейчас {price}):", "en": "Enter the price in {cur} (now {price}):"},
    "ask_pct": {"ru": "На сколько процентов? Например: <b>5</b>", "en": "How many percent? For example: <b>5</b>"},
    "bad_number": {"ru": "Нужно число больше нуля, например 3.5", "en": "Please send a number above zero, e.g. 3.5"},
    "already": {"ru": "Цена уже {side} этого уровня — оповещение сработало бы сразу. Введите другое число.",
                "en": "The price is already {side} that level, so the alert would fire right away. Send another number."},
    "side_above": {"ru": "выше", "en": "above"},
    "side_below": {"ru": "ниже", "en": "below"},
    "saved": {"ru": "✅ Готово! Напишу, когда {what}.", "en": "✅ Done! I'll message you when {what}."},
    "w_above": {"ru": "{sym} поднимется до {lvl}", "en": "{sym} rises to {lvl}"},
    "w_below": {"ru": "{sym} опустится до {lvl}", "en": "{sym} drops to {lvl}"},
    "w_move": {"ru": "{sym} изменится на {pct}% (от {lvl})", "en": "{sym} moves by {pct}% (from {lvl})"},
    "too_many": {"ru": "У вас уже {n} оповещений — это максимум. Удалите лишние в «Мои оповещения».",
                 "en": "You already have {n} alerts, which is the limit. Delete some in “My alerts”."},
    "list_empty": {"ru": "Оповещений пока нет.", "en": "No alerts yet."},
    "list_head": {"ru": "Ваши оповещения (нажмите, чтобы удалить):", "en": "Your alerts (tap to delete):"},
    "deleted": {"ru": "🗑 Удалено.", "en": "🗑 Deleted."},
    "prices_empty": {"ru": "Добавьте оповещение — и здесь появятся цены ваших монет.",
                     "en": "Add an alert and the prices of your coins will show up here."},
    "hit_above": {"ru": "🚀 <b>{sym}</b> поднялся до <b>{price}</b> (ваш уровень {lvl})",
                  "en": "🚀 <b>{sym}</b> rose to <b>{price}</b> (your level {lvl})"},
    "hit_below": {"ru": "🔻 <b>{sym}</b> опустился до <b>{price}</b> (ваш уровень {lvl})",
                  "en": "🔻 <b>{sym}</b> dropped to <b>{price}</b> (your level {lvl})"},
    "hit_move": {"ru": "⚡ <b>{sym}</b> {dir} на <b>{chg}</b>: {old} → <b>{price}</b>\nСледующее оповещение — при изменении ещё на {pct}% от новой цены.",
                 "en": "⚡ <b>{sym}</b> {dir} <b>{chg}</b>: {old} → <b>{price}</b>\nNext alert after another {pct}% move from the new price."},
    "dir_up": {"ru": "вырос", "en": "is up"},
    "dir_down": {"ru": "упал", "en": "is down"},
}


def t(key: str, lang: str, **kw) -> str:
    return T[key][lang].format(**kw)


def lang_of(user) -> str:
    saved = db_get_lang(user.id)
    if saved:
        return saved
    return "ru" if (user.language_code or "").startswith(("ru", "uk", "be", "kk")) else "en"


def fmt_price(p: float) -> str:
    sign = "$" if CURRENCY == "usd" else ""
    suffix = "" if CURRENCY == "usd" else " " + CURRENCY.upper()
    if p >= 1000:
        s = f"{p:,.0f}"
    elif p >= 1:
        s = f"{p:,.2f}"
    elif p >= 0.01:
        s = f"{p:.4f}"
    else:
        s = f"{p:.8f}".rstrip("0")
    return f"{sign}{s}{suffix}"


def parse_number(text: str):
    try:
        v = float(text.replace(" ", "").replace(",", ".").replace("$", "").replace("%", ""))
        return v if v > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------- storage
DB = sqlite3.connect(BASE / "alerts.db")
DB.executescript("""
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, coin_id TEXT, symbol TEXT,
    kind TEXT, value REAL, base REAL, active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, lang TEXT);
""")


def db_get_lang(uid: int):
    row = DB.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else None


def db_set_lang(uid: int, lang: str) -> None:
    DB.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, lang))
    DB.commit()


def user_alerts(uid: int):
    return DB.execute("SELECT id, coin_id, symbol, kind, value, base FROM alerts WHERE user_id=? AND active=1 ORDER BY id",
                      (uid,)).fetchall()


# ---------------------------------------------------------------- CoinGecko
class Prices:
    """Tiny async client with a short cache, so many users share one request."""

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, tuple[float, dict]] = {}

    async def _get(self, path: str, **params):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                                 headers={"User-Agent": "price-alert-bot/1.0"})
        async with self.session.get(CG + path, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"CoinGecko {r.status}")
            return await r.json()

    async def search(self, query: str) -> list[dict]:
        data = await self._get("/search", query=query)
        coins = data.get("coins") or []
        q = query.lower()
        exact = [c for c in coins if c["symbol"].lower() == q or c["id"] == q or c["name"].lower() == q]
        if coins and len(exact) >= 1 and exact[0] is coins[0]:
            return [coins[0]]  # the best-ranked coin is an exact match (BTC, ETH, ...) -> no need to ask
        # otherwise let the user pick: exact matches + the biggest coins, ordered by market cap rank
        exact = exact[:2]
        others = [c for c in coins if c not in exact][:5 - len(exact)]
        return sorted(exact + others, key=lambda c: c.get("market_cap_rank") or 10 ** 9)

    async def quotes(self, ids: list[str], max_age: float = 20) -> dict:
        now = time.time()
        need = [i for i in ids if i not in self.cache or now - self.cache[i][0] > max_age]
        for k in range(0, len(need), 200):  # API accepts long id lists; split just in case
            part = need[k:k + 200]
            data = await self._get("/simple/price", ids=",".join(part), vs_currencies=CURRENCY,
                                   include_24hr_change="true")
            for cid, v in data.items():
                if CURRENCY in v:
                    self.cache[cid] = (now, {"price": float(v[CURRENCY]),
                                             "chg": float(v.get(f"{CURRENCY}_24h_change") or 0)})
        return {i: self.cache[i][1] for i in ids if i in self.cache}


PRICES = Prices()


# ---------------------------------------------------------------- keyboards
def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=a, callback_data=b) for a, b in row] for row in rows])


def menu_kb(lang: str) -> InlineKeyboardMarkup:
    return kb([[(t("btn_new", lang), "new")], [(t("btn_list", lang), "list"), (t("btn_prices", lang), "prices")],
               [(t("btn_lang", lang), "lang")]])


def back_kb(lang: str) -> InlineKeyboardMarkup:
    return kb([[(t("btn_menu", lang), "menu")]])


# ---------------------------------------------------------------- dialog
class NewAlert(StatesGroup):
    coin = State()
    level = State()


router = Router()


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(t("start", lang_of(m.from_user)), reply_markup=menu_kb(lang_of(m.from_user)))


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = lang_of(c.from_user)
    await c.message.edit_text(t("start", lang), reply_markup=menu_kb(lang))


@router.callback_query(F.data == "lang")
async def cb_lang(c: CallbackQuery):
    lang = "en" if lang_of(c.from_user) == "ru" else "ru"
    db_set_lang(c.from_user.id, lang)
    await c.message.edit_text(t("start", lang), reply_markup=menu_kb(lang))


@router.callback_query(F.data == "new")
async def cb_new(c: CallbackQuery, state: FSMContext):
    lang = lang_of(c.from_user)
    if len(user_alerts(c.from_user.id)) >= MAX_ALERTS:
        await c.answer(t("too_many", lang, n=MAX_ALERTS), show_alert=True)
        return
    await state.set_state(NewAlert.coin)
    await c.message.edit_text(t("ask_coin", lang), reply_markup=back_kb(lang))


async def show_coin(target: Message, state: FSMContext, lang: str, coin: dict, edit: bool):
    try:
        q = (await PRICES.quotes([coin["id"]])).get(coin["id"])
    except Exception:
        q = None
    if not q:
        await target.answer(t("api_error", lang), reply_markup=back_kb(lang))
        return
    await state.update_data(coin_id=coin["id"], symbol=coin["symbol"].upper(), price=q["price"])
    text = t("coin_info", lang, name=html.escape(coin["name"]), sym=coin["symbol"].upper(),
             price=fmt_price(q["price"]), chg=f"{q['chg']:+.1f}%")
    markup = kb([[(t("btn_above", lang), "k:above"), (t("btn_below", lang), "k:below")],
                 [(t("btn_move", lang), "k:move")], [(t("btn_menu", lang), "menu")]])
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(NewAlert.coin, F.text)
async def got_coin(m: Message, state: FSMContext):
    lang = lang_of(m.from_user)
    try:
        found = await PRICES.search(m.text.strip()[:40])
    except Exception:
        await m.answer(t("api_error", lang), reply_markup=back_kb(lang))
        return
    if not found:
        await m.answer(t("not_found", lang))
        return
    if len(found) == 1:  # one exact match (e.g. "TON") -> straight to the coin card
        await show_coin(m, state, lang, found[0], edit=False)
        return
    await state.update_data(choices={c["id"]: {"id": c["id"], "symbol": c["symbol"], "name": c["name"]} for c in found})
    rows = [[(f"{c['name']} ({c['symbol'].upper()})", "c:" + c["id"][:60])] for c in found]
    await m.answer(t("pick_coin", lang), reply_markup=kb(rows + [[(t("btn_menu", lang), "menu")]]))


@router.callback_query(NewAlert.coin, F.data.startswith("c:"))
async def picked_coin(c: CallbackQuery, state: FSMContext):
    coin = (await state.get_data()).get("choices", {}).get(c.data[2:])
    if coin:
        await show_coin(c.message, state, lang_of(c.from_user), coin, edit=True)


@router.callback_query(F.data.startswith("k:"))
async def picked_kind(c: CallbackQuery, state: FSMContext):
    lang, data = lang_of(c.from_user), await state.get_data()
    if "coin_id" not in data:
        await cb_menu(c, state)
        return
    kind = c.data[2:]
    await state.update_data(kind=kind)
    await state.set_state(NewAlert.level)
    text = t("ask_pct", lang) if kind == "move" else t("ask_level", lang, cur=CURRENCY.upper(), price=fmt_price(data["price"]))
    await c.message.edit_text(text, reply_markup=back_kb(lang))


@router.message(NewAlert.level, F.text)
async def got_level(m: Message, state: FSMContext):
    lang, data = lang_of(m.from_user), await state.get_data()
    value = parse_number(m.text)
    if value is None or (data["kind"] == "move" and value >= 1000):
        await m.answer(t("bad_number", lang))
        return
    price, kind, sym = data["price"], data["kind"], data["symbol"]
    if kind == "above" and price >= value or kind == "below" and price <= value:
        await m.answer(t("already", lang, side=t("side_" + kind, lang)))
        return
    DB.execute("INSERT INTO alerts (user_id, coin_id, symbol, kind, value, base, created) VALUES (?,?,?,?,?,?,?)",
               (m.from_user.id, data["coin_id"], sym, kind, value, price, time.time()))
    DB.commit()
    await state.clear()
    what = t("w_" + kind, lang, sym=sym, lvl=fmt_price(value if kind != "move" else price), pct=f"{value:g}")
    await m.answer(t("saved", lang, what=what), reply_markup=menu_kb(lang))


@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery):
    lang = lang_of(c.from_user)
    rows = []
    for aid, _cid, sym, kind, value, base in user_alerts(c.from_user.id):
        label = {"above": f"📈 {sym} ≥ {fmt_price(value)}", "below": f"📉 {sym} ≤ {fmt_price(value)}",
                 "move": f"⚡ {sym} ±{value:g}% ({fmt_price(base)})"}[kind]
        rows.append([("🗑 " + label, f"d:{aid}")])
    text = t("list_head", lang) if rows else t("list_empty", lang)
    await c.message.edit_text(text, reply_markup=kb(rows + [[(t("btn_menu", lang), "menu")]]))


@router.callback_query(F.data.startswith("d:"))
async def cb_delete(c: CallbackQuery):
    DB.execute("UPDATE alerts SET active=0 WHERE id=? AND user_id=?", (int(c.data[2:]), c.from_user.id))
    DB.commit()
    await c.answer(t("deleted", lang_of(c.from_user)))
    await cb_list(c)


@router.callback_query(F.data == "prices")
async def cb_prices(c: CallbackQuery):
    lang = lang_of(c.from_user)
    coins = {cid: sym for _a, cid, sym, *_ in user_alerts(c.from_user.id)}
    if not coins:
        await c.message.edit_text(t("prices_empty", lang), reply_markup=back_kb(lang))
        return
    try:
        q = await PRICES.quotes(list(coins))
    except Exception:
        await c.answer(t("api_error", lang), show_alert=True)
        return
    lines = [f"<b>{sym}</b>  {fmt_price(q[cid]['price'])}  ({q[cid]['chg']:+.1f}%)" for cid, sym in coins.items() if cid in q]
    await c.message.edit_text("💹\n" + "\n".join(lines), reply_markup=kb([[("🔄", "prices"), (t("btn_menu", lang), "menu")]]))


# ---------------------------------------------------------------- background checker
async def checker(bot: Bot):
    while True:
        try:
            alerts = DB.execute("SELECT id, user_id, coin_id, symbol, kind, value, base FROM alerts WHERE active=1").fetchall()
            if alerts:
                q = await PRICES.quotes(sorted({a[2] for a in alerts}), max_age=CHECK_SECONDS / 2)
                for aid, uid, cid, sym, kind, value, base in alerts:
                    if cid not in q:
                        continue
                    price, lang = q[cid]["price"], db_get_lang(uid) or "en"
                    text = None
                    if kind == "above" and price >= value:
                        text = t("hit_above", lang, sym=sym, price=fmt_price(price), lvl=fmt_price(value))
                        DB.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                    elif kind == "below" and price <= value:
                        text = t("hit_below", lang, sym=sym, price=fmt_price(price), lvl=fmt_price(value))
                        DB.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                    elif kind == "move" and base and abs(price - base) / base * 100 >= value:
                        chg = (price - base) / base * 100
                        text = t("hit_move", lang, sym=sym, dir=t("dir_up" if chg > 0 else "dir_down", lang),
                                 chg=f"{chg:+.1f}%", old=fmt_price(base), price=fmt_price(price), pct=f"{value:g}")
                        DB.execute("UPDATE alerts SET base=? WHERE id=?", (price, aid))  # repeating alert
                    if text:
                        DB.commit()
                        try:
                            await bot.send_message(uid, text)
                        except Exception as e:  # user blocked the bot, etc.
                            logging.warning("send to %s failed: %s", uid, e)
                            DB.execute("UPDATE alerts SET active=0 WHERE user_id=?", (uid,))
                            DB.commit()
        except Exception as e:
            logging.warning("price check failed: %s", e)
        await asyncio.sleep(CHECK_SECONDS)


@router.message(F.text)
async def fallback(m: Message, state: FSMContext):
    """Any other text outside a dialog just shows the menu."""
    await cmd_start(m, state)


async def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("123456789:"):
        raise SystemExit("Put your bot token from @BotFather into .env (see .env.example)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(checker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        if PRICES.session:
            await PRICES.session.close()


if __name__ == "__main__":
    asyncio.run(main())
