"""
Разовый скрипт для получения токена Яндекс Музыки.

Запусти один раз:
    python get_yandex_token.py

Скрипт покажет ссылку и код — открой ссылку в браузере, войди под своим
аккаунтом Яндекса, введи код. После подтверждения тут же появится токен —
скопируй его в .env как YANDEX_MUSIC_TOKEN.

Токен даёт боту не доступ к твоему личному аккаунту, а просто "пропуск"
для похода в API Яндекс Музыки — с ним можно читать любые ПУБЛИЧНЫЕ
плейлисты кого угодно, а не только твои собственные.
"""

from yandex_music import Client


def on_code(code):
    print()
    print("Открой эту ссылку в браузере:", code.verification_url)
    print("И введи код:", code.user_code)
    print()
    print("Жду подтверждения...")


client = Client()
token = client.device_auth(on_code=on_code)

print()
print("Готово! Вот твой токен — скопируй его в .env как YANDEX_MUSIC_TOKEN:")
print()
print(token.access_token)
