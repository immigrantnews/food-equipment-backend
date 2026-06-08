#!/usr/bin/env python3
import requests
import json
import time

API = "http://localhost:8000"

# Список статей для генерации
ARTICLES = [
    # Хлебопекарное оборудование
    {"slug": "hlebopekarnoe/testomesy", "section": "hlebopekarnoe", "category": "Тестомес", "title": "Тестомесильные машины б/у"},
    {"slug": "hlebopekarnoe/pechi", "section": "hlebopekarnoe", "category": "Печь", "title": "Печи хлебопекарные б/у"},
    {"slug": "hlebopekarnoe/rastoynye-shkafy", "section": "hlebopekarnoe", "category": "Расстойный шкаф", "title": "Расстоечные шкафы б/у"},
    {"slug": "hlebopekarnoe/otcadochnye-mashiny", "section": "hlebopekarnoe", "category": "Отсадочная машина", "title": "Отсадочные машины б/у"},
    {"slug": "hlebopekarnoe/miksery", "section": "hlebopekarnoe", "category": "Миксер", "title": "Миксеры и кремовзбивальные машины б/у"},
    {"slug": "hlebopekarnoe/testodeliteli", "section": "hlebopekarnoe", "category": "Тестоделитель", "title": "Тестоделительные машины б/у"},
    {"slug": "hlebopekarnoe/testookrugliteli", "section": "hlebopekarnoe", "category": "Тестоокруглитель", "title": "Тестоокруглительные машины б/у"},
    {"slug": "hlebopekarnoe/hleborezki", "section": "hlebopekarnoe", "category": "Хлеборезка", "title": "Хлеборезки б/у"},
    # Бренды
    {"slug": "brendy/apach", "section": "brendy", "category": "Apach", "title": "Оборудование Apach — обзор, цены б/у"},
    {"slug": "brendy/unox", "section": "brendy", "category": "Unox", "title": "Оборудование Unox — обзор, цены б/у"},
    {"slug": "brendy/musson-rotor", "section": "brendy", "category": "Муссон-ротор", "title": "Печи Муссон-ротор — обзор, цены б/у"},
    {"slug": "brendy/prismafood", "section": "brendy", "category": "Prismafood", "title": "Оборудование Prismafood — обзор, цены б/у"},
    # Руководства
    {"slug": "guide/kak-kupit-bu", "section": "guide", "category": "Покупка", "title": "Как купить б/у пищевое оборудование"},
    {"slug": "guide/kak-prodat-bu", "section": "guide", "category": "Продажа", "title": "Как продать б/у пищевое оборудование"},
    {"slug": "guide/kak-ocenit-sostoyanie", "section": "guide", "category": "Оценка", "title": "Как оценить состояние б/у оборудования"},
    {"slug": "guide/kak-rasschitat-okupaemost", "section": "guide", "category": "Финансы", "title": "Как рассчитать окупаемость пищевого оборудования"},
]

import os, sys
sys.path.insert(0, '/root/food-equipment-backend')
os.chdir('/root/food-equipment-backend')

from dotenv import load_dotenv
load_dotenv()

import jwt
from datetime import datetime, timedelta, timezone

JWT_SECRET = os.environ.get('JWT_SECRET', 'indmart-secret-key')
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_TELEGRAM_IDS', '714616220').split(',')]

exp = datetime.now(timezone.utc) + timedelta(hours=1)
token = jwt.encode({'user_id': 1, 'telegram_id': ADMIN_IDS[0], 'exp': exp}, JWT_SECRET, algorithm='HS256')

headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}

# Проверяем какие статьи уже есть
existing = set()
try:
    r = requests.get(f"{API}/baza", timeout=10)
    data = r.json()
    for section, articles in data.get('sections', {}).items():
        for a in articles:
            existing.add(a['slug'])
except:
    pass

print(f"Уже есть статей: {len(existing)}")

# Генерируем по одной новой статье
generated = 0
for article in ARTICLES:
    if article['slug'] in existing:
        print(f"Пропускаем (уже есть): {article['slug']}")
        continue
    
    print(f"Генерируем: {article['title']}...")
    try:
        r = requests.post(f"{API}/baza/generate", json=article, headers=headers, timeout=120)
        if r.ok:
            data = r.json()
            print(f"OK: {article['slug']}")
            print(f"Превью: {data.get('preview', '')[:100]}...")
            generated += 1
        else:
            print(f"Ошибка {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"Ошибка: {e}")
    
    time.sleep(5)  # Пауза между запросами
    break  # Генерируем по одной статье за запуск

print(f"Сгенерировано: {generated}")
