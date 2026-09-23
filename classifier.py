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
VALID_AGE_RATINGS = {"0+", "6+", "12+", "16+", "18+"}


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
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                parsed, _ = decoder.raw_decode(text[match.start():])
                if isinstance(parsed, dict) and "events" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue
        raise


def _request_classification(system_prompt: str, user_prompt, retry_note: str | None = None) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    if retry_note:
        messages.append({"role": "user", "content": retry_note})

    response = requests.post(
        AITUNNEL_URL,
        headers={
            "Authorization": f"Bearer {AITUNNEL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AITUNNEL_MODEL,
            "temperature": 0.1,
            "messages": messages,
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


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

Если публикация не содержит подходящих событий, верни {{"events":[],"need_image":false}}.
Если одна публикация содержит несколько самостоятельных доступных мероприятий, верни отдельный объект для каждого.

КАРТИНКА
Ты сейчас анализируешь только текст публикации.
Верни need_image=true, если у публикации есть изображение И выполняется хотя бы одно:
- по тексту уже найдено событие, но не хватает существенной фактической информации, которая вероятно есть на афише: даты, времени, места/адреса, цены или официального возрастного ограничения;
- текст явно сообщает об афише, расписании, программе, календаре мероприятий или отсылает к изображению за подробностями, даже если из самого текста нельзя извлечь ни одного конкретного события.

ВАЖНО: events может быть пустым при need_image=true. Например, текст «Расписание спектаклей на октябрь, билеты можно приобрести...» без названий и дат означает {"events":[],"need_image":true}, если у поста есть изображение.
Если изображение не нужно для поиска или дополнения событий, need_image=false.

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

Верни ТОЛЬКО валидный JSON без Markdown и пояснений вида {{"events":[...],"need_image":false}}.
""".strip()

    user_prompt = json.dumps(
        {
            "source_name": post.get("source_name"),
            "source_city": source_city,
            "source_url": post.get("source_url"),
            "published_at": post.get("published_at"),
            "text": post.get("text"),
            "has_image": bool(post.get("images")),
        },
        ensure_ascii=False,
    )

    content = _request_classification(system_prompt, user_prompt)
    try:
        parsed = _extract_json(content)
    except json.JSONDecodeError:
        logger.warning(
            "Invalid JSON from classifier for %s. Raw response: %r",
            post.get("source_item_id"),
            content[:1000],
        )
        content = _request_classification(
            system_prompt,
            user_prompt,
            retry_note=(
                "Предыдущий ответ не удалось разобрать как JSON. "
                "Повтори ответ заново. Верни только один валидный JSON-объект "
                'формата {"events":[...]}, без Markdown, комментариев и текста до или после JSON.'
            ),
        )
        try:
            parsed = _extract_json(content)
        except json.JSONDecodeError:
            logger.error(
                "Invalid JSON after retry for %s. Raw response: %r",
                post.get("source_item_id"),
                content[:1000],
            )
            raise

    events = parsed.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Разметчик вернул events не массивом")

    if parsed.get("need_image") is True and post.get("images"):
        try:
            if events:
                events = _complete_events_from_image(events, post)
            else:
                events = _extract_events_from_image(post, allowed)
        except Exception:
            logger.exception(
                "Vision analysis failed for %s; keeping text-only result",
                post.get("source_item_id"),
            )

    return events




def _extract_events_from_image(post: dict, allowed: dict[str, list[str]]) -> list[dict]:
    image_url = (post.get("images") or [None])[0]
    if not image_url:
        return []

    today = datetime.now(ARKHANGELSK_TZ).date().isoformat()
    source_city = str(post.get("source_city") or "").strip()

    prompt = f"""Ты анализируешь изображение с афишей, расписанием или программой мероприятий.
Текст публикации уже показал, что существенная информация о событиях находится на изображении.

Извлеки ВСЕ самостоятельные публичные мероприятия, которые обычный читатель может посетить или на которые может купить билет/зарегистрироваться.
Если на афише несколько спектаклей, концертов, лекций или других событий — верни отдельный объект для каждого.
Не объединяй расписание в одно событие.

Текущая дата: {today}.
Город источника: {source_city}.

Для каждого события верни поля:
title, event_date, time, end_time, venue, address, city, price_min, price_max,
age, age_group, description, category, audience, mood, features.

Правила:
- event_date: YYYY-MM-DD; если год на афише не указан, определи ближайшую будущую дату относительно текущей даты, только если месяц и день указаны однозначно.
- time/end_time: HH:MM или пустая строка.
- city: явно указанный город; иначе город источника.
- price_min/price_max: число в рублях или null; если явно бесплатно — 0.
- age: только явно указанные 0+, 6+, 12+, 16+, 18+; иначе пустая строка.
- description: 1–2 коротких фактических предложения по афише/тексту, без рекламы и выдумок.
- category: ровно одно разрешённое значение или пустая строка.
- audience, mood, features, age_group: массивы только из разрешённых значений.
- Не придумывай отсутствующие даты, время, цены, адреса или возрастные ограничения.
- Если изображение не содержит подходящих публичных событий, верни {"events":[]}.

Разрешённые category: {json.dumps(allowed["category"], ensure_ascii=False)}
Разрешённые audience: {json.dumps(allowed["audience"], ensure_ascii=False)}
Разрешённые mood: {json.dumps(allowed["mood"], ensure_ascii=False)}
Разрешённые features: {json.dumps(allowed["feature"], ensure_ascii=False)}
Разрешённые age_group: {json.dumps(allowed["age_group"], ensure_ascii=False)}

Верни только валидный JSON вида {"events":[...]}, без Markdown и пояснений."""

    content = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "post_text": post.get("text", ""),
                    "source_name": post.get("source_name", ""),
                    "source_city": source_city,
                    "published_at": post.get("published_at"),
                },
                ensure_ascii=False,
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": image_url, "detail": "high"},
        },
    ]

    raw = _request_classification(prompt, content)
    parsed = _extract_json(raw)
    events = parsed.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Vision extraction returned invalid events")
    return events


def _complete_events_from_image(events: list[dict], post: dict) -> list[dict]:
    image_url = (post.get("images") or [None])[0]
    if not image_url:
        return events

    indexed_events = [
        {"event_index": index, **event}
        for index, event in enumerate(events)
    ]

    prompt = """Проанализируй афишу на изображении и ДОПОЛНИ уже найденные события.
Не классифицируй публикацию заново, не создавай новые события, не объединяй и не удаляй существующие.
Каждое событие имеет event_index. Верни только точечные изменения для тех событий,
для которых изображение даёт дополнительную фактическую информацию.

Разрешено дополнять только поля:
title, event_date, time, end_time, venue, address, city, price_min, price_max, age.

Не меняй category, audience, mood, features, age_group и description.
Не придумывай данные. age — только явно указанные 0+, 6+, 12+, 16+, 18+.
Если изображение ничего полезного не добавляет, верни {"updates":[]}.

Формат ответа:
{"updates":[{"event_index":0,"time":"18:00","age":"12+"}]}

Верни только валидный JSON без Markdown и пояснений."""

    content = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "existing_events": indexed_events,
                    "post_text": post.get("text", ""),
                    "published_at": post.get("published_at"),
                },
                ensure_ascii=False,
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": image_url, "detail": "low"},
        },
    ]

    raw = _request_classification(prompt, content)
    parsed = _extract_json(raw)
    updates = parsed.get("updates")

    if not isinstance(updates, list):
        raise ValueError("Vision enrichment returned invalid updates")

    allowed_fields = {
        "title", "event_date", "time", "end_time", "venue", "address",
        "city", "price_min", "price_max", "age",
    }
    result = [dict(event) for event in events]

    for update in updates:
        if not isinstance(update, dict):
            continue

        index = update.get("event_index")
        if not isinstance(index, int) or index < 0 or index >= len(result):
            logger.warning(
                "Vision returned invalid event_index=%r for %s",
                index,
                post.get("source_item_id"),
            )
            continue

        for key in allowed_fields:
            value = update.get(key)
            if value not in (None, "", []):
                result[index][key] = value

    return result

def _normalize_event(event: dict) -> dict:
    normalized = dict(event)

    age = str(normalized.get("age") or "").strip()
    normalized["age"] = age if age in VALID_AGE_RATINGS else ""

    for field in ("age_group", "audience", "mood", "features"):
        value = normalized.get(field)
        if isinstance(value, list):
            normalized[field] = value
        elif not value:
            normalized[field] = []
        else:
            normalized[field] = [str(value).strip()]

    return normalized


def enrich_event(event: dict, post: dict) -> dict:
    event = _normalize_event(event)
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
