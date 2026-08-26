import json
import os
import re
from datetime import datetime
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# Собираем все доступные ключи в пул
API_KEYS = [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2")
]
# Очищаем список от пустых значений (если вдруг добавишь только один)
VALID_KEYS = [key for key in API_KEYS if key]


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

    # Проходимся по пулу ключей
    for index, api_key in enumerate(VALID_KEYS):
        try:
            # Инициализируем клиента с текущим ключом из цикла
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

            return json.loads(raw_text)

        except Exception as e:
            err_str = str(e)
            # Если словили лимит, и есть следующий ключ — пробуем его
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                print(f"Ключ {index + 1} словил лимит 429. Переключаюсь на следующий...")
                continue

            # Если ошибка синтаксиса JSON или сбой парсинга
            print(f"КРИТИЧЕСКАЯ ОШИБКА ПАРСИНГА LLM (Ключ {index + 1}): {e}")
            return {
                "reply": "Сэр, произошла ошибка в моих нейронных цепях. Пожалуйста, повторите команду.",
                "extracted_data": [],
                "new_preferences": None,
                "reminders": [],
                "system_command": None
            }

    # Если цикл закончился, значит оба ключа "сгорели" на лимитах
    print("ВЕСЬ ПУЛ КЛЮЧЕЙ ИСЧЕРПАН (429).")
    return {
        "reply": "Сэр, оба моих API-ключа временно заблокированы из-за лимитов Google. Подождите пару минут.",
        "extracted_data": [],
        "new_preferences": None,
        "reminders": [],
        "system_command": None
    }