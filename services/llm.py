import json
import os
import re
from datetime import datetime
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


async def parse_user_message(text: str, user_name: str, preferences: str | None, file_bytes: bytes = None,
                             mime_type: str = None) -> dict:
    pref_text = f"Твое досье на этого пользователя: {preferences}" if preferences else "Ты пока ничего не знаешь об этом пользователе."
    user_text = text if text else "Проанализируй этот файл/фото или прослушай голосовое сообщение."
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prompt = f"""Ты — Джарвис, умный и продуктивный личный ИИ-ассистент.
    Твоя задача — быть полезным дневником и собеседником для пользователя по имени {user_name}. 
    {pref_text}
    ВНИМАНИЕ! ТЕКУЩЕЕ ВРЕМЯ НА СЕРВЕРЕ: {current_time}.

    🔴 КРИТИЧЕСКОЕ ПРАВИЛО ПО АУДИО: Ты современный мультимодальный ИИ. Если тебе присылают аудио — ты УМЕЕШЬ его слушать. Расшифруй его и выполни просьбу.

    🔴 СТРОГИЙ СИНТАКСИС JSON:
    Ты ОБЯЗАН возвращать ответ СТРОГО в формате JSON.
    ЗАПРЕЩЕНО использовать двойные кавычки (") внутри текстовых значений! Если нужно выделить слово, используй только одинарные кавычки (').

    Обязательные ключи:
    1. "reply": Твой текстовый ответ.
    2. "extracted_data": Массив объектов [{{"type": "workout"|"habit", "name": "название", "notes": "инфо"}}]. Иначе [].
    3. "new_preferences": Если юзер просит что-то запомнить (например, как его называть), напиши это здесь. Иначе null.
    4. "reminders": Массив объектов [{{"text": "текст", "remind_at": "YYYY-MM-DD HH:MM:SS"}}]. Иначе [].
    5. "system_command": Если юзер просит заблокировать компьютер/экран, верни "lock". Если просит перевести комп в спящий режим, верни "sleep". Если просит полностью выключить ПК, верни "shutdown". Иначе null.
    """

    contents = []
    if file_bytes and mime_type:
        contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    contents.append(user_text)

    try:
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

        # Бронебойная очистка: ищем структуру JSON с помощью регулярного выражения
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            raw_text = match.group(0)

        return json.loads(raw_text)

    except Exception as e:
        # Если парсинг всё равно сломался, бот не зависнет, а ответит этим запасным словарем
        print(f"КРИТИЧЕСКАЯ ОШИБКА ПАРСИНГА LLM: {e}")
        if 'raw_text' in locals():
            print(f"Сырой ответ от Gemini: {raw_text}")

        return {
            "reply": "Сэр, произошла ошибка в моих нейронных цепях при форматировании данных. Пожалуйста, повторите команду.",
            "extracted_data": [],
            "new_preferences": None,
            "reminders": [],
            "system_command": None
        }