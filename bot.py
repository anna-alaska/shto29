import logging
import os

from dotenv import load_dotenv
import requests
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.utils import get_random_id

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


def send_message(vk, peer_id: int, text: str) -> None:
    vk.messages.send(peer_id=peer_id, random_id=get_random_id(), message=text)


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
                "Привет! Команды: «подборка» и «собрать».",
            )
        elif text in {"подборка", "дайджест", "digest", "/digest"}:
            try:
                send_message(vk, peer_id, format_sources(get_sources()))
            except Exception:
                logger.exception("Failed to read Google Sheets")
                send_message(vk, peer_id, "Не получилось прочитать Google Sheets.")
        elif text in {"собрать", "сбор", "collect", "/collect"}:
            try:
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
                "Пока я понимаю команды: «привет», «подборка» и «собрать».",
            )


if __name__ == "__main__":
    main()
