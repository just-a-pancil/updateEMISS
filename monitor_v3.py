import requests
import json
import time
import hashlib
import random
import re
import os
import html
import threading
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


# ─── Настройки ────────────────────────────────────────────────

TELEGRAM_PROXIES=None
# Раскомментить, если используется прокси через зарубежный сервер
# TELEGRAM_PROXIES = {
#     "http":  "socks5h://localhost:1080",
#     "https": "socks5h://localhost:1080",
# }


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


# Список получателей обновлений данных
TELEGRAM_CHAT_IDS = [
    chat_id.strip()
    for chat_id in os.environ["TELEGRAM_CHAT_IDS"].split(",")
    if chat_id.strip()
]

# Получатель технических ошибок
ALERT_CHAT_ID = os.environ["ALERT_CHAT_ID"]

INDICATORS = {
    63005: "Алкоголь: потребление",
    62309: "Алкоголь: розничные продажи",
    62852: "СКР Я1",
    62925: "Охват граждан репродуктивного возраста (18–49 лет) диспансеризацией",
    62853: "СКР 3",
    62924: "Доля беременных женщин, обратившихся в медицинские организации в ситуации репродуктивного выбора",
    41684: "Число принятых родов с 22 недель беременности",
    41696: "Число прерываний беременности МИНЗДРАВ",
    31595: "Число прерываний беременности РОССТАТ",
}

WATCH_LIST = list(INDICATORS.keys())

CHECK_INTERVAL        = 600
CHECK_INTERVAL_JITTER = 120

STATE_FILE = "emiss_state.json"

ERROR_ALERT_COOLDOWN = 3600

# ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

_last_error_alert: dict = {}


# ─── Telegram ─────────────────────────────────────────────────

def telegram_api(method: str, payload: dict, timeout: int = 10):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    resp = requests.post(
        url,
        json=payload,
        timeout=timeout,
        proxies=TELEGRAM_PROXIES,
    )
    resp.raise_for_status()

    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data)

    return data.get("result")


def send_telegram(message: str, chat_ids: list = None):
    """Отправляет сообщение списку chat_id."""
    if chat_ids is None:
        chat_ids = TELEGRAM_CHAT_IDS

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            telegram_api("sendMessage", payload, timeout=10)
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Telegram ошибка (chat_id={chat_id}): {e}")


def send_telegram_to_chat(chat_id: str, message: str):
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    telegram_api("sendMessage", payload, timeout=10)


def build_start_message() -> str:
    lines = [
        "👋 <b>Мониторинг ЕМИСС</b>",
        "",
        "Отслеживаемые показатели:",
        "",
    ]

    for ind_id in WATCH_LIST:
        title = html.escape(INDICATORS.get(ind_id, f"Показатель {ind_id}"))
        url = f"https://www.fedstat.ru/indicator/{ind_id}"

        lines.append(
            f"• <a href=\"{url}\">{title}</a> "
            f"— <code>{ind_id}</code>"
        )

    return "\n".join(lines)


def handle_telegram_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = str(chat.get("id"))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    if re.match(r"^/start(?:@\w+)?(?:\s|$)", text):
        try:
            send_telegram_to_chat(chat_id, build_start_message())
            print(f"[{datetime.now()}] Ответили на /start пользователю {chat_id}")
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Ошибка ответа на /start ({chat_id}): {e}")


def telegram_command_loop():
    """
    Отдельный поток для обработки команд Telegram.
    На /start присылает список индикаторов со ссылками на ЕМИСС.
    """
    print(f"[{datetime.now()}] Telegram command loop запущен.")

    offset = None

    # При старте сбрасываем старые непрочитанные апдейты,
    # чтобы бот не отвечал на древние /start после перезапуска.
    try:
        old_updates = telegram_api(
            "getUpdates",
            {
                "timeout": 1,
                "allowed_updates": ["message", "edited_message"],
            },
            timeout=5,
        )

        if old_updates:
            offset = max(u["update_id"] for u in old_updates) + 1

    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Не удалось сбросить старые updates: {e}")

    while True:
        try:
            payload = {
                "timeout": 30,
                "allowed_updates": ["message", "edited_message"],
            }

            if offset is not None:
                payload["offset"] = offset

            updates = telegram_api(
                "getUpdates",
                payload,
                timeout=40,
            )

            for update in updates:
                offset = update["update_id"] + 1
                handle_telegram_update(update)

        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Ошибка Telegram command loop: {e}")
            time.sleep(5)


