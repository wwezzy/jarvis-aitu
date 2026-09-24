import re

from aiogram import F, Router
from aiogram.types import CallbackQuery

from config import get_settings
from services.notifications import notification_feedback
from services.scheduler import sync_assignment_jobs

router = Router(name="notifications")


@router.callback_query(F.data.startswith("notice:"))
async def notice_callback(callback: CallbackQuery, bot, scheduler):
    if not callback.from_user or callback.from_user.id != get_settings().admin_id:
        await callback.answer("Access denied", show_alert=True)
        return
    match = re.fullmatch(r"notice:(done|snooze|reschedule|mute):(\d{1,12})", callback.data or "")
    if not match:
        await callback.answer("Invalid action", show_alert=True)
        return
    try:
        result = await notification_feedback(callback.from_user.id, int(match[2]), match[1])
        await sync_assignment_jobs(scheduler, bot, callback.from_user.id)
        await callback.answer(result[:200], show_alert=True)
    except ValueError:
        await callback.answer("Action is no longer available", show_alert=True)
