import os
import sys
import subprocess

import pystray
from PIL import Image, ImageDraw

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(SCRIPT_DIR, "bot.py")

# Тот же pythonw.exe, что и в start_bot.vbs — версия Python без окна консоли.
# Строим путь через переменную окружения, а не хардкодим имя пользователя.
PYTHONW_PATH = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Python", "bin", "pythonw.exe")


LOG_FILE = os.path.join(SCRIPT_DIR, "bot_log.txt")


def make_icon_image():
    """Рисуем простую иконку прямо в коде — не нужен отдельный файл картинки."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(88, 101, 242, 255))  # цвет Discord blurple
    draw.text((18, 16), "B", fill=(255, 255, 255, 255))
    return image


def start_bot_process():
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # Раз запуск идёт без окна консоли, весь print() из bot.py и cogs
    # улетал бы в никуда — перенаправляем его в файл bot_log.txt рядом с ботом,
    # чтобы можно было заглянуть туда при любых проблемах.
    log_file = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    return subprocess.Popen(
        [PYTHONW_PATH, BOT_SCRIPT],
        cwd=SCRIPT_DIR,
        creationflags=creationflags,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def main():
    bot_process = start_bot_process()

    def on_quit(icon, item):
        bot_process.terminate()
        icon.stop()

    def on_status(icon, item):
        pass  # просто пункт-заглушка с информацией, ничего не делает при клике

    def on_open_logs(icon, item):
        if os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)  # откроет в блокноте (или связанной программе)

    running_label = "Статус: бот запущен ✅"
    menu = pystray.Menu(
        pystray.MenuItem(running_label, on_status, enabled=False),
        pystray.MenuItem("Открыть логи", on_open_logs),
        pystray.MenuItem("Остановить бота", on_quit),
    )

    icon = pystray.Icon("discord_bot_tray", make_icon_image(), "Воздухан (Discord-бот)", menu)
    icon.run()


if __name__ == "__main__":
    main()
