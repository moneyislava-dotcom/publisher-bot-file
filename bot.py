import asyncio
import os
import random
import re
from datetime import datetime

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from cryptography.fernet import Fernet
from telethon import TelegramClient, errors
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH") or ""
DB_PATH = "publisher.db"
SESSION_DIR = "sessions"
KEY_FILE = "telegram_secret.key"
MESSAGES_FILE = "messages.txt"

if os.path.exists(KEY_FILE):
    with open(KEY_FILE, "rb") as f:
        FERNET_KEY = f.read()
else:
    FERNET_KEY = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(FERNET_KEY)

fernet = Fernet(FERNET_KEY)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

pending_clients = {}
publishing_lock = asyncio.Lock()
console_login_lock = asyncio.Lock()
session_lock = asyncio.Lock()


def session_busy() -> bool:
    return (
        session_lock.locked()
        or publishing_lock.locked()
        or console_login_lock.locked()
    )


def encrypt_value(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()


def db_connect():
    return aiosqlite.connect(DB_PATH, timeout=30)


class ConnectStates(StatesGroup):
    password = State()


class PublishStates(StatesGroup):
    confirm = State()


class ReplyStates(StatesGroup):
    choose = State()


class SearchStates(StatesGroup):
    query = State()


class AdminStates(StatesGroup):
    delete_manager = State()


async def init_db():
    async with db_connect() as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        await db.execute("PRAGMA journal_mode = WAL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                api_id INTEGER NOT NULL,
                api_hash TEXT NOT NULL,
                phone TEXT,
                username TEXT,
                first_name TEXT,
                session_path TEXT NOT NULL,
                connected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                title TEXT,
                username TEXT,
                can_send INTEGER,
                last_status TEXT,
                last_error TEXT,
                last_attempt TEXT,
                replied INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account_id, telegram_id)
            )
        """)
        try:
            await db.execute(
                "ALTER TABLE groups ADD COLUMN replied INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        await db.execute(
            "UPDATE groups SET can_send = 0 WHERE last_status = 'BLOCKED'"
        )
        await db.commit()


async def get_user(telegram_id):
    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT id, telegram_id, username, first_name, role
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        return await cursor.fetchone()


async def create_user(message, role):
    user = message.from_user
    async with db_connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO users
            (telegram_id, username, first_name, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.username,
                user.first_name,
                role,
                datetime.now().isoformat(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_account(user_id):
    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT id, user_id, api_id, api_hash, phone,
                   username, first_name, session_path, connected
            FROM telegram_accounts
            WHERE user_id = ?
            """,
            (user_id,),
        )
        return await cursor.fetchone()


