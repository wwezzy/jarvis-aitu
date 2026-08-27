import time
import os
import ctypes
from upstash_redis import Redis

# Жестко прошитые ключи доступа
UPSTASH_URL = "https://vocal-wren-179571.upstash.io"
UPSTASH_TOKEN = "gQAAAAAAAr1zAAIgcDI5MTk3MTEwOGU4OWI0ZjVjODJmZmM3MzkwMjE4N2E0NA"


def execute_command(cmd):
    """Маршрутизация системных директив"""
    print(f"🔥 ВЫПОЛНЯЮ: {cmd}")
    if cmd == "sleep":
        ctypes.windll.PowrProf.SetSuspendState(0, 1, 0)
    elif cmd == "lock":
        ctypes.windll.user32.LockWorkStation()
    elif cmd == "shutdown":
        os.system("shutdown /s /t 0")
    elif cmd == "restart":
        os.system("shutdown /r /t 0")


def start_agent():
    print("Инициализация протоколов...")

    # Бесконечно ждем подключения к Wi-Fi при запуске Windows
    while True:
        try:
            redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
            print("✅ Связь с ядром установлена. Мониторинг активен...")
            break
        except Exception:
            time.sleep(5)

    # Неубиваемый цикл прослушивания
    while True:
        try:
            cmd = redis.get("pc_command")
            if cmd:
                redis.delete("pc_command")  # Зачищаем базу перед выполнением
                execute_command(cmd)
        except Exception:
            pass  # Игнорируем временные скачки пинга

        time.sleep(2)  # Такт опроса


if __name__ == "__main__":
    start_agent()