# ─── ЕМИСС ────────────────────────────────────────────────────

def send_error_alert(indicator_id: int, error: Exception):
    """Отправляет алерт об ошибке доступа к показателю с cooldown-защитой."""
    now_ts = time.time()
    last_sent = _last_error_alert.get(indicator_id, 0)

    if now_ts - last_sent < ERROR_ALERT_COOLDOWN:
        return

    _last_error_alert[indicator_id] = now_ts

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"🔴 <b>Ошибка доступа к показателю ЕМИСС</b>\n"
        f"📊 Показатель: <code>{indicator_id}</code>\n"
        f"🔗 <a href='https://www.fedstat.ru/indicator/{indicator_id}'>fedstat.ru/indicator/{indicator_id}</a>\n"
        f"💬 Ошибка: <code>{html.escape(str(error)[:300])}</code>\n"
        f"🕐 Время: {now_str}"
    )

    print(f"[{datetime.now()}] 🔴 Алерт об ошибке для {indicator_id} → {ALERT_CHAT_ID}")
    send_telegram(msg, chat_ids=[ALERT_CHAT_ID])


def get_indicator_data(indicator_id: int):
    url = f"https://fedstat.ru/indicator/{indicator_id}.do"

    try:
        response = SESSION.get(
            url,
            params={"format": "sdmx"},
            timeout=(10, 120),
        )
        response.raise_for_status()
        return response.text, None

    except requests.RequestException as e:
        print(f"[{datetime.now()}] Ошибка запроса для {indicator_id}: {e}")
        return None, e


def get_stable_hash(xml_text: str) -> str:
    """Хешируем только данные, убирая динамические поля заголовка."""
    cleaned = re.sub(r"<Prepared>.*?</Prepared>", "", xml_text)
    cleaned = re.sub(r"<Extracted>.*?</Extracted>", "", cleaned)
    cleaned = re.sub(r"<ID>.*?</ID>", "", cleaned)

    return hashlib.md5(cleaned.encode("utf-8")).hexdigest()


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def monitor(indicator_ids: list):
    print(f"[{datetime.now()}] Запуск мониторинга ЕМИСС...")

    # Стартовое сообщение в Telegram убрано.
    # Бот больше не пишет "Мониторинг ЕМИСС запущен".

    state = load_state()

    while True:
        for ind_id in indicator_ids:
            data, error = get_indicator_data(ind_id)

            if data is None:
                send_error_alert(ind_id, error)
                time.sleep(random.uniform(3, 8))
                continue

            current_hash = get_stable_hash(data)
            prev_hash = state.get(str(ind_id))

            if prev_hash is None:
                print(f"[{datetime.now()}] Показатель {ind_id}: начальное состояние зафиксировано.")

            elif current_hash != prev_hash:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                title = html.escape(INDICATORS.get(ind_id, f"Показатель {ind_id}"))

                msg = (
                    f"⚡️ <b>Обновление данных ЕМИСС!</b>\n"
                    f"📊 Показатель: <code>{ind_id}</code>\n"
                    f"📝 {title}\n"
                    f"🔗 <a href='https://www.fedstat.ru/indicator/{ind_id}'>Открыть на fedstat.ru</a>\n"
                    f"🕐 Время: {now}\n"
                    f"/load@checkgsheet_bot"
                )

                print(f"[{datetime.now()}] ⚡️ ОБНОВЛЕНИЕ! Показатель {ind_id} изменился!")
                send_telegram(msg)

            else:
                print(f"[{datetime.now()}] Показатель {ind_id}: изменений нет.")

            state[str(ind_id)] = current_hash
            time.sleep(random.uniform(3, 8))

        save_state(state)

        sleep_time = CHECK_INTERVAL + random.randint(
            -CHECK_INTERVAL_JITTER,
            CHECK_INTERVAL_JITTER,
        )

        print(
            f"[{datetime.now()}] Следующая проверка через "
            f"{sleep_time // 60} мин {sleep_time % 60} сек."
        )

        time.sleep(sleep_time)


if __name__ == "__main__":
    command_thread = threading.Thread(
        target=telegram_command_loop,
        daemon=True,
    )
    command_thread.start()

    monitor(WATCH_LIST)