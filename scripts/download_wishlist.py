#!/usr/bin/env python3
"""Download wishlist tracks you don't already own via Soulseek.

This script bridges three things:

  1. the pymix **wishlist API**       -> what you *want*
  2. your **Navidrome library**       -> what you already *have* (queried over the
                                         Subsonic search API)
  3. an **slskd** instance            -> Soulseek client with an HTTP API to fetch the gap

For each wishlist item, it asks your Navidrome instance (which serves directly off
your beets collection) whether the track already exists by searching its index on
artist/title. Only items with no match are considered missing. For each missing item
it then searches Soulseek through slskd, picks the best available file, enqueues the
download and waits for it to finish. slskd writes the file into the directory it's
configured with (point that at your Subbox watch dir), and Subbox's watch importer
ingests it from there — this script never touches the download directory itself.

Once a file is pulled, this script flips its wishlist item to ``downloaded`` via the
pymix API (``PATCH /wishlist/{id}``). That's what stops the same track being fetched
again every pass: the next pass only pulls ``wishlist``-status items, and pymix's own
reconcile loop promotes ``downloaded`` -> ``available`` once beets has imported the
file and Navidrome can match it. This script never writes ``available`` itself.

Querying Navidrome's search per item -- rather than pulling the whole collection and
diffing locally -- keeps this cheap on large libraries: it's a handful of indexed,
server-side searches instead of a full-library download plus an O(items x tracks)
fuzzy scan held in memory.

Every wishlist row is expected to carry a curated artist + title, and a curated field
is always trusted as-is and searched against Soulseek verbatim -- this script never
overwrites an artist or title that's already populated. Only a *missing* field is
filled in, and only enough to plug that gap:

  * For a YouTube-sourced row missing a field, the real "Artist - Title" is often
    packed into the video title (with the uploading channel left in ``artist``) --
    so a missing piece is recovered by splitting the title on its " - ".
  * If a field is still missing (curation failed, or the row was added straight from
    a link and never curated), the script falls back to the row's source URL
    (``youtube_url`` / ``bandcamp_url`` / ``soundcloud_url``): it fetches a free-text
    title via oEmbed, then asks MusicBrainz's recording search -- built to cope with
    noisy, unstructured strings -- for its best-scoring (artist, title) match.

Pass ``--no-musicbrainz-fallback`` to skip the MusicBrainz step. A row with no
artist/title *and* no URL (a bare raw-note inbox entry) is always skipped -- there's
nothing to search on.

It uses only the Python standard library, so it runs with a bare ``python3`` and no
``pip install`` -- handy for non-technical users who just need slskd running locally.

Navidrome side: this talks to the **Subsonic REST API** your Navidrome already
exposes. Auth follows the Subsonic scheme -- username + a salted MD5 token of your
password -- using the **same credentials you log into Subbox with**. So a single
``--username`` / ``--password`` pair covers both the wishlist API and Navidrome.

Soulseek side: this talks to **slskd** (https://github.com/slskd/slskd), NOT the
SoulseekQt / Nicotine+ desktop apps (those expose no automation API). The script just
needs slskd's HTTP URL and its web login (username/password).


Setting up slskd
----------------

Easiest: use the helper script for your platform — they sit alongside this one:

    scripts/run-slskd-macos.sh           # macOS (Apple Silicon or Intel — auto-detected)
    scripts/run-slskd-windows.ps1        # Windows

On first run it downloads slskd; every run it prompts for your credentials and the
directory you want finished tracks in (your Subbox watch dir), launches slskd pointed
at that dir, verifies the login, and prints the exact ``download_wishlist.py`` command
to copy — pre-filled with the matching ``--slskd-url`` and ``--slskd-username``. It
also caches the credentials and dir so later runs don't re-prompt, and leaves slskd at
http://127.0.0.1:5030 (your ``--slskd-url``).

This script never touches the download directory itself — it only talks to the pymix,
Navidrome and slskd HTTP APIs. slskd is the one that writes files, so you tell *slskd*
where they go (the run script does this for you via its prompt; an slskd you run by
hand needs its ``downloads`` dir set to your watch dir). Once a download completes,
Subbox's watch importer picks it up from there, including from slskd's per-uploader
subfolder, so no move step is needed.

You enter ONE username/password and the run script uses it for both credentials slskd
needs — keep the distinction in mind if you ever configure slskd by hand:

  * **Soulseek** login — logs slskd *into* the Soulseek network. Free; pick any
    username/password and it registers on first connect.
  * **slskd web** login — guards slskd's own web UI / API. THIS is what this script
    authenticates with via ``--slskd-username`` / ``--slskd-password`` (no API key
    needed). The script exchanges them for a token via slskd's ``/api/v0/session``.

(Advanced: prefer to run slskd yourself? Any slskd works — Docker or the standalone
binary. Set the Soulseek creds (``soulseek.*``) and a web login
(``web.authentication.*``), then point ``--slskd-url`` / ``--slskd-username`` /
``--slskd-password`` here. An ``--slskd-api-key`` from ``web.authentication.api_keys``
also works and takes precedence over the web login if both are given.)

Usage (minimal):

    python3 scripts/download_wishlist.py \
        --username alice \
        --password "$SUBBOX_PASSWORD" \
        --slskd-username "$SLSKD_USERNAME" \
        --slskd-password "$SLSKD_PASSWORD"

Most connection settings have env-var fallbacks (see ``--help``) so you can avoid
long command lines:

    PYMIX_URL, PYMIX_USERNAME, PYMIX_PASSWORD, PYMIX_SESSION_ID,
    NAVIDROME_URL,
    SLSKD_URL, SLSKD_USERNAME, SLSKD_PASSWORD, SLSKD_API_KEY

Use ``--dry-run`` to see what *would* be downloaded without enqueuing anything.

By default the script waits for the enqueued transfers to finish and reports the
result (slskd writes the files itself); pass ``--no-wait`` to enqueue and exit,
letting slskd finish them in the background.

Add ``--watch`` to keep it running: after each pass it sleeps ``--interval`` seconds
(default 300) and re-checks the wishlist, picking up anything newly added or still
missing. A failing pass (e.g. a brief network blip) is logged and the watcher keeps
going; Ctrl-C stops it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Audio file extensions we're willing to download, best-preferred first. FLAC
# (lossless) ranks top, then MP3; the remaining formats are fallbacks. Within a
# single format, higher bitrate wins (see pick_file), so a 320kbps MP3 beats a
# 128kbps one.
AUDIO_EXTENSIONS = (".flac", ".mp3", ".alac", ".wav", ".aiff", ".m4a", ".aac", ".ogg", ".opus")
# Quality preference: lower index == more preferred format.
FORMAT_RANK = {ext: i for i, ext in enumerate(AUDIO_EXTENSIONS)}

# Set by --insecure: when True, HTTPS requests skip certificate verification. Needed
# for local dev behind self-signed certs (e.g. *.docker.localhost); never for prod.
_INSECURE_TLS = False


def _ssl_context() -> Optional[ssl.SSLContext]:
    if not _INSECURE_TLS:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# --------------------------------------------------------------------------- #
# Small stdlib HTTP helper
# --------------------------------------------------------------------------- #
def http_request(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[Any] = None,
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    timeout: float = 30.0,
    retries: int = 4,
    retry_backoff: float = 1.0,
) -> Any:
    """Make an HTTP request and return parsed JSON (or ``None`` for empty bodies).

    Raises ``RuntimeError`` with a readable message on non-2xx responses.

    Connection-level failures (refused/reset/dropped, vs. an HTTP error status) are
    retried up to ``retries`` times with exponential backoff. slskd in particular
    intermittently resets its API connections when it's busy distributing a search
    across the Soulseek network or starting a transfer — a brief wait and retry rides
    over that, where a single attempt would spuriously fail the whole item. HTTP error
    *statuses* (4xx/5xx) are deterministic and are not retried.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = None
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {body[:500]}") from exc
        except OSError as exc:
            # Covers URLError (connect failures) and bare socket errors such as
            # ConnectionResetError / RemoteDisconnected raised *during* the response
            # read. Retry with backoff; give up (as a RuntimeError, which callers treat
            # as a recoverable per-item failure) once attempts are exhausted.
            attempt += 1
            if attempt > retries:
                reason = getattr(exc, "reason", exc)
                raise RuntimeError(
                    f"{method} {url} -> connection error after {attempt} attempt(s): {reason}"
                ) from exc
            time.sleep(retry_backoff * (2 ** (attempt - 1)))

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Normalisation + matching
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalise(text: Optional[str]) -> str:
    """Lower-case, drop parenthetical asides (feat/remix/etc.) and punctuation."""
    if not text:
        return ""
    text = text.lower()
    text = _PAREN_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def track_key(artist: Optional[str], title: Optional[str]) -> str:
    return f"{normalise(artist)} {normalise(title)}".strip()


