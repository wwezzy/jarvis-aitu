"""An authenticated tap confirms only an existing, user/chat-bound challenge."""
import re

from aiogram import F, Router
from aiogram.types import CallbackQuery

from config import get_settings
from services.pc_agent import handle_pc_request

router = Router(name="pc_confirmation")


@router.callback_query(F.data.startswith("pc:"))
async def pc_confirmation(query: CallbackQuery, pc_redis=None):
    settings = get_settings()
    match = re.fullmatch(r"pc:(confirm|cancel):([a-f0-9]{16})", query.data or "")
    if (query.from_user.id != settings.admin_id or not query.message or not match
            or not callable(getattr(query.message, "edit_text", None))):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    user_id, chat_id = query.from_user.id, query.message.chat.id
    if match[1] == "cancel":
        if pc_redis is None:
            reply = "Канал недоступен. Подтверждение автоматически истечёт через 60 секунд."
        else:
            try:
                revoked = await pc_redis.getdel(f"jarvis:pc_confirm:{user_id}:{chat_id}:{match[2]}")
                reply = ("Подтверждение отменено. Команда не отправлена." if revoked else
                    "Подтверждение уже использовано или истекло. /pc status — проверить результат; "
                    "/pc cancel_shutdown — отменить запланированное выключение.")
            except Exception:
                reply = "Отмену не удалось подтвердить. Подтверждение истечёт через 60 секунд; /pc status — проверить результат."
    else:
        reply = await handle_pc_request(f"/pc confirm {match[2]}", user_id, chat_id,
            pc_redis, settings.pc_agent_secret)
    await query.message.edit_text(reply, reply_markup=None, parse_mode=None)
