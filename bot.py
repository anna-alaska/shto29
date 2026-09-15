import logging
import os

from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll


load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def send_message(vk, peer_id: int, text: str) -> None:
    vk.messages.send(
        peer_id=peer_id,
        random_id=0,
        message=text,
    )


def main() -> None:
    token = os.getenv("VK_BOT_TOKEN")
    group_id = os.getenv("VK_GROUP_ID")

    if not token:
        raise RuntimeError("Не задан VK_BOT_TOKEN")
    if not group_id:
        raise RuntimeError("Не задан VK_GROUP_ID")

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
                "Привет! Я буду собирать интересные события в Архангельске 👋\n\n"
                "Напиши «подборка», чтобы проверить, что я работаю.",
            )
        elif text in {"подборка", "дайджест", "digest", "/digest"}:
            send_message(
                vk,
                peer_id,
                "Я живой 👋\n\nСледующий шаг — подключить Google Sheets, а потом сбор анонсов из VK.",
            )
        else:
            send_message(
                vk,
                peer_id,
                "Пока я понимаю команды: «привет» и «подборка».",
            )


if __name__ == "__main__":
    main()