# A " - " (hyphen/en-dash/em-dash surrounded by spaces) separating an embedded
# "Artist - Title" inside a single string.
_ARTIST_TITLE_SEP_RE = re.compile(r"\s[-–—]\s")


def split_youtube_artist_title(artist: str, title: str) -> tuple[str, str]:
    """Recover the real artist/title for a YouTube-sourced wishlist item.

    When an item is added from YouTube, ``artist`` is the uploading *channel*
    (e.g. "AusMusicUK", "Houseum") -- not the performer -- and the real
    "Artist - Title" is packed into the video ``title`` (e.g.
    "Will Hofbauer - Squito"). Searching with the channel name in front finds
    nothing on Soulseek, so we drop it: split the title on its " - " separator
    and use those parts. If the title has no separator there's no artist to
    recover, so we just drop the channel and search on the bare title.

    This is gated on YouTube provenance precisely so it never mangles a normal
    item whose title legitimately contains " - " (e.g. "Xtal - Original Mix").
    """
    parts = _ARTIST_TITLE_SEP_RE.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", title


def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------- #
# Fallback: URL -> MusicBrainz, for wishlist rows with no curated artist/title
# --------------------------------------------------------------------------- #
# A "wishlist"-status row is supposed to already have artist + title curated (see
# pymix's WishlistStatus docs), but curation can fail or be skipped, leaving a row
# with only a source URL. Rather than silently dropping those rows, pull a free-text
# title off the URL via oEmbed and let MusicBrainz's recording search -- which is
# built to cope with noisy, unstructured strings -- resolve it to a clean
# artist/title. Both steps are plain HTTP GET + JSON, so this stays inside the
# stdlib-only constraint the rest of the script has to honour (no musicbrainzngs,
# no yt-dlp). Mirrors the approach prototyped in scratch/musicbrainz.py.
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_USER_AGENT = "subbox-wishlist-downloader/1.0 ( https://github.com/laker-93/subbox-slskd )"

