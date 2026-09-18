import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)


def _source_domain(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    path = parsed.path.strip("/")
    if not path:
        raise ValueError(f"Не удалось определить VK-сообщество из URL: {url}")

    domain = path.split("/")[0]
    if domain.startswith("club") and domain[4:].isdigit():
        return f"-{domain[4:]}"
    if domain.startswith("public") and domain[6:].isdigit():
        return f"-{domain[6:]}"
    return domain


def _best_photo_url(attachment: dict) -> str | None:
    if attachment.get("type") != "photo":
        return None
    sizes = attachment.get("photo", {}).get("sizes", [])
    if not sizes:
        return None
    best = max(sizes, key=lambda item: item.get("width", 0) * item.get("height", 0))
    return best.get("url")


def collect_vk_posts(vk, source: dict, hours: int = 24, count: int = 50) -> tuple[list[dict], str | None, bool]:
    domain = _source_domain(source.get("url", ""))
    checkpoint = str(source.get("last_processed_item_id") or "").strip()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    posts = []
    newest_item_id = None
    reached_checkpoint = False
    offset = 0

    while True:
        response = vk.wall.get(domain=domain, count=count, offset=offset, filter="owner")
        items = response.get("items", [])
        if not items:
            break

        stop = False
        for item in items:
            owner_id = item.get("owner_id")
            post_id = item.get("id")
            item_id = f"wall{owner_id}_{post_id}"

            if newest_item_id is None:
                newest_item_id = item_id

            if checkpoint and item_id == checkpoint:
                reached_checkpoint = True
                stop = True
                break

            published_at = datetime.fromtimestamp(item["date"], tz=timezone.utc)
            # Before the first checkpoint exists, keep the old 24-hour bootstrap window.
            if not checkpoint and published_at < cutoff:
                stop = True
                break

            images = [
                image
                for image in (_best_photo_url(a) for a in item.get("attachments", []))
                if image
            ]
            posts.append(
                {
                    "source_id": source.get("source_id"),
                    "source_name": source.get("name", ""),
                    "source_city": source.get("city", ""),
                    "source_type": "vk",
                    "source_item_id": item_id,
                    "source_url": f"https://vk.com/{item_id}",
                    "published_at": published_at.isoformat(),
                    "text": item.get("text", "").strip(),
                    "images": images,
                }
            )

        if stop or len(items) < count:
            break
        offset += len(items)

    logger.info(
        "Collected %s new VK posts from source_id=%s (%s), checkpoint=%s reached=%s",
        len(posts),
        source.get("source_id"),
        source.get("name"),
        checkpoint or "-",
        reached_checkpoint,
    )
    return posts, newest_item_id, reached_checkpoint


def collect_posts(vk, sources: list[dict], hours: int = 24) -> tuple[list[dict], list[dict]]:
    posts = []
    results = []

    for source in sources:
        if str(source.get("type", "")).strip().lower() != "vk":
            continue

        try:
            source_posts, newest_item_id, reached_checkpoint = collect_vk_posts(
                vk, source, hours=hours
            )
            posts.extend(source_posts)
            results.append(
                {
                    "source_id": source.get("source_id"),
                    "name": source.get("name"),
                    "ok": True,
                    "posts": len(source_posts),
                    "newest_item_id": newest_item_id,
                    "reached_checkpoint": reached_checkpoint,
                }
            )
        except Exception as exc:
            logger.exception("Failed to collect source_id=%s", source.get("source_id"))
            results.append(
                {
                    "source_id": source.get("source_id"),
                    "name": source.get("name"),
                    "ok": False,
                    "posts": 0,
                    "error": str(exc),
                }
            )

    return posts, results