async def save_account(
    user_id,
    api_id,
    api_hash,
    phone,
    username,
    first_name,
    session_path,
    connected,
):
    encrypted_hash = encrypt_value(api_hash)
    now = datetime.now().isoformat()
    async with db_connect() as db:
        await db.execute(
            """
            INSERT INTO telegram_accounts
            (
                user_id, api_id, api_hash, phone,
                username, first_name, session_path,
                connected, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                api_id = excluded.api_id,
                api_hash = excluded.api_hash,
                phone = excluded.phone,
                username = excluded.username,
                first_name = excluded.first_name,
                session_path = excluded.session_path,
                connected = excluded.connected,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                api_id,
                encrypted_hash,
                phone,
                username,
                first_name,
                session_path,
                connected,
                now,
                now,
            ),
        )
        await db.commit()


async def update_group_status(account_id, telegram_id, status, error=""):
    async with db_connect() as db:
        await db.execute(
            """
            UPDATE groups
            SET last_status = ?,
                last_error = ?,
                last_attempt = ?,
                can_send = CASE WHEN ? = 'BLOCKED' THEN 0 ELSE can_send END
            WHERE account_id = ? AND telegram_id = ?
            """,
            (
                status,
                error[:500] if error else None,
                datetime.now().isoformat(),
                status,
                account_id,
                telegram_id,
            ),
        )
        await db.commit()


def manager_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Подключить Telegram"),
                KeyboardButton(text="Войти заново"),
            ],
            [
                KeyboardButton(text="Обновить группы"),
                KeyboardButton(text="Мои группы"),
            ],
            [
                KeyboardButton(text="Успешные группы"),
                KeyboardButton(text="Моя статистика"),
            ],
            [KeyboardButton(text="Есть ответ")],
            [KeyboardButton(text="Найти чаты")],
            [KeyboardButton(text="Запустить рассылку")],
        ],
        resize_keyboard=True,
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Менеджеры"),
                KeyboardButton(text="Статистика"),
            ],
            [KeyboardButton(text="Удалить менеджера")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )


def confirm_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Да, запускаем"),
                KeyboardButton(text="Отмена"),
            ]
        ],
        resize_keyboard=True,
    )


def personalize_text(text: str, username: str, first_name: str) -> str:
    contact = f"@{username}" if username else (first_name or "мне в личные сообщения")
    return re.sub(r"@jobrossi", contact, text, flags=re.IGNORECASE)


def load_messages():
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            lines = f.read().lstrip("\ufeff").splitlines()
    except FileNotFoundError:
        return []

    messages = []
    current = []
    for line in lines:
        if line.strip().lstrip("\ufeff").upper().startswith("ВАРИАНТ"):
            if current:
                msg = "\n".join(current).strip()
                if msg:
                    messages.append(msg)
            current = []
            continue
        current.append(line)
    if current:
        msg = "\n".join(current).strip()
        if msg:
            messages.append(msg)
    return messages


def is_group_chat(dialog) -> bool:
    entity = dialog.entity
    if getattr(entity, "broadcast", False):
        return False
    if getattr(entity, "megagroup", False):
        return True
    return bool(dialog.is_group)


async def iter_group_dialogs(client):
    seen = set()
    for archived in (None, True):
        async for dialog in client.iter_dialogs(limit=None, archived=archived):
            if dialog.id in seen or not is_group_chat(dialog):
                continue
            seen.add(dialog.id)
            yield dialog


async def drop_pending(telegram_id: int):
    pending = pending_clients.pop(telegram_id, None)
    if not pending:
        return
    task = pending.get("task")
    if task and task is not asyncio.current_task():
        task.cancel()
    client = pending.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    session_path = pending.get("session_path")
    if session_path:
        for suffix in (".session", ".session-journal"):
            path = session_path + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


async def close_client(client):
    if not client:
        return
    try:
        await client.disconnect()
    except Exception:
        pass
    session = getattr(client, "session", None)
    conn = getattr(session, "_conn", None) if session else None
    if conn:
        try:
            conn.close()
        except Exception:
            pass


async def release_session(session_path: str):
    for key, pending in list(pending_clients.items()):
        if pending.get("session_path") != session_path:
            continue
        client = pending.get("client")
        pending_clients.pop(key, None)
        await close_client(client)


async def refresh_groups(account_id: int) -> int:
    async with db_connect() as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        cursor = await db.execute(
            """
            SELECT api_id, api_hash, session_path
            FROM telegram_accounts
            WHERE id = ?
            """,
            (account_id,),
        )
        account = await cursor.fetchone()

    if not account:
        return 0

    api_id, encrypted_hash, session_path = account
    api_hash = decrypt_value(encrypted_hash)
    await release_session(session_path)

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()
    session_conn = getattr(client.session, "_conn", None)
    if session_conn:
        session_conn.execute("PRAGMA busy_timeout = 30000")
    if not await client.is_user_authorized():
        await close_client(client)
        return 0

    rows = []
    async for dialog in iter_group_dialogs(client):
        rows.append(
            (
                account_id,
                dialog.id,
                dialog.name,
                getattr(dialog.entity, "username", None),
                None,
            )
        )

    await close_client(client)

    async with db_connect() as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        await db.executemany(
            """
            INSERT INTO groups
            (account_id, telegram_id, title, username, can_send)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id, telegram_id)
            DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                can_send = CASE
                    WHEN groups.last_status = 'BLOCKED' THEN 0
                    ELSE groups.can_send
                END
            """,
            rows,
        )
        await db.commit()

    return len(rows)


async def search_public_chats(account, query: str, limit: int = 20):
    account_id, session_path = account[0], account[7]
    api_id = account[2]
    api_hash = decrypt_value(account[3])
    await release_session(session_path)

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return None
        result = await client(SearchRequest(q=query, limit=limit))
    finally:
        await close_client(client)

    found = []
    seen = set()
    for chat in result.chats:
        title = (getattr(chat, "title", None) or "").strip()
        username = getattr(chat, "username", None)
        key = username or (title, getattr(chat, "id", None))
        if not title or key in seen:
            continue
        seen.add(key)
        if isinstance(chat, Channel):
            if chat.broadcast or not chat.megagroup:
                continue
            kind = "группа"
        else:
            kind = "чат"
        found.append((kind, title, username))
    path = f"found_chats_{account_id}.txt"
    lines = []
    for kind, title, username in found:
        link = f"https://t.me/{username}" if username else "без ссылки"
        lines.append(f"{kind} | {title} | {link}")
    with open(path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines))
        if lines:
            file.write("\n")
    return found


@dp.message(F.text == "Найти чаты")
async def search_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account or not account[8]:
        await message.answer("Сначала подключите Telegram-аккаунт.")
        return
    if session_busy():
        await message.answer("Подожди, предыдущая команда ещё работает.")
        return
    await state.set_state(SearchStates.query)
    await message.answer(
        "Напиши, что искать. Например: чат Саранск подработка\n"
        "Каналы не показываю: туда нельзя писать. Бот никуда не вступает.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(SearchStates.query, F.text != "Отмена")
async def search_query(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Нужно хотя бы 2 буквы.")
        return
    user = await get_user(message.from_user.id)
    if not user:
        await state.clear()
        return
    account = await get_account(user[0])
    if not account:
        await state.clear()
        await message.answer("Аккаунт не найден.", reply_markup=manager_keyboard())
        return

    await message.answer("Ищу открытые чаты...")
    try:
        async with session_lock:
            found = await search_public_chats(account, query)
    except Exception as error:
        await state.clear()
        await message.answer(
            f"Поиск не удался.\n\n{error}",
            reply_markup=manager_keyboard(),
        )
        return

    await state.clear()
    if found is None:
        await message.answer(
            "Сессия не авторизована. Подключите аккаунт заново.",
            reply_markup=manager_keyboard(),
        )
        return
    if not found:
        await message.answer(
            "Ничего не найдено. По этому слову Telegram отдал только каналы, либо пусто. Добавь слово «чат» или «подработка».",
            reply_markup=manager_keyboard(),
        )
        return

    text = f"НАЙДЕННЫЕ ЧАТЫ\nЗапрос: {query}\nВсего: {len(found)}\nВступать нужно самому.\n\n"
    for index, (kind, title, username) in enumerate(found, 1):
        text += f"{index}. [{kind}] {title}\n"
        if username:
            text += f"   https://t.me/{username}\n"
        else:
            text += "   без ссылки\n"
        if len(text) > 3500:
            await message.answer(text)
            text = ""
    text += f"\nФайл: found_chats_{account[0]}.txt"
    await message.answer(text, reply_markup=manager_keyboard())


async def run_publishing(message: Message, account, delay: int = 10):
    account_id = account[0]
    api_id = account[2]
    encrypted_hash = account[3]
    session_path = account[7]
    api_hash = decrypt_value(encrypted_hash)
    messages_list = load_messages()

    if not messages_list:
        await message.answer("Файл messages.txt пуст или не найден.")
        return

    variant_no = random.randrange(len(messages_list))
    raw_text = messages_list[variant_no]

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT telegram_id, title, can_send
            FROM groups
            WHERE account_id = ?
              AND IFNULL(can_send, 1) != 0
              AND IFNULL(last_status, '') != 'BLOCKED'
            ORDER BY title
            """,
            (account_id,),
        )
        groups = await cursor.fetchall()

    sendable = list(groups)
    if not sendable:
        await message.answer(
            "Нет групп для рассылки. Заблокированные пропускаются. Нажми «Обновить группы»."
        )
        return

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        await message.answer("Сессия не авторизована. Подключите аккаунт заново.")
        return

    me = await client.get_me()
    text = personalize_text(raw_text, me.username or "", me.first_name or "")
    sender = f"@{me.username}" if me.username else (me.first_name or "без username")

    await message.answer(
        f"Рассылка запущена.\n"
        f"Групп: {len(sendable)}\n"
        f"Текст: случайный вариант {variant_no + 1} из {len(messages_list)}\n"
        f"От имени: {sender}\n"
        f"Пауза: {delay} сек."
    )

    sent = blocked = slowmode = flood = errors_count = 0
    for idx, (tg_id, title, _) in enumerate(sendable, 1):
        try:
            await client.send_message(tg_id, text)
            await update_group_status(account_id, tg_id, "SENT")
            sent += 1
            mark = "OK"
        except errors.FloodWaitError as e:
            await update_group_status(account_id, tg_id, "FLOOD_WAIT", f"{e.seconds} sec")
            flood += 1
            await message.answer(
                f"FLOOD_WAIT {e.seconds} сек. Рассылка остановлена на группе: {title}"
            )
            break
        except errors.SlowModeWaitError as e:
            await update_group_status(account_id, tg_id, "SLOWMODE_WAIT", f"{e.seconds} sec")
            slowmode += 1
            mark = "SLOW"
        except Exception as e:
            err = str(e)
            banned = any(
                x in err
                for x in (
                    "USER_BANNED_IN_CHANNEL",
                    "You're banned from sending",
                    "You can't write in this chat",
                    "CHAT_WRITE_FORBIDDEN",
                )
            )
            if banned:
                await update_group_status(account_id, tg_id, "BLOCKED", err)
                blocked += 1
                mark = "BLOCKED"
            else:
                await update_group_status(account_id, tg_id, "ERROR", err)
                errors_count += 1
                mark = "ERROR"

        if idx % 5 == 0 or idx == len(sendable):
            await message.answer(f"[{idx}/{len(sendable)}] {mark} {title}")

        if idx < len(sendable):
            await asyncio.sleep(delay)

    await client.disconnect()
    saved = await export_sent_groups(account_id)
    await message.answer(
        "Рассылка завершена.\n"
        f"Успешно: {sent}\n"
        f"Заблокировано: {blocked}\n"
        f"Slow Mode: {slowmode}\n"
        f"Flood Wait: {flood}\n"
        f"Ошибки: {errors_count}\n"
        f"Список успешных: sent_groups_{account_id}.txt ({saved})",
        reply_markup=manager_keyboard(),
    )


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    existing = await get_user(message.from_user.id)
    if existing:
        if existing[4] == "admin":
            await message.answer(
                "Вы уже зарегистрированы как администратор.",
                reply_markup=admin_keyboard(),
            )
        else:
            await message.answer(
                "Вы уже зарегистрированы как менеджер.",
                reply_markup=manager_keyboard(),
            )
        return

    async with db_connect() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cursor.fetchone())[0]

    if total == 0:
        await create_user(message, "admin")
        await message.answer(
            "Вы стали администратором. Это первый пользователь системы.",
            reply_markup=admin_keyboard(),
        )
    else:
        await create_user(message, "manager")
        await message.answer(
            "Вы зарегистрированы как менеджер. Начните с подключения Telegram-аккаунта.",
            reply_markup=manager_keyboard(),
        )