# oEmbed endpoints for the URL types a wishlist row can carry. Each returns JSON with
# (at least) `title` and `author_name` via a plain unauthenticated GET.
_OEMBED_ENDPOINTS = {
    "youtube_url": "https://www.youtube.com/oembed",
    "soundcloud_url": "https://soundcloud.com/oembed",
    "bandcamp_url": "https://bandcamp.com/oembed",
}

# MusicBrainz asks for no more than one request/second without an API key. This
# fallback only fires for rows missing both artist and title -- rare -- so a naive
# process-wide throttle is enough; no need for anything fancier.
_last_musicbrainz_call = 0.0


def _throttle_musicbrainz() -> None:
    global _last_musicbrainz_call
    wait = _last_musicbrainz_call + 1.0 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_musicbrainz_call = time.monotonic()


def extract_metadata_text(url: str, url_field: str) -> Optional[str]:
    """Fetch a free-text description ("uploader title") of `url` via oEmbed.

    Returned as one string so it can be handed straight to MusicBrainz as a search
    query -- no further parsing is attempted here, since MusicBrainz's own relevance
    scoring is what disambiguates it.
    """
    endpoint = _OEMBED_ENDPOINTS.get(url_field)
    if not endpoint:
        return None
    try:
        data = http_request("GET", endpoint, params={"url": url, "format": "json"})
    except RuntimeError as exc:
        print(f"  ! oEmbed lookup failed for {url}: {exc}")
        return None
    if not isinstance(data, dict):
        return None
    text = " ".join(p for p in (data.get("author_name"), data.get("title")) if p).strip()
    return text or None


def musicbrainz_best_match(query_text: str) -> Optional[tuple[str, str]]:
    """Search MusicBrainz recordings for `query_text`, return the best (artist, title).

    MusicBrainz's recording search accepts a free-text query and returns its own
    relevance `score` (0-100) per hit, so we just sort on that and take the top
    result -- same approach as the youtube-track-matcher prototype this mirrors.
    """
    if not query_text or not query_text.strip():
        return None
    _throttle_musicbrainz()
    try:
        data = http_request(
            "GET",
            MUSICBRAINZ_URL,
            params={"query": query_text, "fmt": "json", "limit": 5},
            headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
        )
    except RuntimeError as exc:
        print(f"  ! musicbrainz search failed for {query_text!r}: {exc}")
        return None

    recordings = (data or {}).get("recordings", []) or []
    if not recordings:
        return None
    recordings.sort(key=lambda r: int(r.get("score", 0) or 0), reverse=True)
    best = recordings[0]

    title = best.get("title")
    artist = None
    credits = best.get("artist-credit") or []
    if credits:
        artist = "".join(
            c.get("name") or (c.get("artist") or {}).get("name", "")
            for c in credits
            if isinstance(c, dict)
        )
    if not artist or not title:
        return None
    return artist, title


def resolve_missing_metadata(raw_item: dict) -> tuple[str, str]:
    """Best-effort (artist, title) for a wishlist row with no curated artist/title.

    Tries each URL field the row carries, in order, extracting a free-text
    description via oEmbed and asking MusicBrainz for its best-scoring match.
    Returns ("", "") if nothing resolves -- the caller then skips the row exactly
    as it did before this fallback existed.
    """
    for url_field in ("youtube_url", "bandcamp_url", "soundcloud_url"):
        url = raw_item.get(url_field)
        if not url:
            continue
        text = extract_metadata_text(url, url_field)
        if not text:
            continue
        match = musicbrainz_best_match(text)
        if match:
            return match
    return "", ""


# --------------------------------------------------------------------------- #
# Data sources
# --------------------------------------------------------------------------- #
@dataclass
class WishItem:
    wishlist_id: str
    artist: str
    title: str
    album: Optional[str]
    status: str

    @property
    def query(self) -> str:
        return " ".join(p for p in (self.artist, self.title) if p).strip()


