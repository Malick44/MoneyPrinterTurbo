"""Caption-first discovery from selected, allowlisted video sources."""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import itertools
import math
import re
import socket
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from app.models.search import SearchError
from .captions import ingest_captions
from .repository import json_text, new_id, now


def _domain_allowed(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def canonicalize_url(
    url: str, settings, registration_only: bool = False
) -> tuple[str, str, str]:
    if not isinstance(url, str) or len(url) > 4096:
        raise SearchError("A valid source URL is required.")
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme == "local":
            identifier = host or parsed.path.strip("/")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identifier):
                raise SearchError("Owned source IDs must be simple stable identifiers.")
            return f"local:{identifier}", "local", f"local://{identifier}"
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise SearchError(
                "Source URLs must use HTTPS without embedded credentials."
            )
        if host == "localhost" or "." not in host:
            raise SearchError("Local network source URLs are not supported.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise SearchError("Private network source URLs are not supported.")
        if not registration_only and not _domain_allowed(
            host, settings.allowed_domains
        ):
            raise SearchError(
                "This source domain is outside the configured discovery allowlist.", 403
            )
        if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
            if host == "youtu.be":
                identifier = parsed.path.strip("/").split("/")[0]
            elif parsed.path in {"/watch", "/watch/"}:
                identifier = parse_qs(parsed.query).get("v", [""])[0]
            else:
                parts = parsed.path.strip("/").split("/")
                identifier = (
                    parts[1]
                    if len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}
                    else ""
                )
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", identifier):
                raise SearchError(
                    "Select a direct YouTube video URL; channel and playlist expansion is bounded separately."
                )
            return (
                f"youtube:{identifier}",
                "youtube",
                "https://www.youtube.com/watch?" + urlencode({"v": identifier}),
            )
        # Only identity-safe public parameters survive registration; no credentials
        # or signed CDN locations are persisted as canonical public source URLs.
        query = parse_qs(parsed.query)
        public_query = {
            key: value
            for key, value in query.items()
            if key.lower() in {"id", "video", "videoid", "v", "p", "page"}
        }
        canonical = urlunsplit(
            ("https", host, parsed.path or "/", urlencode(public_query, doseq=True), "")
        )
        identifier = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        return f"web:{identifier}", "web", canonical
    except (ValueError, TypeError) as error:
        if isinstance(error, SearchError):
            raise
        raise SearchError("The source URL is malformed.") from None


def verify_public_network(url: str, settings, caption: bool = False) -> None:
    if caption:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or not _domain_allowed(
                    parsed.hostname.lower(), settings.allowed_domains
                )
            ):
                raise SearchError(
                    "Caption delivery host is outside the source allowlist.", 403
                )
        except ValueError:
            raise SearchError("The caption URL is malformed.") from None
    else:
        canonicalize_url(url, settings)
    host = urlsplit(url).hostname
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise SearchError(
            "The approved source host could not be resolved.", 503
        ) from None
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_global for item in addresses
    ):
        raise SearchError(
            "Source resolution reached a private or restricted network address.", 403
        )


def public_metadata(metadata: dict) -> dict:
    """Snapshot a whitelist: extractor credentials and signed URLs stay transient."""
    allowed = {
        "id",
        "title",
        "description",
        "uploader",
        "uploader_id",
        "channel",
        "channel_id",
        "creator_name",
        "creator_id",
        "duration",
        "duration_ms",
        "language",
        "upload_date",
        "published_at",
        "license",
        "license_claim",
        "extractor",
        "extractor_key",
        "evidence_type",
        "asset_kind",
        "context_note",
        "is_video",
        "owned",
        "catalog_id",
        "research_notes",
    }
    result = {}
    for key in allowed:
        value = metadata.get(key)
        if value is not None and isinstance(value, (str, int, float, bool, list)):
            if isinstance(value, float) and not math.isfinite(value):
                continue
            result[key] = value
    # Store availability, never signed subtitle delivery locations.
    for name in ("subtitles", "automatic_captions"):
        if isinstance(metadata.get(name), dict):
            result[name + "_languages"] = sorted(metadata[name])
    if isinstance(metadata.get("reference_urls"), list):
        references = []
        for value in metadata["reference_urls"][:100]:
            try:
                # Registration only: no fetch and no host allowlist expansion.
                _, _, canonical = canonicalize_url(value, None, registration_only=True)
                if canonical.startswith("https://"):
                    references.append(canonical)
            except SearchError:
                continue
        result["reference_urls"] = list(dict.fromkeys(references))
    return result


