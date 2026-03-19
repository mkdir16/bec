import hashlib
import hmac
import json
from urllib.parse import parse_qs, unquote
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")


def verify_telegram_init_data(init_data: str) -> dict | None:
    """
    Проверяет подпись initData от Telegram.
    Возвращает dict с данными юзера или None если подпись неверна.
    """
    try:
        # Парсим строку вида "hash=abc&user=%7B...%7D&auth_date=..."
        parsed = parse_qs(init_data)

        # Извлекаем hash (подпись)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            return None

        # Убираем hash из строки для проверки
        data_check_parts = []
        for key, values in sorted(parsed.items()):
            if key != "hash":
                data_check_parts.append(f"{key}={values[0]}")

        data_check_string = "\n".join(data_check_parts)

        # Считаем HMAC-SHA256
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        # Сравниваем подписи
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        # Достаём данные юзера
        user_json = unquote(parsed.get("user", ["{}"])[0])
        user_data = json.loads(user_json)

        return user_data  # {"id": 123456, "first_name": "Азиз", ...}

    except Exception:
        return None
