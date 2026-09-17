import logging
import os

from dotenv import load_dotenv
import requests
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.utils import get_random_id

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


def format_sources(sources: list[dict]) -> str:
    if not sources:
        return "Google Sheets подключён ✅\n\nВ Sources пока нет активных источников."

    lines = ["Google Sheets подключён ✅", "", "Активные источники:"]
    for source in sources:
        lines.append(
            f"{source.get('source_id', '—')}. {source.get('name') or 'Без названия'} · "
            f"{source.get('type') or '—'} · {source.get('city') or '—'}"
        )
    lines.extend(["", f"Всего: {len(sources)}"])
    return "\n".join(lines)


def run_collection(vk) -> tuple[list[dict], list[dict]]:
    sources = get_sources()
    posts, results = collect_posts(vk, sources, hours=24)

    for result in results:
        if result["ok"]:
            try:
                sheets_post("update_source_checked", source_id=result["source_id"])
            except Exception:
                logger.exception(
                    "Failed to update last_checked_at for source_id=%s",
                    result["source_id"],
                )

    return posts, results


def format_collection_result(posts: list[dict], results: list[dict]) -> str:
    if not results:
        return "Активных VK-источников пока нет."

    lines = ["Сборщик отработал 👀", ""]
    for result in results:
        if result["ok"]:
            lines.append(f"✅ {result['name']}: {result['posts']} постов за 24 ч.")
        else:
            lines.append(f"❌ {result['name']}: ошибка чтения")

    lines.extend(["", f"Всего получено постов: {len(posts)}"])
    if posts:
        lines.append("Пока я их не сохраняю — следующим шагом отправим их разметчику.")
    return "\n".join(lines)


def main() -> None:
    token = os.getenv("VK_BOT_TOKEN")
    group_id = os.getenv("VK_GROUP_ID")

    if not token:
        raise RuntimeError("Не задан VK_BOT_TOKEN")
    if not group_id:
        raise RuntimeError("Не задан VK_GROUP_ID")
    if not SHEETS_URL:
        raise RuntimeError("Не задан GOOGLE_SHEETS_URL")
    if not SHEETS_API_KEY:
        raise RuntimeError("Не задан GOOGLE_SHEETS_API_KEY")

    session = vk_api.VkApi(token=token)
    vk = session.get_api()
    longpoll = VkBotLongPoll(session, int(group_id))

    group = vk.groups.getById(group_id=group_id)[0]
    logger.info("VK bot started for group: %s (id=%s)", group.get("name"), group_id)

    for event in longpoll.listen():
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        message = event.object.message
        text = (message.get("text") or "").strip().lower()
        peer_id = message["peer_id"]
        logger.info("Received message from peer_id=%s: %s", peer_id, text)

        if text in {"начать", "start", "/start", "привет"}:
            send_message(
                vk,
                peer_id,
                "Привет! Я собираю интересные события 👋\n\n"
                "«Подборка» — проверить источники.\n"
                "«Собрать» — забрать свежие посты VK за последние 24 часа.",
            )

        elif text in {"подборка", "дайджест", "digest", "/digest"}:
            try:
                send_message(vk, peer_id, format_sources(get_sources()))
            except Exception:
                logger.exception("Failed to read Google Sheets")
                send_message(vk, peer_id, "Не получилось прочитать Google Sheets 😕")

        elif text in {"собрать", "сбор", "collect", "/collect"}:
            try:
                posts, results = run_collection(vk)
                send_message(vk, peer_id, format_collection_result(posts, results))
            except Exception:
                logger.exception("Collection failed")
                send_message(vk, peer_id, "Сборщик упал 😕 Посмотри логи Timeweb.")

        else:
            send_message(
                vk,
                peer_id,
                "Пока я понимаю команды: «привет», «подборка» и «собрать».",
            )


if __name__ == "__main__":
    main()