def persist_metadata(
    repo,
    source_id: str,
    metadata: dict,
    extractor_name: str = "provided",
    extractor_version: str | None = None,
) -> None:
    snapshot = public_metadata(metadata)
    serialized = json_text(snapshot)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    duration = snapshot.get("duration_ms")
    if duration is None and isinstance(snapshot.get("duration"), (float, int)):
        if isinstance(snapshot["duration"], bool):
            raise SearchError("Metadata duration must be a positive measured value.")
        duration = round(snapshot["duration"] * 1000)
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise SearchError("Metadata duration must be a positive measured value.")
    duration = round(duration) if duration is not None else None
    timestamp = now()
    with repo.connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO metadata_snapshots VALUES(?,?,?,?,?,?,?)",
            (
                new_id("snap_"),
                source_id,
                serialized,
                digest,
                extractor_name,
                extractor_version,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE sources SET title=?,description=?,creator_name=?,creator_id=?,duration_ms=COALESCE(?,duration_ms),language=COALESCE(?,language),published_at=COALESCE(?,published_at),metadata_json=?,metadata_hash=?,state=CASE WHEN state='registered' THEN 'metadata_fetched' ELSE state END,updated_at=? WHERE id=?",
            (
                str(snapshot.get("title", "")),
                str(snapshot.get("description", "")),
                str(
                    snapshot.get("creator_name")
                    or snapshot.get("uploader")
                    or snapshot.get("channel")
                    or ""
                ),
                snapshot.get("creator_id")
                or snapshot.get("uploader_id")
                or snapshot.get("channel_id"),
                duration,
                snapshot.get("language"),
                snapshot.get("published_at") or snapshot.get("upload_date"),
                serialized,
                digest,
                timestamp,
                source_id,
            ),
        )


class _QuietLogger:
    def debug(self, message):
        pass

    info = debug
    warning = debug
    error = debug


def _fetch_caption(url: str, settings) -> str:
    import requests

    # Extractor-controlled subtitle URLs are still checked before fetching. Do not
    # follow redirects or accept unrelated CDN hosts merely because metadata names one.
    verify_public_network(url, settings, caption=True)
    with requests.get(
        url, timeout=(15, 45), allow_redirects=False, stream=True
    ) as response:
        if response.status_code != 200:
            raise SearchError(
                f"The source caption endpoint returned HTTP {response.status_code}.",
                503,
            )
        body = bytearray()
        for block in response.iter_content(65536):
            body.extend(block)
            if len(body) > 5_000_000:
                raise SearchError(
                    "The caption payload exceeds the configured safety limit."
                )
        return body.decode("utf-8-sig", errors="replace")


def ingest_source(service, payload: dict) -> dict:
    source_id = payload["source_id"]
    source = service.repo.get("sources", source_id)
    if source is None:
        raise SearchError("Source not found.", 404)
    if source["platform"] == "local":
        return {"source_id": source_id, "state": source["state"]}
    url = source["canonical_url"]
    if urlsplit(url).hostname == "tegna.kurator.com":
        from .kurator import ingest_source as ingest_kurator

        return ingest_kurator(service, payload)
    verify_public_network(url, service.settings)
    try:
        import yt_dlp
    except ImportError:
        raise SearchError(
            "Install the targeted-search extra to discover metadata and captions.", 503
        ) from None
    options = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "socket_timeout": 30,
        "retries": 1,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": False,
        "cachedir": False,
        "ignoreconfig": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            metadata = downloader.extract_info(url, download=False)
    except Exception:
        raise SearchError(
            "Metadata discovery failed for this selected source; check source availability and extractor support.",
            503,
        ) from None
    if not isinstance(metadata, dict) or metadata.get("_type") in {
        "playlist",
        "multi_video",
    }:
        raise SearchError(
            "Select a single video URL for the caption-first discovery slice."
        )
    canonicalize_url(metadata.get("webpage_url") or url, service.settings)
    if (
        source["platform"] == "youtube"
        and metadata.get("id") != source["platform_video_id"]
    ):
        raise SearchError("The extractor returned a different source identity.", 409)
    if (
        isinstance(metadata.get("duration"), (int, float))
        and metadata["duration"] * 1000 > service.settings.max_source_duration_ms
    ):
        raise SearchError(
            "This source exceeds the configured processing duration limit."
        )
    persist_metadata(
        service.repo,
        source_id,
        metadata,
        metadata.get("extractor_key", "yt-dlp"),
        importlib.metadata.version("yt-dlp"),
    )
    indexed = 0
    errors = 0
    error_details = []
    for language in service.settings.caption_languages:
        for field, kind in (
            ("subtitles", "manual"),
            ("automatic_captions", "automatic"),
        ):
            language_map = metadata.get(field, {})
            # YouTube labels some authored English tracks en-<track-id>. These
            # are authentic captions, not a reason to fall back to translated ASR.
            matching = [
                key
                for key in language_map
                if key == language or key.startswith(language + "-")
            ]
            matching.sort(
                key=lambda key: (key != language, key != language + "-orig", key)
            )
            tracks = [track for key in matching for track in language_map[key]]
            tracks = sorted(
                tracks,
                key=lambda track: {"vtt": 0, "srt": 1, "json3": 2}.get(
                    track.get("ext"), 9
                ),
            )
            if not tracks:
                continue
            track = next(
                (item for item in tracks if item.get("ext") in {"vtt", "srt", "json3"}),
                None,
            )
            if track is None or not track.get("url"):
                continue
            try:
                raw = _fetch_caption(track["url"], service.settings)
                ingest_captions(
                    service.repo, source_id, raw, language, track["ext"], kind, "yt-dlp"
                )
                indexed += 1
                break  # Prefer authored captions over rolling automatic captions.
            except SearchError as error:
                errors += 1
                error_details.append(
                    {
                        "language": language,
                        "kind": kind,
                        "status": error.status_code,
                        "reason": error.message,
                    }
                )
    if not indexed:
        with service.repo.connect() as connection:
            connection.execute(
                "UPDATE sources SET state='captions_unavailable',updated_at=? WHERE id=?",
                (now(), source_id),
            )
    service.repo.event(
        "discovery_complete",
        source_id=source_id,
        payload={
            "caption_tracks_indexed": indexed,
            "caption_tracks_unavailable": errors,
            "caption_errors": error_details,
            "media_downloaded": False,
        },
    )
    if indexed and service.settings.semantic_enabled:
        service.enqueue_index(source_id)
    return {
        "source_id": source_id,
        "state": "text_indexed" if indexed else "captions_unavailable",
        "media_downloaded": False,
    }


