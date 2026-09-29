"""Данные YouTube: YouTube Data API v3 (если есть ключ) или публичные RSS-ленты каналов (без ключа)."""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET

import httpx

from ..config import secret

API = "https://www.googleapis.com/youtube/v3"


class YouTube:
    def __init__(self):
        self.key = secret("YOUTUBE_API_KEY")
        self.http = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 ISTORIK-Factory"})

    @property
    def has_api(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, **params) -> dict:
        params["key"] = self.key
        r = self.http.get(f"{API}/{path}", params=params)
        r.raise_for_status()
        return r.json()

    def resolve_channel(self, ref: str) -> str | None:
        """@handle / URL / UC-id → channel_id."""
        ref = ref.strip()
        if re.fullmatch(r"UC[\w-]{22}", ref):
            return ref
        handle = ref.split("/")[-1]
        if self.has_api and handle.startswith("@"):
            j = self._get("channels", part="id", forHandle=handle)
            if j.get("items"):
                return j["items"][0]["id"]
        try:  # без ключа: достаём channelId со страницы канала
            url = ref if ref.startswith("http") else f"https://www.youtube.com/{handle}"
            html = self.http.get(url).text
            m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', html)
            return m.group(1) if m else None
        except httpx.HTTPError:
            return None

    def channel_videos(self, channel_id: str, limit: int = 30) -> list[dict]:
        if self.has_api:
            ch = self._get("channels", part="contentDetails,snippet", id=channel_id)
            if not ch.get("items"):
                return []
            uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            title = ch["items"][0]["snippet"]["title"]
            pl = self._get("playlistItems", part="snippet,contentDetails", playlistId=uploads, maxResults=min(50, limit))
            ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
            return self._with_stats(ids, channel=title)
        return self._rss(channel_id)[:limit]

    def _rss(self, channel_id: str) -> list[dict]:
        try:
            r = self.http.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id})
            r.raise_for_status()
        except httpx.HTTPError:
            return []
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
              "media": "http://search.yahoo.com/mrss/"}
        root = ET.fromstring(r.content)
        author = root.findtext("a:title", default="", namespaces=ns)
        out = []
        for e in root.findall("a:entry", ns):
            stats = e.find("media:group/media:community/media:statistics", ns)
            out.append({"id": e.findtext("yt:videoId", namespaces=ns), "title": e.findtext("a:title", namespaces=ns),
                        "published": e.findtext("a:published", namespaces=ns), "channel": author,
                        "views": int(stats.get("views", 0)) if stats is not None else None})
        return out

    def search_recent(self, query: str, days: int = 45, limit: int = 15) -> list[dict]:
        if not self.has_api:
            return []
        after = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        j = self._get("search", part="snippet", q=query, type="video", order="viewCount", publishedAfter=after,
                      relevanceLanguage="ru", maxResults=min(50, limit))
        ids = [i["id"]["videoId"] for i in j.get("items", [])]
        return self._with_stats(ids)

    def _with_stats(self, ids: list[str], channel: str | None = None) -> list[dict]:
        if not ids:
            return []
        j = self._get("videos", part="snippet,statistics", id=",".join(ids[:50]))
        out = []
        for v in j.get("items", []):
            out.append({"id": v["id"], "title": v["snippet"]["title"], "channel": channel or v["snippet"]["channelTitle"],
                        "channel_id": v["snippet"]["channelId"], "published": v["snippet"]["publishedAt"],
                        "views": int(v.get("statistics", {}).get("viewCount", 0)),
                        "url": f"https://www.youtube.com/watch?v={v['id']}"})
        return out