def fetch_wishlist(
    pymix_url: str,
    username: Optional[str],
    session_id: Optional[str],
    status: str,
    use_musicbrainz_fallback: bool = True,
) -> list[WishItem]:
    """GET {pymix_url}/wishlist filtered by status. Auth via username or session_id."""
    params = {"status": status}
    cookies = None
    if username:
        params["username"] = username
    if session_id:
        cookies = {"session_id": session_id}
    if not username and not session_id:
        raise RuntimeError("Provide --username or --session-id to identify the wishlist owner.")

    data = http_request("GET", f"{pymix_url.rstrip('/')}/wishlist", params=params, cookies=cookies)
    items = (data or {}).get("items", [])
    out: list[WishItem] = []
    for it in items:
        artist = (it.get("artist") or "").strip()
        title = (it.get("title") or "").strip()

        # A curated wishlist row's artist/title is authoritative -- never overwrite
        # a field that's already populated. Fallbacks below only ever *fill in* a
        # field that's missing, using whichever value they derive for that field.
        if not artist or not title:
            # Some YouTube-sourced rows carry the uploading *channel* in `artist`
            # (not the performer) with the real "Artist - Title" packed into
            # `title`. Only reach for this when a field is actually missing --
            # e.g. artist blank, title = "Will Hofbauer - Squito".
            if it.get("youtube_video_id") or it.get("youtube_url"):
                derived_artist, derived_title = split_youtube_artist_title(artist, title)
                artist = artist or derived_artist
                title = title or derived_title

        if (not artist or not title) and use_musicbrainz_fallback:
            # Still missing a field: fall back to whatever source URL the row
            # carries -- pull a free-text title via oEmbed and let MusicBrainz
            # resolve it. Again, only used to fill the gap, not to replace a
            # field that's already set.
            derived_artist, derived_title = resolve_missing_metadata(it)
            artist = artist or derived_artist
            title = title or derived_title

        if not artist and not title:
            # Nothing to search on -- e.g. a raw_note-only inbox row with no
            # link, or a lookup that didn't resolve. Skip it.
            continue
        out.append(
            WishItem(
                wishlist_id=it.get("wishlist_id", ""),
                artist=artist,
                title=title,
                album=(it.get("album") or None),
                status=it.get("status", status),
            )
        )
    return out


# pymix wishlist status we flip an item to once we've pulled its file (mirrors
# pymix's WishlistStatus.DOWNLOADED = "downloaded": "file has landed but not yet in
# beets"). pymix's reconcile loop later promotes it to "available" once beets has
# imported the file and Navidrome can match it. We never write "available" ourselves.
WISHLIST_STATUS_DOWNLOADED = "downloaded"


def set_wishlist_status(
    pymix_url: str,
    username: Optional[str],
    session_id: Optional[str],
    wishlist_id: str,
    status: str,
) -> None:
    """PATCH {pymix_url}/wishlist/{wishlist_id} to set its status. Auth as fetch_wishlist."""
    params = {}
    cookies = None
    if username:
        params["username"] = username
    if session_id:
        cookies = {"session_id": session_id}
    http_request(
        "PATCH",
        f"{pymix_url.rstrip('/')}/wishlist/{urllib.parse.quote(wishlist_id)}",
        params=params or None,
        json_body={"status": status},
        cookies=cookies,
    )


def default_navidrome_url(username: str) -> str:
    """Per-user Navidrome lives at ``https://navidrome<username>.sub-box.net``.

    e.g. username ``cargobox`` -> ``https://navidromecargobox.sub-box.net``.
    """
    return f"https://navidrome{username}.sub-box.net"


class Navidrome:
    """Minimal Subsonic-API client used only to check whether a track already exists.

    Navidrome serves directly off the beets collection, so its search index *is* the
    user's library. Auth uses the Subsonic salted-token scheme: each request carries
    ``u`` (username), ``t`` = md5(password + salt) and ``s`` (salt), so the plaintext
    password never goes over the wire.
    """

    API_VERSION = "1.16.1"
    CLIENT = "subbox-wishlist"

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

    def _auth_params(self) -> dict:
        salt = "".join(random.choice(string.ascii_lowercase) for _ in range(8))
        token = hashlib.md5(f"{self.password}{salt}".encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": self.API_VERSION,
            "c": self.CLIENT,
            "f": "json",
        }

    def _call(self, view: str, extra: Optional[dict] = None) -> dict:
        params = self._auth_params()
        if extra:
            params.update(extra)
        data = http_request("GET", f"{self.base}/rest/{view}", params=params, timeout=self.timeout)
        resp = (data or {}).get("subsonic-response", {})
        if resp.get("status") != "ok":
            err = resp.get("error") or {}
            raise RuntimeError(f"Subsonic error {err.get('code', '?')}: {err.get('message', resp or 'no response')}")
        return resp

    def ping(self) -> None:
        """Validate URL + credentials up front so per-item failures are genuinely rare."""
        self._call("ping.view")

    def search_songs(self, query: str, count: int = 20) -> list[dict]:
        """Return song hits for a free-text query via search3 (artists/albums excluded)."""
        resp = self._call(
            "search3.view",
            {"query": query, "artistCount": 0, "albumCount": 0, "songCount": count},
        )
        return resp.get("searchResult3", {}).get("song", []) or []


