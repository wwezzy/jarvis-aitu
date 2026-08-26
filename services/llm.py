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
    ВНИМАНИЕ! ТЕКУЩЕЕ ВРЕМЯ НА СЕРВЕРЕ: {current_time}.

    🔴 КРИТИЧЕСКОЕ ПРАВИЛО: Ты современный мультимодальный ИИ. Если тебе присылают аудио — ты УМЕЕШЬ его слушать. Расшифруй его и выполни просьбу.

    ТЫ ОБЯЗАН ВСЕГДА ВОЗВРАЩАТЬ ОТВЕТ СТРОГО В ФОРМАТЕ JSON. Без markdown, без текста до/после.
    Ключи: "reply", "extracted_data", "new_preferences", "reminders", "system_command".
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
            temperature=0.2
        )
    )

    # --- Бронебойная очистка от маркдауна ---
    raw_text = response.text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text.replace("```json", "", 1)
    elif raw_text.startswith("```"):
        raw_text = raw_text.replace("```", "", 1)

    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]

    return json.loads(raw_text.strip())