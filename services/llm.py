import json
import os
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
    ВНИМАНИЕ! ТЕКУЩЕЕ ВРЕМЯ НА СЕРВЕРЕ: {current_time}. Учитывай это при расчете времени напоминаний.

    ТЫ ОБЯЗАН ВСЕГДА ВОЗВРАЩАТЬ ОТВЕТ СТРОГО В ФОРМАТЕ JSON, содержащем ПЯТЬ ключей:
    1. "reply": Твой текстовый ответ.
    2. "extracted_data": Массив объектов [{{"type": "workout"|"habit", "name": "название", "notes": "инфо"}}]. Иначе [].
    3. "new_preferences": Если из текста можно извлечь новые факты о пользователе, верни обновленный текст. Иначе null.
    4. "reminders": Массив объектов [{{"text": "текст", "remind_at": "YYYY-MM-DD HH:MM:SS"}}]. Иначе [].
    5. "system_command": Если юзер просит заблокировать компьютер/экран, верни "lock". Если просит перевести комп в спящий режим, верни "sleep". Если просит полностью выключить (вырубить) ПК, верни "shutdown". Во всех остальных случаях верни null.
    """

    contents = []
    if file_bytes and mime_type:
        contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    contents.append(user_text)

    response = await client.aio.models.generate_content(
        model='gemini-3.6-flash',
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            temperature=0.2
        )
    )
    return json.loads(response.text)