"""Plain text splitting respects Telegram's UTF-16 length limit."""
def chunks(text, limit=3900):
    part, units = [], 0
    for char in str(text):
        size = 2 if ord(char) > 0xffff else 1
        if units + size > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += size
    if part:
        yield "".join(part)


async def deliver(message, status, text):
    parts = list(chunks(text)) or ["Пустой ответ."]
    await status.edit_text(parts[0])
    for part in parts[1:]:
        await message.answer(part)
