import requests
import time

# Свой User-Agent обязателен по правилам Liquipedia API — впиши что-то своё,
# например с ником в Discord или email, чтобы они видели, кто стучится.
HEADERS = {
    "User-Agent": "MyDiscordBotTest/1.0 (personal use; contact: example@example.com)"
}

API_URL = "https://liquipedia.net/dota2/api.php"
PAGE = "Esports_World_Cup/2026"

params = {
    "action": "parse",
    "page": PAGE,
    "format": "json",
    "prop": "text",
}

print("Делаю запрос к Liquipedia API...")
response = requests.get(API_URL, params=params, headers=HEADERS)
response.raise_for_status()
data = response.json()

html = data["parse"]["text"]["*"]

with open("liquipedia_page.html", "w", encoding="utf-8") as f:
    f.write(html)

print(f"Готово! Сохранено {len(html)} символов в liquipedia_page.html")
print("Открой этот файл и пришли его мне (или хотя бы фрагмент с одним матчем).")