def is_in_collection(nav: Navidrome, item: WishItem, threshold: float, count: int = 20) -> bool:
    """True if Navidrome's search returns a song matching this item above ``threshold``.

    The server-side search narrows the library to a few candidates; we then confirm
    with the same normalised fuzzy comparison used elsewhere, so match quality matches
    the old full-collection diff while only inspecting a handful of rows per item.
    """
    target = track_key(item.artist, item.title)
    if not target:
        return False
    songs = nav.search_songs(item.query, count=count)
    return any(
        similar(target, track_key(s.get("artist"), s.get("title"))) >= threshold for s in songs
    )


# --------------------------------------------------------------------------- #
# slskd client
# --------------------------------------------------------------------------- #
class Slskd:
    """slskd web-API client.

    Authenticates either with a static API key (``X-API-Key``) or with slskd's web
    login (``--slskd-username`` / ``--slskd-password``), which is exchanged for a JWT
    via ``POST /api/v0/session`` and sent as a ``Bearer`` token. These are slskd's
    *web* credentials (``web.authentication`` in slskd.yml), NOT your Soulseek network
    login.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token: Optional[str] = None

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v0/{path.lstrip('/')}"

    def _login(self) -> str:
        """Exchange web username/password for a JWT and cache it."""
        if not (self.username and self.password):
            raise RuntimeError("slskd needs either an API key or a username + password.")
        resp = http_request(
            "POST",
            self._url("session"),
            json_body={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        token = resp.get("token") if isinstance(resp, dict) else None
        if not token:
            raise RuntimeError(
                f"slskd login to {self.base} did not return a token "
                f"(check --slskd-username/--slskd-password): {str(resp)[:200]}"
            )
        return token

    @property
    def headers(self) -> dict:
        """Auth header for every request; logs in lazily when using username/password."""
        if self.api_key:
            return {"X-API-Key": self.api_key}
        if self._token is None:
            self._token = self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def search(self, text: str, wait: float, poll: float = 1.5) -> list[dict]:
        """Start a search, wait for slskd to gather results, and return the responses.

        Soulseek search is peer-to-peer and asynchronous: slskd forwards the query to
        the network and matches *trickle back* over several seconds, only marking the
        search ``Completed`` once its own server-side search timeout elapses — on a
        stock slskd that's ~15s. So we must wait for slskd to declare the search
        finished; ``wait`` is only a hard cap (the loop breaks early the moment slskd
        reports completion). Bailing out before then routinely returns an empty list
        even when the identical query in the slskd UI shows plenty of files a few
        seconds later — the UI is simply being read after the results have landed.
        """
        created = http_request(
            "POST", self._url("searches"), json_body={"searchText": text}, headers=self.headers, timeout=self.timeout
        )
        search_id = created.get("id")
        deadline = time.monotonic() + wait
        while True:
            time.sleep(poll)
            state = http_request("GET", self._url(f"searches/{search_id}"), headers=self.headers, timeout=self.timeout)
            # slskd signals completion via `isComplete`, an `endedAt` timestamp, or a
            # `state` string containing "Completed" (which spelling varies by version).
            complete = bool(
                state.get("isComplete")
                or state.get("endedAt")
                or "Completed" in str(state.get("state", ""))
            )
            if complete or time.monotonic() >= deadline:
                break
        responses = http_request(
            "GET", self._url(f"searches/{search_id}/responses"), headers=self.headers, timeout=self.timeout
        )
        return responses or []

    def enqueue(self, username: str, files: list[dict]) -> None:
        payload = [{"filename": f["filename"], "size": f["size"]} for f in files]
        http_request(
            "POST",
            self._url(f"transfers/downloads/{urllib.parse.quote(username)}"),
            json_body=payload,
            headers=self.headers,
            timeout=self.timeout,
        )

    def downloads_for(self, username: str) -> list[dict]:
        """Flat list of this user's download file-transfer objects."""
        data = http_request(
            "GET",
            self._url(f"transfers/downloads/{urllib.parse.quote(username)}"),
            headers=self.headers,
            timeout=self.timeout,
        )
        files: list[dict] = []
        for directory in (data or {}).get("directories", []):
            files.extend(directory.get("files", []))
        return files


