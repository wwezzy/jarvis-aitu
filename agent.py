import time
import ctypes
import subprocess
import os
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

# Агент подключается к облаку
r = Redis(url=os.getenv("UPSTASH_REDIS_REST_URL"), token=os.getenv("UPSTASH_REDIS_REST_TOKEN"))

print("🤖 Агент Джарвиса запущен. Жду команд от сервера...")

while True:
    try:
        # Проверяем "почтовый ящик"
        cmd = r.get("pc_command")
        if cmd:
            print(f"⚡ Получена команда: {cmd}")
            r.delete("pc_command")  # Очищаем ящик

            if cmd == "lock":
                ctypes.windll.user32.LockWorkStation()
            elif cmd == "sleep":
                ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
            elif cmd == "shutdown":
                subprocess.run(["shutdown", "/s", "/t", "0"])

    except Exception as e:
        pass  # Игнорируем мелкие обрывы связи

    time.sleep(2)  # Спрашиваем облако каждые 2 секунды