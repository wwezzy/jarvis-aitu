import os
import time
import json
from upstash_redis import Redis
from dotenv import load_dotenv

load_dotenv()

# Подключаемся к базе
redis = Redis(url=os.getenv("UPSTASH_REDIS_REST_URL"), token=os.getenv("UPSTASH_REDIS_REST_TOKEN"))


def execute_command(command: str):
    if command == "sleep":
        print("Команда получена: Спящий режим")
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    elif command == "shutdown":
        print("Команда получена: Выключение")
        os.system("shutdown /s /t 1")


def listen_for_commands():
    print("Агент запущен. Ожидание команд...")
    while True:
        try:
            # Читаем команду из Redis
            command = redis.get("jarvis_system_command")
            if command:
                execute_command(command)
                # Удаляем команду после выполнения
                redis.delete("jarvis_system_command")

            # Ждем 3 секунды, чтобы не спамить Redis
            time.sleep(3)
        except Exception as e:
            print(f"Ошибка Агента: {e}")
            time.sleep(5)


if __name__ == "__main__":
    listen_for_commands()