@dp.message(F.text == "Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await drop_pending(message.from_user.id)
    await state.clear()
    user = await get_user(message.from_user.id)
    keyboard = admin_keyboard() if (user and user[4] == "admin") else manager_keyboard()
    await message.answer("Действие отменено.", reply_markup=keyboard)


async def ask_console(prompt: str) -> str:
    print("\n" + prompt, flush=True)
    return (await asyncio.to_thread(input, "> ")).strip()


def normalize_phone(raw: str) -> str:
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    return ""


def wipe_session(session_path: str):
    for suffix in (".session", ".session-journal"):
        path = session_path + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def console_login(message: Message, telegram_id: int, force: bool = False):
    if console_login_lock.locked():
        await message.answer("В консоли уже идёт другой вход. Дождитесь его окончания.")
        return

    async with console_login_lock:
        async with session_lock:
            os.makedirs(SESSION_DIR, exist_ok=True)
            session_path = os.path.join(SESSION_DIR, f"manager_{telegram_id}")
            client = TelegramClient(session_path, API_ID, API_HASH)
            try:
                await client.connect()
                if force:
                    await close_client(client)
                    wipe_session(session_path)
                    db_user = await get_user(telegram_id)
                    if db_user:
                        async with db_connect() as db:
                            await db.execute(
                                "UPDATE telegram_accounts SET connected = 0 WHERE user_id = ?",
                                (db_user[0],),
                            )
                            await db.commit()
                    client = TelegramClient(session_path, API_ID, API_HASH)
                    await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                else:
                    await close_client(client)
                    wipe_session(session_path)
                    client = TelegramClient(session_path, API_ID, API_HASH)
                    await client.connect()
                    print(f"\n=== Вход менеджера {telegram_id} ===", flush=True)
                    print("Код в чат бота не отправлять. Вводить только в это окно.", flush=True)
                    phone = ""
                    while not phone:
                        entered = await ask_console(
                            f"Менеджер {telegram_id}. Номер, например +79001234567:"
                        )
                        phone = normalize_phone(entered)
                        if not phone:
                            print(
                                "Номер не принят. Нужен формат +79810830864. Введите ещё раз.",
                                flush=True,
                            )
                    try:
                        await client.send_code_request(phone)
                    except errors.FloodWaitError as error:
                        await message.answer(
                            f"Telegram пока не шлёт новый код. Подождите {error.seconds} сек. и нажмите «Подключить Telegram»."
                        )
                        await close_client(client)
                        wipe_session(session_path)
                        return
                    print("Код отправлен в приложение Telegram и, если привязана, на почту.", flush=True)
                    while True:
                        code = await ask_console("Код из официального Telegram:")
                        if not code:
                            print("Код пустой. Введите пять цифр ещё раз.", flush=True)
                            continue
                        try:
                            await client.sign_in(phone=phone, code=code)
                            break
                        except errors.SessionPasswordNeededError:
                            password = await ask_console("Пароль 2FA. Если его нет, просто Enter:")
                            await client.sign_in(password=password)
                            break
                        except errors.PhoneCodeInvalidError:
                            print("Неверный код. Введите его ещё раз. Новый код сам не придёт.", flush=True)
                        except errors.PhoneCodeExpiredError:
                            print("Код истёк. Запрашиваю новый.", flush=True)
                            await client.send_code_request(phone)
                    me = await client.get_me()

                pending_clients[telegram_id] = {
                    "client": client,
                    "session_path": session_path,
                    "api_id": API_ID,
                    "api_hash": API_HASH,
                    "phone": getattr(me, "phone", None) or "",
                }
                await finish_connection(message, None, telegram_id)
            except Exception as error:
                authorized = False
                try:
                    authorized = await client.is_user_authorized()
                except Exception:
                    authorized = False
                try:
                    await close_client(client)
                except Exception:
                    pass
                if not authorized:
                    wipe_session(session_path)
                await message.answer(
                    "Вход не удался. Можно сразу нажать «Подключить Telegram» ещё раз.\n\n"
                    f"{error}"
                )


@dp.message(F.text == "Подключить Telegram")
async def connect_start(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    if not API_ID or not API_HASH:
        await message.answer("В файле .env нет API_ID или API_HASH.")
        return
    if session_busy():
        await message.answer("Подожди, предыдущая команда ещё работает.")
        return

    await message.answer(
        "Вход открыт в чёрном окне.\n"
        "Регистрация менеджера уже есть, номер спрашиваем заново.\n"
        "Номер можно ввести и без плюса: 79810830864 станет +79810830864.\n"
        "Код в этот чат не отправляйте.",
        reply_markup=manager_keyboard(),
    )
    asyncio.create_task(console_login(message, message.from_user.id))


@dp.message(F.text == "Войти заново")
async def reconnect_start(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    if not API_ID or not API_HASH:
        await message.answer("В файле .env нет API_ID или API_HASH.")
        return
    if session_busy():
        await message.answer("Подожди, предыдущая команда ещё работает.")
        return

    await message.answer(
        "Старый вход этого менеджера сброшен.\n"
        "Номер вводи в чёрном окне, не в этот чат.\n"
        "Код тоже только туда.",
        reply_markup=manager_keyboard(),
    )
    asyncio.create_task(console_login(message, message.from_user.id, force=True))


async def finish_connection(message, state, telegram_id):
    pending = pending_clients.get(telegram_id)
    if not pending:
        return

    db_user = await get_user(telegram_id)
    if not db_user:
        await message.answer("Пользователь не найден. Нажмите /start.")
        return

    client = pending["client"]
    me = await client.get_me()
    phone = pending.get("phone") or getattr(me, "phone", None) or ""

    await save_account(
        user_id=db_user[0],
        api_id=pending.get("api_id") or API_ID,
        api_hash=pending.get("api_hash") or API_HASH,
        phone=phone,
        username=me.username,
        first_name=me.first_name,
        session_path=pending["session_path"],
        connected=1,
    )

    session_path = pending["session_path"]
    await close_client(client)
    pending_clients.pop(telegram_id, None)

    account = await get_account(db_user[0])
    groups_count = 0
    if account:
        try:
            groups_count = await refresh_groups(account[0])
        except Exception as error:
            await message.answer(f"Аккаунт сохранён, но группы не собрались: {error}")

    if state:
        await state.clear()

    username_text = f"@{me.username}" if me.username else "без username"
    await message.answer(
        "Telegram-аккаунт подключён.\n\n"
        f"{me.first_name or 'Без имени'}\n"
        f"{username_text}\n"
        f"Групп найдено: {groups_count}",
        reply_markup=manager_keyboard(),
    )


@dp.message(F.text == "Обновить группы")
async def refresh_groups_handler(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account or not account[8]:
        await message.answer("Telegram-аккаунт ещё не подключён.")
        return
    if session_busy():
        await message.answer("Подожди, предыдущая команда ещё работает.")
        return
    await message.answer("Обновляю список групп...")
    try:
        async with session_lock:
            count = await refresh_groups(account[0])
    except Exception as error:
        await message.answer(f"Не удалось обновить группы.\n\n{error}")
        return
    await message.answer(f"Готово. Найдено групп: {count}", reply_markup=manager_keyboard())


@dp.message(F.text == "Мои группы")
async def my_groups(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account or not account[8]:
        await message.answer("Telegram-аккаунт ещё не подключён.")
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT title, username, can_send, last_status, last_attempt, replied
            FROM groups
            WHERE account_id = ?
            ORDER BY title
            """,
            (account[0],),
        )
        groups = await cursor.fetchall()

    if not groups:
        await message.answer("Групп пока нет. Нажми «Обновить группы».")
        return

    text = f"МОИ ГРУППЫ\n\nВсего: {len(groups)}\n\n"
    for index, group in enumerate(groups, 1):
        title, username, can_send, status, attempt, replied = group
        mark = "OK" if can_send == 1 else "NO" if can_send == 0 else "?"
        text += f"{index}. [{mark}] {title}\n"
        if username:
            text += f"   @{username}\n"
        if status:
            text += f"   Статус: {status}\n"
        if replied:
            text += "   Ответ: есть\n"
        if attempt:
            text += f"   Попытка: {attempt[:16]}\n"
        text += "\n"
        if len(text) > 3500:
            await message.answer(text)
            text = ""
    if text:
        await message.answer(text)


async def export_sent_groups(account_id: int) -> int:
    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT title, username, telegram_id, last_attempt
            FROM groups
            WHERE account_id = ? AND last_status = 'SENT'
            ORDER BY title
            """,
            (account_id,),
        )
        rows = await cursor.fetchall()

    lines = []
    for title, username, telegram_id, attempt in rows:
        name = f"@{username}" if username else title
        lines.append(f"{name} | {title} | {telegram_id} | {attempt or ''}")
    path = f"sent_groups_{account_id}.txt"
    with open(path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines))
        if lines:
            file.write("\n")
    return len(lines)


@dp.message(F.text == "Успешные группы")
async def sent_groups(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account:
        await message.answer("Telegram-аккаунт ещё не подключён.")
        return

    count = await export_sent_groups(account[0])
    if count == 0:
        await message.answer("Пока нет групп, куда сообщение ушло успешно.")
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT title, username
            FROM groups
            WHERE account_id = ? AND last_status = 'SENT'
            ORDER BY title
            """,
            (account[0],),
        )
        rows = await cursor.fetchall()

    text = f"УСПЕШНЫЕ ГРУППЫ\n\nВсего: {count}\nФайл: sent_groups_{account[0]}.txt\n\n"
    for index, (title, username) in enumerate(rows, 1):
        text += f"{index}. {title}"
        if username:
            text += f" (@{username})"
        text += "\n"
        if len(text) > 3500:
            await message.answer(text)
            text = ""
    if text:
        await message.answer(text)


@dp.message(F.text == "Моя статистика")
async def my_statistics(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account:
        await message.answer("Telegram-аккаунт ещё не подключён.")
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN last_status = 'SENT' THEN 1 ELSE 0 END),
                SUM(CASE WHEN last_status = 'BLOCKED' THEN 1 ELSE 0 END),
                SUM(CASE WHEN last_status = 'SLOWMODE_WAIT' THEN 1 ELSE 0 END),
                SUM(CASE WHEN last_status = 'FLOOD_WAIT' THEN 1 ELSE 0 END),
                SUM(CASE WHEN last_status = 'ERROR' THEN 1 ELSE 0 END),
                SUM(CASE WHEN replied = 1 THEN 1 ELSE 0 END)
            FROM groups
            WHERE account_id = ?
            """,
            (account[0],),
        )
        stats = await cursor.fetchone()

    total, sent, blocked, slowmode, flood, errors_count, replies = stats
    await message.answer(
        "МОЯ СТАТИСТИКА\n\n"
        f"Групп: {total or 0}\n"
        f"Успешно: {sent or 0}\n"
        f"Есть ответ: {replies or 0}\n"
        f"Заблокировано: {blocked or 0}\n"
        f"Slow Mode: {slowmode or 0}\n"
        f"Flood Wait: {flood or 0}\n"
        f"Ошибки: {errors_count or 0}"
    )


@dp.message(F.text == "Запустить рассылку")
async def start_publish(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account or not account[8]:
        await message.answer("Сначала подключите Telegram-аккаунт.")
        return
    messages_list = load_messages()
    if not messages_list:
        await message.answer("Файл messages.txt пуст или не найден.")
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM groups
            WHERE account_id = ?
              AND IFNULL(can_send, 1) != 0
              AND IFNULL(last_status, '') != 'BLOCKED'
            """,
            (account[0],),
        )
        count = (await cursor.fetchone())[0]

    if count == 0:
        await message.answer("Нет групп для рассылки. Заблокированные уже пропущены.")
        return

    await state.set_state(PublishStates.confirm)
    await message.answer(
        f"Групп: {count}\n"
        f"От имени: {('@' + account[5]) if account[5] else (account[6] or 'без username')}\n"
        f"Текст: случайный из {len(messages_list)}\n"
        f"Пауза: 10 сек.\n\nЗапустить?",
        reply_markup=confirm_keyboard(),
    )


@dp.message(PublishStates.confirm, F.text == "Да, запускаем")
async def confirm_publish(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if not user:
        return
    account = await get_account(user[0])
    if not account:
        await message.answer("Аккаунт не найден.")
        return
    if session_busy():
        await message.answer("Подожди, предыдущая команда ещё работает.")
        return
    async with publishing_lock:
        async with session_lock:
            await run_publishing(message, account, delay=10)


@dp.message(PublishStates.confirm)
async def publish_cancelled(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=manager_keyboard())


@dp.message(F.text == "Есть ответ")
async def reply_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "manager":
        return
    account = await get_account(user[0])
    if not account:
        await message.answer("Telegram-аккаунт ещё не подключён.")
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT id, title
            FROM groups
            WHERE account_id = ? AND last_status = 'SENT' AND IFNULL(replied, 0) = 0
            ORDER BY title
            LIMIT 30
            """,
            (account[0],),
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("Нет успешных групп без отметки ответа.")
        return

    await state.set_state(ReplyStates.choose)
    await state.update_data(reply_ids=[row[0] for row in rows])
    text = "Напиши номер группы, где человек ответил.\n\n"
    for index, (_, title) in enumerate(rows, 1):
        text += f"{index}. {title}\n"
    text += "\nОтмена — кнопка внизу."
    await message.answer(text, reply_markup=cancel_keyboard())


@dp.message(ReplyStates.choose)
async def reply_choose(message: Message, state: FSMContext):
    data = await state.get_data()
    ids = data.get("reply_ids") or []
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(ids)):
        await message.answer(f"Нужен номер от 1 до {len(ids)}.")
        return

    group_id = ids[int(raw) - 1]
    async with db_connect() as db:
        await db.execute(
            "UPDATE groups SET replied = 1 WHERE id = ?",
            (group_id,),
        )
        cursor = await db.execute("SELECT title FROM groups WHERE id = ?", (group_id,))
        row = await cursor.fetchone()
        await db.commit()

    await state.clear()
    title = row[0] if row else "группа"
    await message.answer(
        f"Отмечено: в «{title}» был ответ.",
        reply_markup=manager_keyboard(),
    )


@dp.message(F.text == "Менеджеры")
async def admin_managers(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "admin":
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT u.first_name, u.username, a.username, a.connected, COUNT(g.id)
            FROM users u
            LEFT JOIN telegram_accounts a ON a.user_id = u.id
            LEFT JOIN groups g ON g.account_id = a.id
            WHERE u.role = 'manager'
            GROUP BY u.id, u.first_name, u.username, a.username, a.connected
            ORDER BY u.id
            """
        )
        managers = await cursor.fetchall()

    if not managers:
        await message.answer("Менеджеров пока нет.")
        return

    text = "МЕНЕДЖЕРЫ\n\n"
    for index, row in enumerate(managers, 1):
        first_name, user_name, tg_username, connected, groups_count = row
        text += (
            f"{index}. {first_name or 'Без имени'} "
            f"({('@' + user_name) if user_name else 'без username'})\n"
            f"   Аккаунт: {('@' + tg_username) if tg_username else 'не подключён'}\n"
            f"   {'подключён' if connected else 'не подключён'}\n"
            f"   Групп: {groups_count}\n\n"
        )
    await message.answer(text)


@dp.message(F.text == "Удалить менеджера")
async def admin_delete_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "admin":
        return

    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT u.id, u.telegram_id, u.first_name, u.username
            FROM users u
            WHERE u.role = 'manager'
            ORDER BY u.id
            """
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("Менеджеров нет.")
        return

    await state.set_state(AdminStates.delete_manager)
    await state.update_data(delete_ids=[row[0] for row in rows])
    text = "Напиши номер менеджера, которого удалить.\n\n"
    for index, (_, _, first_name, username) in enumerate(rows, 1):
        label = f"@{username}" if username else "без username"
        text += f"{index}. {first_name or 'Без имени'} ({label})\n"
    text += "\nОтмена — кнопка внизу."
    await message.answer(text, reply_markup=cancel_keyboard())


@dp.message(AdminStates.delete_manager, F.text != "Отмена")
async def admin_delete_choose(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "admin":
        await state.clear()
        return

    data = await state.get_data()
    ids = data.get("delete_ids") or []
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(ids)):
        await message.answer(f"Нужен номер от 1 до {len(ids)}.")
        return

    user_id = ids[int(raw) - 1]
    async with db_connect() as db:
        cursor = await db.execute(
            """
            SELECT u.first_name, u.telegram_id, a.session_path
            FROM users u
            LEFT JOIN telegram_accounts a ON a.user_id = u.id
            WHERE u.id = ? AND u.role = 'manager'
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            await state.clear()
            await message.answer("Менеджер уже удалён.", reply_markup=admin_keyboard())
            return
        first_name, telegram_id, session_path = row
        await db.execute(
            """
            DELETE FROM groups
            WHERE account_id IN (
                SELECT id FROM telegram_accounts WHERE user_id = ?
            )
            """,
            (user_id,),
        )
        await db.execute("DELETE FROM telegram_accounts WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM users WHERE id = ? AND role = 'manager'", (user_id,))
        await db.commit()

    if session_path:
        wipe_session(session_path)
    if telegram_id:
        await drop_pending(telegram_id)

    await state.clear()
    await message.answer(
        f"Менеджер «{first_name or 'Без имени'}» удалён.",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "Статистика")
async def admin_statistics(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user[4] != "admin":
        return

    async with db_connect() as db:
        async def one(sql):
            cursor = await db.execute(sql)
            return (await cursor.fetchone())[0]

        managers = await one("SELECT COUNT(*) FROM users WHERE role = 'manager'")
        accounts = await one("SELECT COUNT(*) FROM telegram_accounts WHERE connected = 1")
        groups = await one("SELECT COUNT(*) FROM groups")
        sent = await one("SELECT COUNT(*) FROM groups WHERE last_status = 'SENT'")
        replies = await one("SELECT COUNT(*) FROM groups WHERE replied = 1")
        blocked = await one("SELECT COUNT(*) FROM groups WHERE last_status = 'BLOCKED'")
        slowmode = await one("SELECT COUNT(*) FROM groups WHERE last_status = 'SLOWMODE_WAIT'")

    await message.answer(
        "ОБЩАЯ СТАТИСТИКА\n\n"
        f"Менеджеров: {managers}\n"
        f"Подключённых аккаунтов: {accounts}\n"
        f"Групп: {groups}\n"
        f"Успешно: {sent}\n"
        f"Есть ответ: {replies}\n"
        f"Заблокировано: {blocked}\n"
        f"Slow Mode: {slowmode}"
    )


async def main():
    os.makedirs(SESSION_DIR, exist_ok=True)
    await init_db()
    print("Админ-бот запущен")
    print("База:", DB_PATH)
    print("Сессии:", SESSION_DIR)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
