import json
import os
import re
from datetime import datetime
from google import genai
from google.genai import types
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

# 1. Пул из 5 ключей для обхода лимитов
API_KEYS = [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5")
]
VALID_KEYS = [key for key in API_KEYS if key]

# 2. Подключение к Redis для оперативной памяти
redis = Redis(url=os.getenv("UPSTASH_REDIS_REST_URL"), token=os.getenv("UPSTASH_REDIS_REST_TOKEN"))
HISTORY_KEY = "jarvis_chat_history"


def get_chat_history() -> list:
    """Достает историю переписки из Redis (формат: [{"role": "user"/"model", "text": "..."}])"""
    try:
        history_data = redis.get(HISTORY_KEY)
        return json.loads(history_data) if history_data else []
    except Exception as e:
        print(f"Ошибка чтения памяти из Redis: {e}")
        return []


def save_chat_history(history: list):
    """Сохраняет историю, обрезая старые сообщения (Sliding Window на 50 сообщений)"""
    try:
        trimmed_history = history[-50:] # <--- ТЕПЕРЬ ОН ПОМНИТ 50 СООБЩЕНИЙ
        redis.set(HISTORY_KEY, json.dumps(trimmed_history))
    except Exception as e:
        print(f"Ошибка записи памяти в Redis: {e}")


async def parse_user_message(text: str, user_name: str, preferences: str | None, file_bytes: bytes = None,
                             mime_type: str = None) -> dict:
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_text = text if text else "Проанализируй этот файл/фото или прослушай голосовое сообщение."

    # 3. Вшитая личность и контекст
    prompt = f"""Ты — Джарвис, высокоинтеллектуальный персональный ИИ-ассистент.
    Твой стиль общения: лаконичный, сдержанный, но с долей иронии, в стиле ИИ Тони Старка.
    Используй профессиональный IT-сленг. Твоя цель — помогать пользователю с оптимизацией кода, дисциплиной и тренировками.

    ДОСЬЕ НА ПОЛЬЗОВАТЕЛЯ:
    - Имя: {user_name} (Аллажар)
    - Профиль: Студент 2 курса Software Engineering (Astana IT University).
    - Стек и интересы: Backend-разработка (Python, C++, Java), алгоритмы, кибербезопасность.
    - Спорт: Тяжелые тренировки (жим платформы, болгарские сплит-приседания).
    - Динамические данные из базы: {preferences if preferences else "Дополнительной информации пока нет."}

    ВНИМАНИЕ! ТЕКУЩЕЕ ВРЕМЯ НА СЕРВЕРЕ: {current_time}.

    🔴 КРИТИЧЕСКОЕ ПРАВИЛО ПО АУДИО: Ты современный мультимодальный ИИ. Если тебе присылают аудио — расшифруй его и выполни просьбу.

    🔴 СТРОГИЙ СИНТАКСИС JSON:
    Ты ОБЯЗАН возвращать ответ СТРОГО в формате JSON.
    ЗАПРЕЩЕНО использовать двойные кавычки (") внутри текстовых значений! Если нужно выделить слово, используй только одинарные кавычки (').

    Обязательные ключи:
    1. "reply": Твой текстовый ответ (в образе Джарвиса).
    2. "extracted_data": Массив объектов [{{"type": "workout"|"habit", "name": "название", "notes": "инфо"}}]. Иначе [].
    3. "new_preferences": Если юзер просит что-то запомнить, напиши это здесь. Иначе null.
    4. "reminders": Массив объектов [{{"text": "текст", "remind_at": "YYYY-MM-DD HH:MM:SS"}}]. Иначе [].
    5. "system_command": "lock", "sleep", "shutdown" или null.
    """

    # 4. Сборка контекста из памяти
    raw_history = get_chat_history()
    contents = []

    # Загружаем старые сообщения в формат Gemini
    for msg in raw_history:
        contents.append(types.Content(role=msg["role"], parts=[types.Part.from_text(text=msg["text"])]))

    # Формируем текущий запрос (с файлами, если есть)
    current_parts = []
    if file_bytes and mime_type:
        current_parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    current_parts.append(types.Part.from_text(text=user_text))

    contents.append(types.Content(role="user", parts=current_parts))

    # 5. Маршрутизация по пулу ключей (Fallback)
    for index, api_key in enumerate(VALID_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model='gemini-3.6-flash',
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=prompt,
                    temperature=0.2,
                    response_mime_type="application/json"
                )
            )

            raw_text = response.text.strip()
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                raw_text = match.group(0)

            parsed_json = json.loads(raw_text)

            # 6. Сохраняем успешный диалог в память Redis
            raw_history.append({"role": "user", "text": user_text})
            # Сохраняем только текстовый reply бота, чтобы не забивать контекст системными JSON-данными
            raw_history.append({"role": "model", "text": parsed_json.get("reply", "")})
            save_chat_history(raw_history)

            return parsed_json

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                print(f"Ключ {index + 1} словил лимит 429. Переключаюсь на следующий...")
                continue

            print(f"КРИТИЧЕСКАЯ ОШИБКА ПАРСИНГА LLM (Ключ {index + 1}): {e}")
            return {
                "reply": "Сэр, произошла ошибка в моих вычислительных узлах. Данные повреждены.",
                "extracted_data": [],
                "new_preferences": None,
                "reminders": [],
                "system_command": None
            }

    print("ВЕСЬ ПУЛ ИЗ 5 КЛЮЧЕЙ ИСЧЕРПАН.")
    return {
        "reply": "Сэр, все каналы связи с серверами Google перегружены. Ожидайте сброса лимитов.",
        "extracted_data": [],
        "new_preferences": None,
        "reminders": [],
        "system_command": None
    }