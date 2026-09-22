import logging
import os
from datetime import date, datetime

from dotenv import load_dotenv
import requests
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.utils import get_random_id
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

from classifier import classify_posts
from collector import collect_posts

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SHEETS_URL = os.getenv("GOOGLE_SHEETS_URL")
SHEETS_API_KEY = os.getenv("GOOGLE_SHEETS_API_KEY")


def send_message(vk, peer_id: int, text: str, keyboard=None) -> None:
    params = {
        "peer_id": peer_id,
        "random_id": get_random_id(),
        "message": text,
    }
    if keyboard is not None:
        params["keyboard"] = keyboard.get_keyboard()
    vk.messages.send(**params)


def make_keyboard(labels, inline=True, buttons_per_row=2):
    keyboard = VkKeyboard(one_time=False, inline=inline)
    for index, label in enumerate(labels):
        if index and index % buttons_per_row == 0:
            keyboard.add_line()
        keyboard.add_button(label, color=VkKeyboardColor.SECONDARY)
    return keyboard


def main_keyboard():
    return make_keyboard(["Подборка", "Куда пойти?"], inline=False)


def sheets_get(action: str) -> dict:
    if not SHEETS_URL or not SHEETS_API_KEY:
        raise RuntimeError("Не настроено подключение к Google Sheets")
    response = requests.get(
        SHEETS_URL,
        params={"action": action, "api_key": SHEETS_API_KEY},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Google Sheets API returned an error"))
    return data


def sheets_post(action: str, **payload) -> dict:
    if not SHEETS_URL or not SHEETS_API_KEY:
        raise RuntimeError("Не настроено подключение к Google Sheets")
    response = requests.post(
        SHEETS_URL,
        json={"api_key": SHEETS_API_KEY, "action": action, **payload},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Google Sheets API returned an error"))
    return data


def get_sources() -> list[dict]:
    return sheets_get("sources").get("sources", [])


def get_tags() -> list[dict]:
    return sheets_get("tags").get("tags", [])


def get_processed() -> list[dict]:
    return sheets_get("processed").get("processed", [])


def get_events() -> list[dict]:
    return sheets_get("events").get("events", [])


def _parse_event_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def upcoming_events(limit=None):
    today = date.today()
    events = []
    for event in get_events():
        event_date = _parse_event_date(event.get("event_date"))
        if not event_date or event_date < today:
            continue
        status = str(event.get("status") or "").strip().lower()
        if status in {"cancelled", "canceled", "отменено", "архив", "archived"}:
            continue
        item = dict(event)
        item["_date"] = event_date
        events.append(item)

    events.sort(key=lambda item: (
        item["_date"],
        not bool(str(item.get("time") or "").strip()),
        str(item.get("time") or "99:99"),
    ))
    return events[:limit] if limit else events


def _split_tags(value):
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    text = str(value or "").strip().lower()
    if not text:
        return set()
    for separator in (";", "|"):
        text = text.replace(separator, ",")
    return {item.strip() for item in text.split(",") if item.strip()}


def format_event(event):
    event_date = event.get("_date") or _parse_event_date(event.get("event_date"))
    date_text = event_date.strftime("%d.%m") if event_date else "Дата уточняется"
    title = str(event.get("title") or "Без названия").strip()
    venue = str(event.get("venue") or "Место уточняется").strip()
    age = str(event.get("age") or "").strip()
    place_line = venue + (f", {age}" if age else "")
    description = str(event.get("description") or "").strip()
    source_url = str(event.get("source_url") or "").strip()

    parts = [f"{date_text} / {title}", place_line]
    if description:
        parts.append(description)
    if source_url:
        parts.append(source_url)
    return "\n".join(parts)


def set_typing(vk, peer_id):
    try:
        vk.messages.setActivity(peer_id=peer_id, type="typing")
    except Exception:
        logger.debug("Failed to set typing activity for peer_id=%s", peer_id, exc_info=True)


def send_event_list(vk, peer_id, events, intro):
    if not events:
        send_message(vk, peer_id, "Пока не нашла подходящих будущих событий.")
        return
    blocks = [format_event(event) for event in events]
    send_message(vk, peer_id, intro + "\n\n" + "\n\n• • •\n\n".join(blocks))


VIBE_QUESTIONS = {
    1: {
        "text": "С кем идёшь?",
        "answers": ["Одна/один", "С парой", "С друзьями", "С семьёй/детьми"],
    },
    2: {
        "text": "Чего хочется?",
        "answers": ["Спокойно и уютно", "Движ и впечатления", "Узнать что-то новое", "Что-нибудь творческое"],
    },
    3: {
        "text": "Что важнее?",
        "answers": ["Не потратиться", "Лучше в помещении", "На свежем воздухе", "Мне всё равно"],
    },
}


def send_vibe_question(vk, peer_id, step):
    question = VIBE_QUESTIONS[step]
    send_message(
        vk,
        peer_id,
        question["text"],
        keyboard=make_keyboard(question["answers"], inline=True, buttons_per_row=1),
    )


def vibe_answer_number(step, text):
    normalized = text.strip().lower()
    for index, label in enumerate(VIBE_QUESTIONS[step]["answers"], start=1):
        if normalized == label.lower():
            return str(index)
    # Keep numeric replies working as a quiet fallback.
    if normalized in {"1", "2", "3", "4"}:
        return normalized
    return None


AUDIENCE_CHOICES = {
    "1": {"соло"},
    "2": {"пары"},
    "3": {"друзья"},
    "4": {"семья", "с детьми"},
}
MOOD_CHOICES = {
    "1": {"уютно", "романтика"},
    "2": {"шумно", "активно"},
    "3": {"интеллектуально"},
    "4": {"творчество"},
}
FEATURE_CHOICES = {
    "1": "free",
    "2": "в помещении",
    "3": "на улице",
    "4": None,
}


def recommend_by_vibe(answers, limit=3):
    audience = AUDIENCE_CHOICES.get(answers.get(1), set())
    moods = MOOD_CHOICES.get(answers.get(2), set())
    feature = FEATURE_CHOICES.get(answers.get(3))
    ranked = []

    for event in upcoming_events():
        score = 0
        event_audience = _split_tags(event.get("audience"))
        event_moods = _split_tags(event.get("mood"))
        event_features = _split_tags(event.get("features"))

        if audience & event_audience:
            score += 3
        if moods & event_moods:
            score += 2

        if feature == "free":
            try:
                if float(event.get("price_min")) == 0:
                    score += 2
            except (TypeError, ValueError):
                pass
        elif feature and feature in event_features:
            score += 2

        if score > 0:
            ranked.append((score, event))

    ranked.sort(key=lambda pair: (
        -pair[0],
        pair[1]["_date"],
        not bool(str(pair[1].get("time") or "").strip()),
        str(pair[1].get("time") or "99:99"),
    ))
    return [event for _, event in ranked[:limit]]


def format_sources(sources: list[dict]) -> str:
    if not sources:
        return "Google Sheets подключён. В Sources пока нет активных источников."
    lines = ["Google Sheets подключён.", "", "Активные источники:"]
    for source in sources:
        lines.append(
            f"{source.get('source_id', '-')}. {source.get('name') or 'Без названия'} · "
            f"{source.get('type') or '-'} · {source.get('city') or '-'}"
        )
    lines.extend(["", f"Всего: {len(sources)}"])
    return "\n".join(lines)


def run_collection(collector_vk):
    posts, results = collect_posts(collector_vk, get_sources(), hours=24)
    for result in results:
        if result["ok"]:
            try:
                sheets_post("update_source_checked", source_id=result["source_id"])
            except Exception:
                logger.exception("Failed to update last_checked_at")
    return posts, results


def update_source_checkpoints(posts, results, ai_results):
    ai_by_id = {
        str(item.get("source_item_id") or "").strip(): item
        for item in ai_results
    }

    for result in results:
        if not result.get("ok") or not result.get("newest_item_id"):
            continue

        source_id = str(result.get("source_id"))
        source_posts = [
            post for post in posts
            if str(post.get("source_id")) == source_id
        ]

        # A checkpoint may advance only if every fetched post for this source
        # was successfully handled. Posts skipped via Processed are already safe.
        failed = [
            post for post in source_posts
            if (
                str(post.get("source_item_id") or "").strip() in ai_by_id
                and not ai_by_id[str(post.get("source_item_id") or "").strip()].get("ok")
            )
        ]
        if failed:
            logger.warning(
                "Checkpoint not advanced for source_id=%s because %s post(s) failed",
                source_id,
                len(failed),
            )
            continue

        try:
            sheets_post(
                "update_source_checkpoint",
                source_id=result["source_id"],
                source_item_id=result["newest_item_id"],
            )
        except Exception:
            logger.exception("Failed to update source checkpoint for %s", source_id)


def process_posts(posts):
    if not posts:
        return [], [], 0, 0, 0

    processed_ids = {
        str(item.get("source_item_id") or "").strip()
        for item in get_processed()
        if item.get("source_item_id")
    }
    new_posts = [
        post for post in posts
        if str(post.get("source_item_id") or "").strip() not in processed_ids
    ]
    skipped = len(posts) - len(new_posts)

    if not new_posts:
        return [], [], 0, skipped, 0

    events, ai_results = classify_posts(new_posts, get_tags())
    saved = 0
    events_by_post = {}
    save_failures_by_post = set()

    for event in events:
        item_id = str(event.get("source_item_id") or "").strip()
        events_by_post[item_id] = events_by_post.get(item_id, 0) + 1
        try:
            sheets_post("add_event", event=event)
            saved += 1
        except Exception:
            save_failures_by_post.add(item_id)
            logger.exception("Failed to save event from post %s", item_id)

    marked_processed = 0
    for result in ai_results:
        if not result.get("ok"):
            continue
        item_id = str(result.get("source_item_id") or "").strip()
        post = next(
            (item for item in new_posts if str(item.get("source_item_id") or "").strip() == item_id),
            None,
        )
        if not post:
            continue

        # Never mark a post as processed when at least one of its events
        # failed to save. It must be retried on the next collection.
        if item_id in save_failures_by_post:
            result["ok"] = False
            result["error"] = "Не удалось сохранить все события поста"
            logger.warning("Post %s left unprocessed because event saving failed", item_id)
            continue

        try:
            sheets_post(
                "mark_processed",
                source_id=post.get("source_id"),
                source_item_id=item_id,
                events_found=events_by_post.get(item_id, 0),
            )
            marked_processed += 1
        except Exception:
            # The post is not safely recorded in Processed, so do not let
            # the source checkpoint move past it.
            result["ok"] = False
            result["error"] = "Не удалось записать пост в Processed"
            logger.exception("Failed to mark post %s as processed", item_id)

    return events, ai_results, saved, skipped, marked_processed


def format_collection_result(posts, results, events, ai_results, saved, skipped=0, marked_processed=0):
    if not results:
        return "Активных VK-источников пока нет."
    lines = ["Сборщик + ИИ отработали.", ""]
    for result in results:
        if result["ok"]:
            lines.append(f"{result['name']}: {result['posts']} постов за 24 ч.")
        else:
            lines.append(f"{result['name']}: {result.get('error') or 'ошибка чтения'}")
    failed_ai = sum(1 for item in ai_results if not item["ok"])
    lines.extend([
        "",
        f"Постов получено: {len(posts)}",
        f"Уже обработано, пропущено: {skipped}",
        f"Новых отправлено в ИИ: {len(posts) - skipped}",
        f"Событий найдено ИИ: {len(events)}",
        f"Записано в Events: {saved}",
    ])
    if failed_ai:
        lines.append(f"Ошибок ИИ-разметки: {failed_ai}")
    return "\n".join(lines)


def main() -> None:
    bot_token = os.getenv("VK_BOT_TOKEN")
    service_token = os.getenv("VK_SERVICE_TOKEN")
    group_id = os.getenv("VK_GROUP_ID")

    if not bot_token:
        raise RuntimeError("Не задан VK_BOT_TOKEN")
    if not service_token:
        raise RuntimeError("Не задан VK_SERVICE_TOKEN")
    if not group_id:
        raise RuntimeError("Не задан VK_GROUP_ID")
    if not SHEETS_URL:
        raise RuntimeError("Не задан GOOGLE_SHEETS_URL")
    if not SHEETS_API_KEY:
        raise RuntimeError("Не задан GOOGLE_SHEETS_API_KEY")
    if not os.getenv("AITUNNEL_API_KEY"):
        raise RuntimeError("Не задан AITUNNEL_API_KEY")

    bot_session = vk_api.VkApi(token=bot_token)
    vk = bot_session.get_api()
    longpoll = VkBotLongPoll(bot_session, int(group_id))

    collector_session = vk_api.VkApi(token=service_token)
    collector_vk = collector_session.get_api()

    logger.info("VK bot, collector and AI classifier started")
    vibe_sessions = {}

    for event in longpoll.listen():
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        message = event.object.message
        text = (message.get("text") or "").strip().lower()
        peer_id = message["peer_id"]

        if text in {"начать", "start", "/start", "привет"}:
            send_message(
                vk,
                peer_id,
                "Привет! Могу показать ближайшие события или подобрать что-нибудь под настроение.",
                keyboard=main_keyboard(),
            )
        elif text in {"подборка", "дайджест", "digest", "/digest"}:
            try:
                set_typing(vk, peer_id)
                send_event_list(
                    vk,
                    peer_id,
                    upcoming_events(limit=10),
                    "Ближайшие события:",
                )
            except Exception:
                logger.exception("Failed to read events from Google Sheets")
                send_message(vk, peer_id, "Не получилось прочитать события.")
        elif text in {"куда пойти", "куда пойти?", "вайб", "подобрать"}:
            vibe_sessions[peer_id] = {"step": 1, "answers": {}}
            send_vibe_question(vk, peer_id, 1)
        elif peer_id in vibe_sessions:
            session = vibe_sessions[peer_id]
            step = session["step"]
            answer = vibe_answer_number(step, text)
            if not answer:
                send_vibe_question(vk, peer_id, step)
                continue
            session["answers"][step] = answer
            if step < 3:
                session["step"] = step + 1
                send_vibe_question(vk, peer_id, step + 1)
            else:
                try:
                    set_typing(vk, peer_id)
                    recommendations = recommend_by_vibe(session["answers"], limit=3)
                    send_event_list(
                        vk,
                        peer_id,
                        recommendations,
                        "Вот что подходит под твой сегодняшний вайб:",
                    )
                    send_message(vk, peer_id, "Хочешь ещё посмотреть?", keyboard=main_keyboard())
                except Exception:
                    logger.exception("Failed to build vibe recommendations")
                    send_message(vk, peer_id, "Не получилось собрать рекомендации.")
                finally:
                    vibe_sessions.pop(peer_id, None)
        elif text in {"собрать", "сбор", "collect", "/collect"}:
            try:
                set_typing(vk, peer_id)
                posts, results = run_collection(collector_vk)
                events, ai_results, saved, skipped, marked_processed = process_posts(posts)
                update_source_checkpoints(posts, results, ai_results)
                send_message(
                    vk,
                    peer_id,
                    format_collection_result(
                        posts, results, events, ai_results, saved, skipped, marked_processed
                    ),
                )
            except Exception:
                logger.exception("Collection pipeline failed")
                send_message(vk, peer_id, "Пайплайн упал. Посмотри логи Timeweb.")
        else:
            send_message(
                vk,
                peer_id,
                "Напиши «подборка» для ближайших событий или «куда пойти» для подбора по вайбу.",
            )


if __name__ == "__main__":
    main()