def expand_collection(service, url: str, collection_id: str, limit: int = 50) -> dict:
    """Read a selected YouTube playlist/channel flatly, then queue caption jobs."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= service.settings.max_collection_sources
    ):
        raise SearchError("Collection expansion exceeds the configured source cap.")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise SearchError("Select an approved YouTube playlist or channel URL.")
        if not _domain_allowed(host, service.settings.allowed_domains):
            raise SearchError(
                "YouTube is outside the configured discovery allowlist.", 403
            )
        allowed_path = bool(
            re.fullmatch(r"/@[A-Za-z0-9_.-]+(?:/videos)?", parsed.path)
            or re.fullmatch(r"/channel/[A-Za-z0-9_-]+(?:/videos)?", parsed.path)
        )
        playlist = (
            parse_qs(parsed.query).get("list", [""])[0]
            if parsed.path == "/playlist"
            else ""
        )
        if not allowed_path and not re.fullmatch(r"[A-Za-z0-9_-]{6,120}", playlist):
            raise SearchError(
                "Only direct selected playlists and channels can be expanded."
            )
        canonical = urlunsplit(
            (
                "https",
                "www.youtube.com",
                parsed.path,
                urlencode({"list": playlist}) if playlist else "",
                "",
            )
        )
    except ValueError as error:
        if isinstance(error, SearchError):
            raise
        raise SearchError("The collection URL is malformed.") from None
    # Collection hosts need the same DNS boundary without requiring a video path.
    verify_public_network(canonical, service.settings, caption=True)
    if service.repo.get("collections", collection_id) is None:
        raise SearchError("Source collection not found.", 404)
    try:
        import yt_dlp
    except ImportError:
        raise SearchError(
            "Install the targeted-search extra to expand approved source collections.",
            503,
        ) from None
    options = {
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "socket_timeout": 30,
        "retries": 1,
        "cachedir": False,
        "ignoreconfig": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            data = downloader.extract_info(canonical, download=False)
            entries = list(itertools.islice(data.get("entries", []), limit))
    except Exception:
        raise SearchError(
            "The selected source collection could not be discovered.", 503
        ) from None
    sources = []
    for entry in entries:
        if not isinstance(entry, dict) or not re.fullmatch(
            r"[A-Za-z0-9_-]{6,64}", str(entry.get("id", ""))
        ):
            continue
        source_url = "https://www.youtube.com/watch?" + urlencode({"v": entry["id"]})
        service.register_metadata(
            source_url,
            entry.get("title") or "",
            entry.get("description") or "",
            entry.get("uploader") or "",
            collection_id=collection_id,
            metadata=public_metadata(entry),
        )
        sources.append(service.discover(source_url, collection_id=collection_id))
    service.repo.event(
        "collection_expanded",
        payload={
            "collection_id": collection_id,
            "count": len(sources),
            "limit": limit,
            "media_downloaded": False,
        },
    )
    return {
        "collection_id": collection_id,
        "sources": sources,
        "count": len(sources),
        "media_downloaded": False,
    }
