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
    result = {"category": [], "audience": [], "mood": [], "feature": []}
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

    system_prompt = f"""
Ты извлекаешь публичные офлайн/гибридные события из постов для афиши Архангельска.
Сегодня в Архангельске: {today}.

Верни ТОЛЬКО JSON вида {{"events": [...]}}.
Если пост не является анонсом события или из него нельзя понять конкретное будущее событие, верни {{"events":[]}}.
Один пост может содержать несколько событий — тогда верни несколько объектов.

Для каждого события поля:
title, event_date, time, end_time, venue, address, city, price_min, price_max,
age, description, category, audience, mood, features.

Правила:
- event_date: YYYY-MM-DD. Разрешай "сегодня", "завтра", дни недели и русские даты относительно текущей даты.
- time/end_time: HH:MM или пустая строка.
- price_min/price_max: число в рублях или null. Если явно бесплатно — оба 0.
- city: только если можно определить из текста/контекста; для источника Архангельска можно использовать Архангельск.
- description: 1–2 коротких предложения, только факты из поста, ничего не выдумывай.
- category: ровно одно значение из списка или пустая строка.
- audience, mood, features: массивы, только значения из разрешённых списков.
- неизвестные значения оставляй пустыми/null, не додумывай.

Разрешённые category: {json.dumps(allowed["category"], ensure_ascii=False)}
Разрешённые audience: {json.dumps(allowed["audience"], ensure_ascii=False)}
Разрешённые mood: {json.dumps(allowed["mood"], ensure_ascii=False)}
Разрешённые features: {json.dumps(allowed["feature"], ensure_ascii=False)}
""".strip()

    user_prompt = json.dumps(
        {
            "source_name": post.get("source_name"),
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
