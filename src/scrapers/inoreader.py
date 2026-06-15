"""Inoreader API scraper."""
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import List

import httpx

from ..models import ContentItem, SourceType, InoreaderConfig
from .base import BaseScraper

logger = logging.getLogger(__name__)

CATEGORY_MAP = {
    "polymer science": "polymer-science",
    "polymer": "polymer-science",
    "other": "general-science",
}


class InoreaderScraper(BaseScraper):
    """Scraper for Inoreader API using ClientLogin auth."""

    def __init__(self, config: InoreaderConfig, http_client: httpx.AsyncClient):
        super().__init__({"config": config}, http_client)
        self.cfg = config
        self.auth_token: str | None = None

    async def _authenticate(self) -> bool:
        email = os.environ.get(self.cfg.email_env, "")
        password = os.environ.get(self.cfg.password_env, "")
        if not email or not password:
            logger.error("Inoreader credentials not found in env vars")
            return False
        try:
            resp = await self.client.post(
                "https://www.inoreader.com/accounts/ClientLogin",
                data={"Email": email, "Passwd": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            for line in resp.text.splitlines():
                if line.startswith("Auth="):
                    self.auth_token = line[5:].strip()
                    return True
            logger.error("No Auth token in ClientLogin response")
            return False
        except Exception as e:
            logger.error("Inoreader auth failed: %s", e)
            return False

    async def fetch(self, since: datetime) -> List[ContentItem]:
        if not await self._authenticate():
            return []
        auth_header = {"Authorization": f"GoogleLogin auth={self.auth_token}"}
        items = []
        continuation = None
        fetched = 0
        while fetched < self.cfg.fetch_limit:
            params: dict = {"n": 20, "r": "n"}
            if continuation:
                params["c"] = continuation
            try:
                resp = await self.client.get(
                    "https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/reading-list",
                    headers=auth_header,
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("Inoreader API error: %s", e)
                break
            for entry in data.get("items", []):
                published = entry.get("published")
                if not published:
                    continue
                published_at = datetime.fromtimestamp(published, tz=timezone.utc)
                if published_at < since:
                    continue
                url = ""
                for link in entry.get("canonical") or entry.get("alternate") or []:
                    url = link.get("href", "")
                    if url:
                        break
                entry_id = entry.get("id", "")
                entry_hash = hashlib.sha256(entry_id.encode()).hexdigest()[:16]
                feed_title = (
                    (entry.get("origin") or {}).get("title")
                    or entry.get("source", {}).get("title")
                    or "Inoreader"
                )
                content = ""
                summary = entry.get("summary") or {}
                if isinstance(summary, dict):
                    content = summary.get("content", "")
                if not content:
                    content = entry.get("content", {}).get("content", "")
                categories = entry.get("categories", [])
                horizon_category = self._map_category(categories, feed_title)
                item = ContentItem(
                    id=f"inoreader:{entry_hash}",
                    source_type=SourceType.INOREADER,
                    title=entry.get("title", "Untitled"),
                    url=url or "https://www.inoreader.com",
                    content=content,
                    author=entry.get("author", feed_title),
                    published_at=published_at,
                    metadata={
                        "feed_name": feed_title,
                        "category": horizon_category,
                        "inoreader_categories": categories,
                    },
                )
                items.append(item)
            fetched += len(data.get("items", []))
            continuation = data.get("continuation")
            if not continuation:
                break
        return items

    @staticmethod
    def _map_category(categories: list, feed_title: str) -> str:
        for cat in categories:
            if "/label/" in cat:
                label = cat.split("/label/")[-1].lower()
                for key, val in CATEGORY_MAP.items():
                    if key in label:
                        return val
        ft = feed_title.lower()
        if any(k in ft for k in ("battery", "energy", "joule", "electrochem")):
            return "battery-research"
        if any(k in ft for k in ("polymer", "macromol")):
            return "polymer-science"
        if any(k in ft for k in ("nature", "science", "chem")):
            return "materials-chemistry"
        return "general-science"