def pick_file(responses: list[dict], item: WishItem, min_file_score: float = 0.6) -> Optional[tuple[str, dict]]:
    """Choose the best (username, file) for a wishlist item, or None if nothing fits.

    A candidate file is rejected outright if its filename similarity to the wishlist
    item falls below ``min_file_score``. This score compares against the *filename stem*
    (which carries track numbers like ``02. ``, ``(Original Mix)`` suffixes and other
    uploader/album noise), so it is deliberately looser than the tag-based
    ``--match-threshold`` used to decide a track is already owned. Set it too low and
    the script downloads near-random files that can never reconcile back to
    ``available`` and so get re-downloaded every pass; too high and legitimately-named
    files with noisy stems get skipped.
    """
    target = track_key(item.artist, item.title)
    candidates: list[tuple[tuple, str, dict]] = []
    for resp in responses:
        username = resp.get("username")
        has_slot = bool(resp.get("hasFreeUploadSlot"))
        queue_len = resp.get("queueLength", 0) or 0
        speed = resp.get("uploadSpeed", 0) or 0
        for f in resp.get("files", []):
            name = f.get("filename", "")
            ext = Path(name.replace("\\", "/")).suffix.lower()
            if ext not in FORMAT_RANK:
                continue
            name_score = similar(target, normalise(Path(name.replace("\\", "/")).stem))
            if name_score < min_file_score:
                continue
            bitrate = f.get("bitRate") or 0
            # Sort key: free slot first, then closer filename, better format,
            # higher bitrate (e.g. 320 vs 128 MP3), shorter queue, faster uploader.
            # (negate things we want to maximise)
            sort_key = (
                0 if has_slot else 1,
                -round(name_score, 3),
                FORMAT_RANK[ext],
                -bitrate,
                queue_len,
                -speed,
            )
            candidates.append((sort_key, username, f))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, username, f = candidates[0]
    return username, f


# --------------------------------------------------------------------------- #
# Waiting for downloads to complete
# --------------------------------------------------------------------------- #
def remote_basename(filename: str) -> str:
    return Path(filename.replace("\\", "/")).name


def wait_for_downloads(
    slskd: Slskd,
    pending: list[tuple[str, dict, WishItem]],
    timeout: float,
    poll: float = 3.0,
    on_downloaded: Optional["Callable[[WishItem], None]"] = None,
) -> tuple[int, int]:
    """Poll slskd until the enqueued transfers finish, reporting each outcome.

    slskd writes the files itself (into the dir it's configured with — your watch
    dir), so there's nothing to move; we just watch the transfer state for feedback.

    ``on_downloaded`` (if given) is called with the ``WishItem`` each time a transfer
    succeeds — used to flip that item to ``downloaded`` in pymix so it isn't
    re-downloaded on the next pass. It's invoked once per item, right after the success
    is observed, so a long wait still records progress as it happens.

    Returns (completed, failed). Anything still in flight when ``timeout`` elapses is
    reported as timed out and counted as neither.
    """
    remaining = {(u, remote_basename(f["filename"])): item for (u, f, item) in pending}
    completed = 0
    failed = 0
    deadline = time.monotonic() + timeout

    while remaining and time.monotonic() < deadline:
        time.sleep(poll)
        # Group lookups by username to limit API calls.
        for username in {u for (u, _name) in remaining}:
            try:
                files = slskd.downloads_for(username)
            except RuntimeError:
                continue
            for f in files:
                base = remote_basename(f.get("filename", ""))
                state = str(f.get("state", ""))
                key = (username, base)
                if key not in remaining:
                    continue
                if "Completed" in state and "Succeeded" in state:
                    print(f"  ✓ downloaded {base!r} from {username}")
                    completed += 1
                    item = remaining.pop(key, None)
                    if item is not None and on_downloaded is not None:
                        on_downloaded(item)
                elif "Completed" in state and ("Errored" in state or "Cancelled" in state or "Rejected" in state):
                    print(f"  ! transfer failed for {base!r} from {username}: {state}")
                    failed += 1
                    remaining.pop(key, None)

    for (username, base) in remaining:
        print(f"  … timed out waiting for {base!r} from {username}")
    return completed, failed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download wishlist tracks you don't already own (per Navidrome) via Soulseek (slskd).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # pymix / wishlist
    p.add_argument("--pymix-url", default=os.environ.get("PYMIX_URL", "http://pymix.docker.localhost/pymix"),
                   help="Base pymix URL (including the /pymix prefix if behind the proxy).")
    p.add_argument("--username", default=os.environ.get("PYMIX_USERNAME"),
                   help="Subbox username (used for both the wishlist API and Navidrome).")
    p.add_argument("--password", default=os.environ.get("PYMIX_PASSWORD"),
                   help="Subbox password (same login as the player; used to authenticate to Navidrome).")
    p.add_argument("--session-id", default=os.environ.get("PYMIX_SESSION_ID"),
                   help="Optional pymix session_id cookie for the wishlist API (Navidrome still needs --username/--password).")
    p.add_argument("--status", default="wishlist",
                   help="Wishlist status to pull (items still wanting acquisition).")
    p.add_argument("--no-musicbrainz-fallback", action="store_true",
                   help="Skip rows with no curated artist/title instead of resolving them via "
                        "their source URL (oEmbed) + MusicBrainz recording search.")

    # navidrome (owned-check via Subsonic search)
    p.add_argument("--navidrome-url", default=os.environ.get("NAVIDROME_URL"),
                   help="Navidrome base URL. Defaults to https://navidrome<username>.sub-box.net.")
    p.add_argument("--navidrome-song-count", type=int, default=20,
                   help="How many song hits to inspect per search before deciding a track is owned.")

    # slskd
    p.add_argument("--slskd-url", default=os.environ.get("SLSKD_URL", "http://127.0.0.1:5030"),
                   help="slskd base URL. Defaults to the IPv4 loopback on port 5030 (a "
                        "'localhost' URL is rewritten to 127.0.0.1 to avoid IPv6-loopback "
                        "connection resets against slskd); set to a remote host if slskd runs "
                        "elsewhere, e.g. https://slskd.example.com.")
    p.add_argument("--slskd-api-key", default=os.environ.get("SLSKD_API_KEY"),
                   help="slskd API key. Alternative to --slskd-username/--slskd-password.")
    p.add_argument("--slskd-username", default=os.environ.get("SLSKD_USERNAME"),
                   help="slskd web login username (slskd.yml web.authentication, NOT your Soulseek login).")
    p.add_argument("--slskd-password", default=os.environ.get("SLSKD_PASSWORD"),
                   help="slskd web login password. Used with --slskd-username if no API key is given.")

    # behaviour
    p.add_argument("--match-threshold", type=float, default=0.85,
                   help="Fuzzy similarity (0-1) above which a wishlist item counts as already owned.")
    p.add_argument("--min-file-score", type=float, default=0.6,
                   help="Minimum filename similarity (0-1) for a Soulseek file to be a "
                        "download candidate. Looser than --match-threshold because it scores "
                        "against the filename stem (track numbers, '(Original Mix)', album "
                        "noise) rather than tags. Raise it if the script keeps grabbing poor "
                        "matches that never reconcile to 'available' and so re-download each pass.")
    p.add_argument("--max-downloads", type=int, default=0, help="Cap the number of downloads (0 = no cap).")
    p.add_argument("--search-wait", type=float, default=30.0,
                   help="Hard cap (seconds) on gathering Soulseek search results per item. "
                        "slskd usually completes its own search in ~15s and we stop as soon as "
                        "it does, so this is just an upper bound for slow/quiet queries.")
    p.add_argument("--download-timeout", type=float, default=600.0,
                   help="Max seconds to wait for all transfers to finish.")
    p.add_argument("--no-wait", action="store_true",
                   help="Enqueue downloads and exit without waiting; slskd finishes them in the background.")
    p.add_argument("--watch", action="store_true",
                   help="Run continuously: after each pass, sleep --interval and check the "
                        "wishlist again, downloading anything newly added or still missing. "
                        "Ctrl-C to stop.")
    p.add_argument("--interval", type=float, default=300.0,
                   help="Seconds to sleep between passes in --watch mode.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show missing tracks and chosen files without enqueuing.")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification — for local dev behind self-signed "
                        "certs (e.g. *.docker.localhost). Do not use against production.")
    return p.parse_args(argv)


