import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


logger = logging.getLogger(__name__)

AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")
AITUNNEL_MODEL = os.getenv("AITUNNEL_MODEL", "gpt-4o-mini")
AITUNNEL_URL = os.getenv("AITUNNEL_URL", "https://api.aitunnel.ru/v1/chat/completions")
ARKHANGELSK_TZ = ZoneInfo("Europe/Moscow")


def _allowed_tags(tags: list[dict]) -> dict[str, list[str]]:
    result = {
        "category": [],
        "audience": [],
        "mood": [],
        "feature": [],
        "age_group": [],
    }
    for tag in tags:
        tag_type = str(tag.get("type", "")).strip()
        value = str(tag.get("value", "")).strip()
        if tag_type in result and value:
            result[tag_type].append(value)
    return result


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def classify_post(post: dict, tags: list[dict]) -> list[dict]:
    if not AITUNNEL_API_KEY:
        raise RuntimeError("Не задан AITUNNEL_API_KEY")

    allowed = _allowed_tags(tags)
    today = datetime.now(ARKHANGELSK_TZ).date().isoformat()
    source_city = str(post.get("source_city") or "").strip()

    system_prompt = f"""
Ты — классификатор и экстрактор публичных мероприятий для городской афиши.

Твоя задача состоит из двух этапов:
1. Определить, содержит ли публикация мероприятие, доступное обычному читателю для посещения или участия.
2. Если содержит — извлечь данные о мероприятии.

КОНТЕКСТ
Текущая дата: {today}.
Источник публикации: {post.get("source_name", "")}.
Город источника: {source_city}.

КРИТЕРИЙ СОБЫТИЯ
Добавляй мероприятие ТОЛЬКО если обычный читатель публикации может стать его посетителем или участником.

Для участия НЕ обязательно наличие регистрации, билета или специальной ссылки.
Открытое мероприятие, на которое можно просто прийти, тоже подходит.

НЕ СЧИТАЙ мероприятием для афиши:
- новости и пресс-релизы о мероприятиях;
- отчёты о прошедших мероприятиях;
- описание мероприятия, которое уже идёт, если читателя не приглашают присоединиться;
- закрытые мероприятия для заранее определённой группы участников;
- рабочие поездки, делегации и официальные программы;
- экскурсии, мастер-классы и другие активности внутри закрытой программы;
- проекты и инициативы будущих мероприятий, которые пока только предлагается реализовать;
- голосования за проекты;
- простые упоминания мероприятий в информационных материалах.

Ключевой вопрос:
«Может ли читатель этой публикации на основании поста принять решение прийти, посетить, зарегистрироваться или иным способом принять участие в конкретном мероприятии?»
Если нет — это не событие для афиши.

Если публикация не содержит подходящих событий, верни {{"events":[]}}.
Если одна публикация содержит несколько самостоятельных доступных мероприятий, верни отдельный объект для каждого.

Для каждого события поля:
title, event_date, time, end_time, venue, address, city, price_min, price_max,
age, age_group, description, category, audience, mood, features.

ДАТА И ВРЕМЯ
- event_date: YYYY-MM-DD. Преобразовывай "сегодня", "завтра", дни недели и русские даты относительно текущей даты. Если дату определить нельзя — пустая строка.
- time/end_time: HH:MM или пустая строка.

ГОРОД
Определяй город в следующем порядке:
1. Если город проведения явно указан в публикации — используй его.
2. Если место проведения позволяет однозначно определить город из текста — используй этот город.
3. Если город из публикации определить нельзя — используй город источника: {source_city}.
Не заменяй явно указанный город городом источника.

ЦЕНА
- price_min/price_max: число в рублях или null.
- Если явно сказано, что участие или вход бесплатные — оба значения 0.
- Не придумывай стоимость.

ВОЗРАСТНОЕ ОГРАНИЧЕНИЕ
- age — только официальное возрастное ограничение (0+, 6+, 12+, 16+, 18+).
- Заполняй age только если оно явно указано в публикации.
- Если не указано — age = "".

ПРЕДПОЛАГАЕМАЯ ВОЗРАСТНАЯ АУДИТОРИЯ
- age_group — массив предполагаемых возрастных групп, которым мероприятие подходит по формату, теме и описанию.
- Определяй age_group даже если официальный возрастной ценз не указан.
- Используй только разрешённые значения и не придумывай точные возрастные диапазоны.

ОПИСАНИЕ
- description: 1–2 коротких предложения, только факты из публикации, без выдуманных деталей и рекламных формулировок.

ТЕГИ
- category: ровно одно разрешённое значение или пустая строка.
- audience, mood, features, age_group: массивы только из разрешённых значений.

Разрешённые category: {json.dumps(allowed["category"], ensure_ascii=False)}
Разрешённые audience: {json.dumps(allowed["audience"], ensure_ascii=False)}
Разрешённые mood: {json.dumps(allowed["mood"], ensure_ascii=False)}
Разрешённые features: {json.dumps(allowed["feature"], ensure_ascii=False)}
Разрешённые age_group: {json.dumps(allowed["age_group"], ensure_ascii=False)}

ОБЩИЕ ПРАВИЛА
Не придумывай отсутствующие фактические данные.
Разрешено интерпретировать содержание публикации только для age_group, audience, mood, category и features.
Не путай упоминание мероприятия с приглашением на мероприятие.
Если есть сомнение, доступно ли мероприятие обычному читателю, не добавляй его в афишу.

Верни ТОЛЬКО валидный JSON без Markdown и пояснений вида {{"events":[...]}}.
""".strip()

    user_prompt = json.dumps(
        {
            "source_name": post.get("source_name"),
            "source_city": source_city,
            "source_url": post.get("source_url"),
            "published_at": post.get("published_at"),
            "text": post.get("text"),
        },
        ensure_ascii=False,
    )

    response = requests.post(
        AITUNNEL_URL,
        headers={
            "Authorization": f"Bearer {AITUNNEL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AITUNNEL_MODEL,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json(content)
    events = parsed.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Разметчик вернул events не массивом")
    return events


def enrich_event(event: dict, post: dict) -> dict:
    image_url = ""
    images = post.get("images") or []
    if images:
        image_url = images[0]

    return {
        **event,
        "source_id": post.get("source_id"),
        "source_url": post.get("source_url", ""),
        "source_item_id": post.get("source_item_id", ""),
        "image_url": image_url,
        "found_at": datetime.now(ARKHANGELSK_TZ).isoformat(),
        "status": "new",
        "sent": False,
    }


def classify_posts(posts: list[dict], tags: list[dict]) -> tuple[list[dict], list[dict]]:
    events = []
    results = []

    for post in posts:
        try:
            found = [enrich_event(event, post) for event in classify_post(post, tags)]
            events.extend(found)
            results.append(
                {
                    "source_item_id": post.get("source_item_id"),
                    "ok": True,
                    "events": len(found),
                }
            )
            logger.info(
                "Classified %s: %s event(s)",
                post.get("source_item_id"),
                len(found),
            )
        except Exception as exc:
            logger.exception("Failed to classify %s", post.get("source_item_id"))
            results.append(
                {
                    "source_item_id": post.get("source_item_id"),
                    "ok": False,
                    "events": 0,
                    "error": str(exc),
                }
            )

    return events, results
