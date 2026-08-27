import time
import os
from upstash_redis import Redis

# Твои данные вшиты напрямую
UPSTASH_URL = "https://vocal-wren-179571.upstash.io"
UPSTASH_TOKEN = "gQAAAAAAAr1zAAIgcDI5MTk3MTEwOGU4OWI0ZjVjODJmZmM3MzkwMjE4N2E0NA"

print("Агент запускается...")

try:
    redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
    print("✅ Успешное подключение к Upstash! Слушаю команды...")
except Exception as e:
    print(f"❌ Ошибка подключения: {e}")

while True:
    try:
        cmd = redis.get("pc_command")
        if cmd:
            print(f"🔥 ПОЛУЧЕНА КОМАНДА: {cmd}")
            redis.delete("pc_command")

            if cmd == "sleep":
                os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
            elif cmd == "lock":
                os.system("rundll32.exe user32.dll,LockWorkStation")
    except Exception as e:
        print(f"❌ Ошибка чтения базы: {e}")

    time.sleep(2)