def _ipv4_localhost(url: str) -> str:
    """Rewrite a ``localhost`` slskd URL to the IPv4 loopback ``127.0.0.1``.

    slskd binds IPv6 dual-stack (it logs ``Listening ... at http://:::5030/``). On
    macOS ``localhost`` resolves to ``::1`` as well as ``127.0.0.1``, and connecting to
    slskd's socket over the IPv6 loopback gets the connection reset before the request
    reaches slskd's handlers ("Connection reset by peer" / "Remote end closed
    connection") — so a run that talks to slskd over ``localhost`` fails almost every
    call. Forcing IPv4 avoids it entirely. Non-localhost hosts (a remote slskd reached
    by DNS) are left untouched.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname == "localhost":
        netloc = parsed.netloc.replace("localhost", "127.0.0.1", 1)
        return urllib.parse.urlunsplit(parsed._replace(netloc=netloc))
    return url


def build_slskd(args: argparse.Namespace) -> Optional[Slskd]:
    """Construct an Slskd client from whichever credentials were supplied, else None."""
    url = _ipv4_localhost(args.slskd_url)
    if args.slskd_api_key:
        return Slskd(url, api_key=args.slskd_api_key)
    if args.slskd_username and args.slskd_password:
        return Slskd(url, username=args.slskd_username, password=args.slskd_password)
    return None


def run_once(
    args: argparse.Namespace,
    navidrome_url: str,
) -> int:
    """Do one full pass: fetch wishlist → diff against the library → search/enqueue.

    Returns a process exit code. In --watch mode the caller ignores it and keeps
    looping, so a transient failure here (wishlist fetch, Navidrome ping) ends only
    the current pass, not the watcher.
    """
    # 1. wishlist
    try:
        wishlist = fetch_wishlist(
            args.pymix_url, args.username, args.session_id, args.status,
            use_musicbrainz_fallback=not args.no_musicbrainz_fallback,
        )
    except RuntimeError as exc:
        print(f"error: failed to fetch wishlist: {exc}", file=sys.stderr)
        return 1
    print(f"wishlist: {len(wishlist)} item(s) with status {args.status!r}")
    if not wishlist:
        return 0

    # 2. owned-check via Navidrome (Subsonic search), one indexed query per item
    nav = Navidrome(navidrome_url, args.username, args.password)
    try:
        nav.ping()
    except RuntimeError as exc:
        print(f"error: could not reach Navidrome at {navidrome_url}: {exc}", file=sys.stderr)
        return 1

    missing: list[WishItem] = []
    for it in wishlist:
        try:
            owned = is_in_collection(nav, it, args.match_threshold, args.navidrome_song_count)
        except RuntimeError as exc:
            # A single search failing (vs the ping that already validated auth) is
            # most likely transient. Surface it and treat the item as missing so the
            # user still gets a download attempt rather than a silent skip.
            print(f"  ! navidrome search failed for {it.artist} - {it.title}: {exc}", file=sys.stderr)
            owned = False
        if not owned:
            missing.append(it)
    print(f"missing:  {len(missing)} item(s) not in your library\n")
    if not missing:
        print("Nothing to download.")
        return 0
    if args.max_downloads > 0:
        missing = missing[: args.max_downloads]

    if args.dry_run:
        slskd = build_slskd(args)
        for it in missing:
            line = f"- {it.artist} - {it.title}"
            if slskd:
                try:
                    chosen = pick_file(slskd.search(it.query, args.search_wait), it, args.min_file_score)
                    line += f"  =>  {chosen[1]['filename']}" if chosen else "  =>  (no match found)"
                except RuntimeError as exc:
                    line += f"  =>  (search failed: {exc})"
            print(line)
        print(f"\n[dry-run] {len(missing)} item(s) would be processed.")
        return 0

    # 4. search + enqueue
    slskd = build_slskd(args)
    assert slskd is not None  # guaranteed: non-dry-run requires slskd credentials
    pending: list[tuple[str, dict, WishItem]] = []
    for it in missing:
        print(f"searching: {it.artist} - {it.title}")
        try:
            responses = slskd.search(it.query, args.search_wait)
        except RuntimeError as exc:
            print(f"  ! search failed: {exc}")
            continue
        chosen = pick_file(responses, it, args.min_file_score)
        if not chosen:
            print("  - no suitable file found")
            continue
        username, f = chosen
        try:
            slskd.enqueue(username, [f])
        except RuntimeError as exc:
            print(f"  ! enqueue failed: {exc}")
            continue
        print(f"  + queued {remote_basename(f['filename'])} from {username}")
        pending.append((username, f, it))

    def mark_downloaded(item: WishItem) -> None:
        """Flip a pulled item to ``downloaded`` in pymix so it isn't re-fetched.

        The next pass fetches ``--status wishlist`` and so skips it; pymix's reconcile
        loop promotes it to ``available`` once beets imports the file. A failure here is
        non-fatal — the file is already downloaded — so we warn and move on (worst case
        the item is retried next pass, i.e. the old behaviour).
        """
        if not item.wishlist_id:
            print(f"  ! can't mark {item.artist} - {item.title} downloaded: no wishlist_id")
            return
        try:
            set_wishlist_status(
                args.pymix_url, args.username, args.session_id,
                item.wishlist_id, WISHLIST_STATUS_DOWNLOADED,
            )
        except RuntimeError as exc:
            print(f"  ! failed to mark {item.artist} - {item.title} downloaded: {exc}")

    print(f"\nqueued {len(pending)} download(s).")
    if not pending or args.no_wait:
        if args.no_wait and pending:
            # Can't confirm completion in --no-wait, so mark optimistically: the
            # alternative is leaving them 'wishlist' and re-downloading them next pass.
            for _u, _f, it in pending:
                mark_downloaded(it)
            print("(--no-wait) slskd will finish these into its configured downloads dir.")
        return 0

    print("waiting for downloads to complete…")
    completed, failed = wait_for_downloads(
        slskd, pending, args.download_timeout, on_downloaded=mark_downloaded
    )
    print(f"\ndone: {completed} completed, {failed} failed.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.insecure:
        global _INSECURE_TLS
        _INSECURE_TLS = True

    have_slskd_auth = bool(args.slskd_api_key or (args.slskd_username and args.slskd_password))
    if not args.dry_run and not have_slskd_auth:
        print(
            "error: slskd credentials required unless --dry-run — pass --slskd-api-key, "
            "or --slskd-username and --slskd-password.",
            file=sys.stderr,
        )
        return 2

    if not args.username:
        print("error: --username (or PYMIX_USERNAME) is required.", file=sys.stderr)
        return 2
    if not args.password:
        print("error: --password (or PYMIX_PASSWORD) is required.", file=sys.stderr)
        return 2

    navidrome_url = args.navidrome_url or default_navidrome_url(args.username)
    if not args.watch:
        return run_once(args, navidrome_url)

    # --watch: loop forever, surviving per-pass errors, until the user interrupts.
    print(f"watch mode: checking the wishlist every {args.interval:.0f}s (Ctrl-C to stop)\n")
    while True:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== pass @ {started} ===")
        try:
            run_once(args, navidrome_url)
        except RuntimeError as exc:
            # run_once already handles its own expected failures; this is a backstop
            # so an unexpected one ends the pass, not the watcher.
            print(f"  ! pass failed: {exc}", file=sys.stderr)
        try:
            print(f"\nsleeping {args.interval:.0f}s until next pass…\n")
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopping watch.")
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted.")
        raise SystemExit(130)
