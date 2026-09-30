"""Public TEGNA archive metadata and timed tracks, without acquiring footage.

Signed delivery locations are used only for this request and never persisted.
Archive descriptions remain distinguishable from spoken audio captions.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.models.search import SearchError

from .captions import ingest_captions, parse_captions
from .discovery import persist_metadata, verify_public_network
from .repository import now

_CAPTION_HOST = "ddk426d1rikbf.cloudfront.net"


class ArchivePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tracks: list[dict] = []
        self.video: dict = {}
        self._json = False
        self._script: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "track":
            self.tracks.append(attributes)
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self._json, self._script = True, []

    def handle_data(self, data):
        if self._json:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._json:
            self._json = False
            try:
                value = json.loads("".join(self._script))
                if isinstance(value, dict) and value.get("@type") == "VideoObject":
                    self.video = value
            except ValueError:
                pass


def caption_allowed(url: str, catalog_id: str, label: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != _CAPTION_HOST or parsed.port not in {None, 443} or parsed.username or parsed.password:
            return False
        pattern = (rf"/transcription_files/{re.escape(catalog_id)}\.vtt" if label == "Audio"
                   else rf"/video_transcription_files/{re.escape(catalog_id)}_\d+\.vtt")
        return re.fullmatch(pattern, parsed.path) is not None
    except ValueError:
        return False


def _read(url: str, settings, maximum: int) -> str:
    import requests

    verify_public_network(url, settings)
    try:
        # The catalog's language redirect is bounded and stays on the exact
        # host and video identity. Caption CDN redirects remain disallowed.
        with requests.get(url, timeout=(15, 45), allow_redirects=False, stream=True) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                destination = urljoin(url, response.headers.get("Location", ""))
                original, redirected = urlsplit(url), urlsplit(destination)
                identity = re.fullmatch(r"/video/detail/(\d+)", original.path)
                if (original.hostname == redirected.hostname == "tegna.kurator.com" and identity
                        and redirected.path == "/en/video/detail/" + identity[1] and not redirected.query):
                    return _read(destination, settings, maximum)
                raise SearchError("The archive returned an unsupported discovery redirect.", 403)
            if response.status_code != 200:
                raise SearchError("The public archive page or caption track is unavailable.", 503)
            body = bytearray()
            for block in response.iter_content(65536):
                body.extend(block)
                if len(body) > maximum:
                    raise SearchError("The public archive response exceeds the discovery size limit.")
            return body.decode("utf-8-sig", errors="replace")
    except requests.RequestException:
        raise SearchError("The public archive request failed.", 503) from None


def ingest_source(service, payload: dict) -> dict:
    source_id = payload["source_id"]
    source = service.repo.get("sources", source_id)
    if not source:
        raise SearchError("Source not found.", 404)
    url = source["canonical_url"]
    parsed = urlsplit(url)
    match = re.fullmatch(r"/video/detail/(\d+)", parsed.path)
    if parsed.hostname != "tegna.kurator.com" or not match:
        raise SearchError("Select a direct public TEGNA archive video page.")
    catalog_id = match[1]
    raw = _read(url, service.settings, 3_000_000)
    page = ArchivePage()
    page.feed(raw)
    if not page.video:
        raise SearchError("This archive page does not contain public video metadata.", 503)
    metadata = {
        "id": catalog_id, "catalog_id": catalog_id,
        "title": page.video.get("name", ""),
        "description": page.video.get("description", ""),
        "creator_name": "TEGNA / KREM", "published_at": page.video.get("uploadDate"),
        "license_claim": "By Request; archive clearance must be reviewed separately",
        "context_note": "Archive upload date may differ from the original broadcast date.",
        "is_video": True,
    }
    # Next.js repeats structured metadata in escaped script strings. Restrict
    # the duration match to its named field rather than a player preview timer.
    unescaped = raw.replace('\\"', '"').replace('\\"', '"')
    duration = re.search(r'"title"\s*:\s*"Duration"\s*,\s*"value"\s*:\s*"(\d{2}):(\d{2}):(\d{2})"', unescaped)
    if duration:
        hours, minutes, seconds = map(int, duration.groups())
        measured = (hours * 3600 + minutes * 60 + seconds) * 1000
        if measured > service.settings.max_source_duration_ms:
            raise SearchError("This source exceeds the configured processing duration limit.")
        if measured:
            metadata["duration_ms"] = measured
    persist_metadata(service.repo, source_id, metadata, "TEGNA public archive", "1")
    indexed, errors = 0, 0
    for track in page.tracks:
        label, location = track.get("label"), track.get("src", "")
        if label not in {"Audio", "Description"} or not caption_allowed(location, catalog_id, label):
            continue
        try:
            text = _read(location, replace(service.settings, allowed_domains=(_CAPTION_HOST,)), 5_000_000)
            kind = "manual" if label == "Audio" else "visual_description"
            if label == "Description" and metadata.get("duration_ms"):
                # Uniform archive frame descriptions sometimes extend the last
                # annotation past the catalog duration. Index its intersection
                # with known media only; never infer a longer source duration.
                cues = parse_captions(text, "vtt")
                duration_ms = metadata["duration_ms"]
                bounded = [{**cue, "end_ms": min(cue["end_ms"], duration_ms)}
                           for cue in cues if cue["start_ms"] < duration_ms]
                if bounded != cues:
                    service.repo.event("archive_description_bounded", source_id=source_id,
                                       payload={"catalog_duration_ms": duration_ms, "original_cue_count": len(cues),
                                                "indexed_cue_count": len(bounded)})
                text = bounded
            ingest_captions(service.repo, source_id, text, track.get("srclang", "en"), "vtt", kind, "TEGNA Archive " + label)
            indexed += 1
        except SearchError:
            errors += 1
    if not indexed:
        with service.repo.connect() as connection:
            connection.execute("UPDATE sources SET state='captions_unavailable',updated_at=? WHERE id=?", (now(), source_id))
    service.repo.event("discovery_complete", source_id=source_id,
                       payload={"adapter": "TEGNA public archive", "caption_tracks_indexed": indexed,
                                "caption_tracks_unavailable": errors, "media_downloaded": False})
    if indexed and service.settings.semantic_enabled:
        service.enqueue_index(source_id)
    return {"source_id": source_id, "state": "text_indexed" if indexed else "captions_unavailable", "media_downloaded": False}
