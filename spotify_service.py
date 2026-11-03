"""Spotify: reprodukcija, povijest slušanja i playliste.

Aplikacija radi u Spotify Development Modeu, gdje (stanje 11/2026):
- search vraća najviše 10 rezultata po stranici,
- artist top-tracks i batch /artists vraćaju 403,
- objekt izvođača više nema polja genres ni popularity.

Zato se pjesme izvođača skupljaju iz pretrage i albuma, a žanrove izvođača
procjenjuje jezični model i sprema ih u tablicu artist_cache.
"""

import json
import math
import os
import random
import re
import sqlite3
import time as time_module
import unicodedata
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import spotipy
from spotipy.oauth2 import SpotifyOAuth


DB_FILE = "memory.db"
TIME_ZONE = os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Europe/Zagreb")
GENRE_MODEL = os.getenv("SPOTIFY_GENRE_MODEL", "gpt-5-mini")

SCOPE = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "user-top-read",
    "user-library-read",
    "user-library-modify",
    "playlist-modify-private",
    "playlist-modify-public",
    "playlist-read-private",
    "user-read-private",
])

SEARCH_PAGE = 10
HISTORY_SYNC_MINUTES = 10
LIKED_CACHE_SECONDS = 600
GENRE_BATCH_SIZE = 80

# Oznaka koju model za žanrove dodaje izvođačima s prostora bivše
# Jugoslavije; koristi se za "domaće".
DOMESTIC_TAG = "ex-yu"

# Inačice iste pjesme koje u playlisti izvođača ne želimo dok ima
# dovoljno studijskih snimaka.
VERSION_WORDS = re.compile(
    r"\b(live|remix|demo|instrumental|acoustic|edit|version|session|"
    r"remaster(ed)?|mix|rehearsal|bbc|commentary)\b"
)

_client = None
_openai = None
_last_history_sync = None
_last_device = None
_liked_cache = {"at": 0.0, "tracks": []}


def sp():
    global _client

    if _client is None:
        _client = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=os.getenv("SPOTIFY_CLIENT_ID"),
                client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
                redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
                scope=SCOPE,
                cache_path=".spotify_cache",
            ),
            retries=3,
        )

    return _client


# =========================================================
# POMOĆNE
# =========================================================

def _fold(value):
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("đ", "d")


def _key(value):
    """Usporedba imena bez razmaka, interpunkcije i dijakritika."""
    return re.sub(r"[^a-z0-9]", "", _fold(value))


def _base_title(name):
    """'Starburster - Live at X' i 'Starburster (Remastered)' → 'starburster'."""
    title = re.split(r" - |\(|\[", name or "", maxsplit=1)[0]
    return _key(title) or _key(name)


def _is_version(name):
    rest = (name or "")[len(re.split(r" - |\(|\[", name or "", maxsplit=1)[0]):]
    return bool(VERSION_WORDS.search(_fold(rest)))


def _artists_text(track):
    return ", ".join(a.get("name", "") for a in track.get("artists", []))


def _track_line(track):
    return f"{_artists_text(track)} - {track.get('name', '')}"


def _simple(track):
    artists = track.get("artists") or [{}]
    return {
        "id": track.get("id"),
        "uri": track.get("uri"),
        "name": track.get("name", ""),
        "artist": artists[0].get("name", ""),
        "artist_id": artists[0].get("id"),
    }


def _search(query, kind, offset=0):
    try:
        result = sp().search(q=query, type=kind, limit=SEARCH_PAGE, offset=offset)
    except Exception as error:
        print("SPOTIFY SEARCH:", query, error)
        return []

    return (result.get(f"{kind}s") or {}).get("items") or []


def _local_zone():
    return ZoneInfo(TIME_ZONE)


# =========================================================
# UREĐAJI I REPRODUKCIJA
# =========================================================

def _device():
    global _last_device

    try:
        devices = sp().devices().get("devices", [])
    except Exception:
        devices = []

    for device in devices:
        if device.get("is_active"):
            _last_device = device
            return device

    if _last_device and any(d.get("id") == _last_device.get("id") for d in devices):
        return _last_device

    if devices:
        _last_device = devices[0]
        return devices[0]

    # Spotify nakon pauze ponekad privremeno ne vraća uređaj, a zadnji
    # poznati uređaj i dalje prihvaća naredbe.
    return _last_device


def current_track():
    try:
        playback = sp().current_playback()
    except Exception as error:
        return f"Spotify greška: {error}"

    if not playback or not playback.get("item"):
        return "Trenutno ništa ne svira."

    track = playback["item"]
    state = "Svira" if playback.get("is_playing") else "Pauzirano"
    device = (playback.get("device") or {}).get("name", "")
    return f"{state}: {_track_line(track)}" + (f" ({device})" if device else "")


def _player_call(action, success):
    device = _device()

    try:
        action(device["id"] if device else None)
        return success
    except spotipy.SpotifyException as error:
        if error.http_status == 404:
            return "Ne vidim aktivan Spotify uređaj. Otvori Spotify na nekom uređaju."
        return f"Spotify greška: {error.msg}"
    except Exception as error:
        return f"Spotify greška: {error}"


def pause():
    return _player_call(lambda d: sp().pause_playback(device_id=d), "Pauzirano.")


def resume():
    return _player_call(lambda d: sp().start_playback(device_id=d), "Nastavljam.")


def next_track():
    return _player_call(lambda d: sp().next_track(device_id=d), "Sljedeća pjesma.")


def previous_track():
    return _player_call(lambda d: sp().previous_track(device_id=d), "Prethodna pjesma.")


def set_volume(percent):
    percent = max(0, min(int(percent), 100))
    return _player_call(
        lambda d: sp().volume(percent, device_id=d),
        f"Glasnoća {percent}%.",
    )


def set_shuffle(enabled):
    return _player_call(
        lambda d: sp().shuffle(bool(enabled), device_id=d),
        "Shuffle uključen." if enabled else "Shuffle isključen.",
    )


def devices():
    try:
        items = sp().devices().get("devices", [])
    except Exception as error:
        return f"Spotify greška: {error}"

    if not items:
        return "Ne vidim Spotify uređaje."

    return "\n".join(
        f"• {d['name']} ({d['type']}{', aktivan' if d.get('is_active') else ''})"
        for d in items
    )


def play(query, kind="track"):
    """Pusti pjesmu, izvođača, album ili playlistu."""
    kind = kind if kind in ("track", "artist", "album", "playlist") else "track"

    if kind == "track":
        track = find_track(query)
        if not track:
            return f"Nisam pronašao pjesmu: {query}"
        return _player_call(
            lambda d: sp().start_playback(device_id=d, uris=[track["uri"]]),
            f"Puštam {_track_line(track)}.",
        )

    if kind == "artist":
        item = find_artist(query)
    else:
        items = _search(query, kind)
        item = items[0] if items else None

    if not item:
        return f"Nisam pronašao: {query}"

    return _player_call(
        lambda d: sp().start_playback(device_id=d, context_uri=item["uri"]),
        f"Puštam {item.get('name', query)}.",
    )


def get_queue(limit=10):
    try:
        queue = sp().queue().get("queue", [])
    except Exception as error:
        return f"Spotify greška: {error}"

    if not queue:
        return "Red je prazan."

    return "\n".join(
        f"{i}. {_track_line(t)}" for i, t in enumerate(queue[:int(limit)], 1)
    )


def add_to_queue(tracks):
    """tracks: popis 'Izvođač - Naslov'; dodaju se redom."""
    if isinstance(tracks, str):
        tracks = [tracks]

    added, missing = [], []
    device = _device()

    for query in tracks:
        track = find_track(query)
        if not track:
            missing.append(query)
            continue
        try:
            sp().add_to_queue(track["uri"], device_id=device["id"] if device else None)
            added.append(_track_line(track))
        except spotipy.SpotifyException as error:
            if error.http_status == 404:
                return "Ne vidim aktivan Spotify uređaj. Pokreni reprodukciju pa pokušaj ponovno."
            missing.append(query)

    lines = [f"Dodano u red ({len(added)}):"] + [f"• {x}" for x in added]
    if missing:
        lines += ["Nisam pronašao:"] + [f"• {x}" for x in missing]
    return "\n".join(lines)


# =========================================================
# PRETRAGA
# =========================================================

def find_artist(name):
    """Pronađi izvođača; podnosi tipfelere ('fontained dc')."""
    wanted = _key(name)
    candidates = _search(f'artist:"{name}"', "artist") or _search(name, "artist")

    for artist in candidates:
        if _key(artist.get("name")) == wanted:
            return artist

    return candidates[0] if candidates else None


def find_track(query):
    """query: 'Izvođač - Naslov' ili slobodan tekst."""
    query = (query or "").strip()
    if not query:
        return None

    artist, _, title = query.partition(" - ")

    if title:
        attempts = [
            (f'track:"{title}" artist:"{artist}"', artist),
            (f"{artist} {title}", artist),
            # Korisnik ponekad napiše 'Naslov - Izvođač'.
            (f'track:"{artist}" artist:"{title}"', title),
        ]
    else:
        attempts = [(query, None)]

    for search_query, expected_artist in attempts:
        items = _search(search_query, "track")
        if not items:
            continue
        if not expected_artist:
            return items[0]
        wanted = _key(expected_artist)
        for track in items:
            names = [_key(a.get("name")) for a in track.get("artists", [])]
            if any(wanted in n or n in wanted for n in names if n):
                return track

    return None


def artist_tracks(artist, count):
    """Do `count` različitih pjesama izvođača, popularnije prve.

    Pretraga vraća pjesme po relevantnosti (≈ popularnosti); kad ne
    dostaje, nadopunjava se pjesmama s albuma.
    """
    artist_id = artist["id"]
    name = artist["name"]
    count = max(1, min(int(count), 100))

    found, versions, seen = [], [], set()

    def consider(track):
        if not track or not track.get("uri"):
            return
        if artist_id not in {a.get("id") for a in track.get("artists", [])}:
            return
        base = _base_title(track.get("name"))
        if base in seen:
            return
        if _is_version(track.get("name")):
            versions.append(track)
            return
        seen.add(base)
        found.append(track)

    for query in (f'artist:"{name}"', name):
        for offset in range(0, 100, SEARCH_PAGE):
            items = _search(query, "track", offset)
            if not items:
                break
            for track in items:
                consider(track)
            if len(found) >= count:
                return found[:count]

    try:
        albums = sp().artist_albums(
            artist_id, include_groups="album,single", limit=10
        ).get("items", [])
    except Exception as error:
        print("SPOTIFY ALBUMS:", error)
        albums = []

    for album in albums:
        try:
            items = sp().album_tracks(album["id"], limit=50).get("items", [])
        except Exception:
            continue
        for track in items:
            consider(track)
        if len(found) >= count:
            return found[:count]

    # Tek kad studijskih snimaka nema dovoljno, uzimaju se live/remix inačice.
    for track in versions:
        base = _base_title(track.get("name"))
        if base not in seen:
            seen.add(base)
            found.append(track)
        if len(found) >= count:
            break

    return found[:count]


# =========================================================
# PLAYLISTE
# =========================================================

def create_playlist(name, tracks, description="Pero AI"):
    uris = []
    for track in tracks:
        uri = track.get("uri")
        if uri and uri not in uris:
            uris.append(uri)

    if not uris:
        return "Nisam pronašao nijednu pjesmu, playlista nije napravljena."

    try:
        playlist = sp().current_user_playlist_create(
            name=name, public=False, description=description[:300]
        )
        for i in range(0, len(uris), 100):
            sp().playlist_add_items(playlist["id"], uris[i:i + 100])
    except Exception as error:
        return f"Playlista nije napravljena: {error}"

    url = playlist.get("external_urls", {}).get("spotify", "")
    return f"Playlista '{name}' napravljena ({len(uris)} pjesama).\n{url}"


def _interleave(groups):
    result = []
    for i in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if i < len(group):
                result.append(group[i])
    return result


def playlist_from_artists(artists, name=None, mix=True):
    """artists: [{"name": "Fontaines D.C.", "count": 20}, ...]"""
    groups, lines, missing = [], [], []

    for spec in artists:
        artist = find_artist(spec.get("name", ""))
        if not artist:
            missing.append(spec.get("name", ""))
            continue
        tracks = artist_tracks(artist, spec.get("count") or 10)
        groups.append(tracks)
        lines.append(f"{artist['name']}: {len(tracks)} pjesama")

    if not any(groups):
        return "Nisam pronašao izvođače: " + ", ".join(missing)

    tracks = _interleave(groups) if mix else [t for g in groups for t in g]
    name = name or " / ".join(l.split(":")[0] for l in lines)[:90]

    result = create_playlist(name, tracks, "Pero AI: odabrani izvođači")
    report = [result, ""] + lines
    if missing:
        report.append("Nisam pronašao: " + ", ".join(missing))
    return "\n".join(report)


def playlist_from_tracks(tracks, name=None):
    """tracks: ['Izvođač - Naslov', ...], redoslijed se zadržava."""
    found, missing = [], []

    for query in tracks:
        track = find_track(query)
        if track:
            found.append(track)
        else:
            missing.append(query)

    result = create_playlist(name or "Pero AI playlista", found, "Pero AI: zadani popis")
    if missing:
        result += "\nNisam pronašao:\n" + "\n".join(f"• {m}" for m in missing)
    return result


def add_tracks_to_existing_playlist(playlist_name, tracks):
    playlist = _find_own_playlist(playlist_name)
    if not playlist:
        return f"Nemam playlistu '{playlist_name}'."

    found, missing = [], []
    for query in tracks:
        track = find_track(query)
        (found if track else missing).append(track or query)

    if found:
        sp().playlist_add_items(playlist["id"], [t["uri"] for t in found])

    result = f"U '{playlist['name']}' dodano {len(found)} pjesama."
    if missing:
        result += "\nNisam pronašao:\n" + "\n".join(f"• {m}" for m in missing)
    return result


def _find_own_playlist(name):
    wanted = _key(name)
    offset = 0
    while True:
        page = sp().current_user_playlists(limit=50, offset=offset)
        for playlist in page.get("items", []):
            if playlist and _key(playlist.get("name")) == wanted:
                return playlist
        if not page.get("next"):
            return None
        offset += 50


# =========================================================
# LIKED SONGS
# =========================================================

def liked_tracks():
    if time_module.time() - _liked_cache["at"] < LIKED_CACHE_SECONDS:
        return _liked_cache["tracks"]

    tracks, offset = [], 0
    while True:
        page = sp().current_user_saved_tracks(limit=50, offset=offset)
        for item in page.get("items", []):
            if item.get("track"):
                tracks.append(_simple(item["track"]))
        if not page.get("next"):
            break
        offset += 50

    _liked_cache.update(at=time_module.time(), tracks=tracks)
    return tracks


def list_liked(limit=20):
    tracks = liked_tracks()[:int(limit)]
    if not tracks:
        return "Nema Liked Songs."
    return "\n".join(f"{i}. {t['artist']} - {t['name']}" for i, t in enumerate(tracks, 1))


# =========================================================
# ŽANROVI (procjena modelom, cache u artist_cache)
# =========================================================

def _cached_genres(artist_ids):
    if not artist_ids:
        return {}
    conn = sqlite3.connect(DB_FILE)
    marks = ",".join("?" * len(artist_ids))
    rows = conn.execute(
        f"SELECT artist_id, genres FROM artist_cache WHERE artist_id IN ({marks})",
        list(artist_ids),
    ).fetchall()
    conn.close()

    result = {}
    for artist_id, genres in rows:
        try:
            values = json.loads(genres or "[]")
        except Exception:
            values = []
        # Prazne liste ostale su iz vremena kad ih je davao Spotify.
        if values:
            result[artist_id] = values
    return result


def _classify_artists(names):
    global _openai

    from openai import OpenAI

    if _openai is None:
        _openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = _openai.responses.create(
        model=GENRE_MODEL,
        instructions=(
            "Za svakog glazbenog izvođača vrati 1-4 žanra, malim slovima, "
            "na engleskom, od užeg prema širem (npr. 'post-punk', 'punk', "
            "'indie rock', 'rock', 'synth-pop', 'jazz', 'hip hop', "
            "'electronic', 'pop', 'folk', 'chanson', 'metal'). Ako je "
            f"izvođač s prostora bivše Jugoslavije, dodaj i '{DOMESTIC_TAG}'. "
            "Ako izvođača ne poznaješ, vrati ['unknown']. "
            'Odgovori samo JSON objektom {"artists": {"<ime>": [žanrovi]}}.'
        ),
        input="Izvođači (odgovor u JSON obliku):\n" + "\n".join(names),
        text={"format": {"type": "json_object"}},
    )
    data = json.loads(response.output_text or "{}")
    return {
        name: [str(g).lower().strip() for g in genres if str(g).strip()]
        for name, genres in (data.get("artists") or {}).items()
        if isinstance(genres, list)
    }


def artist_genres(artists):
    """artists: {artist_id: ime}. Vraća {artist_id: [žanrovi]}."""
    genres = _cached_genres(set(artists))
    missing = {aid: name for aid, name in artists.items() if aid not in genres}
    if not missing:
        return genres

    items = list(missing.items())
    conn = sqlite3.connect(DB_FILE)
    for i in range(0, len(items), GENRE_BATCH_SIZE):
        batch = dict(items[i:i + GENRE_BATCH_SIZE])
        try:
            by_name = {_key(n): g for n, g in _classify_artists(list(batch.values())).items()}
        except Exception as error:
            print("SPOTIFY ŽANROVI:", error)
            continue
        for artist_id, name in batch.items():
            values = by_name.get(_key(name)) or ["unknown"]
            genres[artist_id] = values
            conn.execute(
                """
                INSERT OR REPLACE INTO artist_cache (artist_id, artist_name, genres, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (artist_id, name, json.dumps(values), datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    conn.close()
    return genres


def _genre_matches(genres, wanted):
    """'punk' odgovara 'punk', 'punk rock', 'hardcore punk', ali ne 'post-punk'."""
    wanted = _fold(wanted).strip()
    for genre in genres:
        genre = _fold(genre)
        if genre == wanted or genre.startswith(wanted + " ") or genre.endswith(" " + wanted):
            return True
    return False


# =========================================================
# PLAYLISTA IZ LIKED SONGS
# =========================================================

def playlist_from_liked(
    name=None,
    track_count=30,
    genres=None,
    domestic="any",
    artists=None,
    mode="mixed",
    avoid_recent_days=None,
    max_per_artist=3,
):
    """
    genres: [{"name": "punk", "weight": 5}, ...] — udio pjesama po žanru.
    domestic: any | prefer | only | exclude
    artists: ograniči na ove izvođače iz Liked Songs
    mode: mixed | favorites (najslušanije) | forgotten (dugo neslušane)
    """
    sync_history_if_needed()

    track_count = max(1, min(int(track_count or 30), 100))
    max_per_artist = max(1, int(max_per_artist or 3))
    genres = [g for g in (genres or []) if g.get("name")]

    tracks = liked_tracks()
    if not tracks:
        return "Nema Liked Songs."

    if artists:
        wanted = {_key(a) for a in artists}
        tracks = [t for t in tracks if _key(t["artist"]) in wanted]
        if not tracks:
            return "Među Liked Songs nema pjesama tih izvođača."

    needs_genres = bool(genres) or domestic != "any"
    artist_map = {t["artist_id"]: t["artist"] for t in tracks if t["artist_id"]}
    genre_map = artist_genres(artist_map) if needs_genres else {}
    if needs_genres and not genre_map:
        return "Ne mogu trenutno odrediti žanrove izvođača (OpenAI greška). Pokušaj ponovno."

    total_counts = play_counts()
    max_total = max(total_counts.values(), default=1)
    last_played = last_play_dates()
    now = datetime.now(timezone.utc)

    def score(track):
        plays = total_counts.get(track["id"], 0)
        last = last_played.get(track["id"])
        days = (now - last).days if last else 365
        value = random.random()
        if mode == "favorites":
            value += 3 * math.log1p(plays) / math.log1p(max_total)
        elif mode == "forgotten":
            value += 3 * min(days / 180, 1)
        if domestic == "prefer" and DOMESTIC_TAG in genre_map.get(track["artist_id"], []):
            value += 1.5
        return value

    pool = []
    for track in tracks:
        artist_genre_list = genre_map.get(track["artist_id"], [])
        is_domestic = DOMESTIC_TAG in artist_genre_list
        if domestic == "only" and not is_domestic:
            continue
        if domestic == "exclude" and is_domestic:
            continue
        if avoid_recent_days:
            last = last_played.get(track["id"])
            if last and (now - last).days < int(avoid_recent_days):
                continue
        pool.append({**track, "_genres": artist_genre_list, "_score": score(track)})

    pool.sort(key=lambda t: t["_score"], reverse=True)

    if genres:
        total_weight = sum(max(float(g.get("weight") or 1), 0.1) for g in genres)
        quotas = [
            (g["name"], round(track_count * max(float(g.get("weight") or 1), 0.1) / total_weight))
            for g in genres
        ]
    else:
        quotas = [(None, track_count)]

    selected, used, per_artist = [], set(), {}

    def take(genre_name, limit):
        taken = 0
        for track in pool:
            if taken >= limit or len(selected) >= track_count:
                break
            if track["uri"] in used:
                continue
            if genre_name and not _genre_matches(track["_genres"], genre_name):
                continue
            artist = _key(track["artist"])
            if per_artist.get(artist, 0) >= max_per_artist:
                continue
            selected.append(track)
            used.add(track["uri"])
            per_artist[artist] = per_artist.get(artist, 0) + 1
            taken += 1
        return taken

    report = []
    for genre_name, quota in quotas:
        taken = take(genre_name, quota)
        if genre_name:
            report.append(f"{genre_name}: {taken}")

    # Ako jednom žanru nedostaje pjesama, mjesto popunjavaju ostali traženi žanrovi.
    for genre_name, _ in quotas:
        if len(selected) >= track_count:
            break
        take(genre_name, track_count - len(selected))

    if not selected:
        return "U Liked Songs nema pjesama koje odgovaraju tim kriterijima."

    # Pjesme istog izvođača ne idu jedna za drugom.
    random.shuffle(selected)
    for i in range(1, len(selected)):
        if selected[i]["artist"] == selected[i - 1]["artist"]:
            for j in range(i + 1, len(selected)):
                if selected[j]["artist"] != selected[i - 1]["artist"]:
                    selected[i], selected[j] = selected[j], selected[i]
                    break

    name = name or "Pero AI: " + (", ".join(g["name"] for g in genres) or "Liked mix")
    result = create_playlist(name, selected, "Pero AI: iz Liked Songs")
    if len(selected) < track_count:
        result += f"\nPronašao sam samo {len(selected)} od {track_count} traženih pjesama."
    if report:
        result += "\nPo žanru: " + ", ".join(report)
    return result


# =========================================================
# SLIČNI IZVOĐAČI
# =========================================================

def _known_artist_ids(min_liked=2):
    counts = {}
    for track in liked_tracks():
        if track["artist_id"]:
            counts[track["artist_id"]] = counts.get(track["artist_id"], 0) + 1
    known = {aid for aid, n in counts.items() if n >= min_liked}

    for time_range in ("short_term", "medium_term", "long_term"):
        try:
            for artist in sp().current_user_top_artists(limit=50, time_range=time_range)["items"]:
                known.add(artist["id"])
        except Exception as error:
            print("SPOTIFY TOP ARTISTS:", error)
    return known


def similar_artists(
    seed_artist,
    candidate_artists,
    artist_count=5,
    tracks_per_artist=2,
    only_new=True,
    create=False,
    name=None,
):
    """Kandidate predlaže model; Python provjerava jesu li stvarno novi."""
    artist_count = max(1, min(int(artist_count), 15))
    tracks_per_artist = max(1, min(int(tracks_per_artist), 10))

    seed = find_artist(seed_artist)
    excluded = {seed["id"]} if seed else set()
    if only_new:
        excluded |= _known_artist_ids()

    chosen, skipped = [], []
    for candidate in candidate_artists:
        if len(chosen) >= artist_count:
            break
        artist = find_artist(candidate)
        if not artist or artist["id"] in {a["id"] for a, _ in chosen}:
            continue
        if artist["id"] in excluded:
            skipped.append(artist["name"])
            continue
        tracks = artist_tracks(artist, tracks_per_artist)
        if tracks:
            chosen.append((artist, tracks))

    if not chosen:
        return "Nisam pronašao nove izvođače. Probaj s drugim izvođačem."

    lines = [f"Slično kao {seed['name'] if seed else seed_artist}:"]
    for i, (artist, tracks) in enumerate(chosen, 1):
        lines.append(f"{i}. {artist['name']}: " + ", ".join(t["name"] for t in tracks))

    if create:
        playlist_tracks = _interleave([tracks for _, tracks in chosen])
        lines.append("")
        lines.append(create_playlist(
            name or f"Slično kao {seed['name'] if seed else seed_artist}",
            playlist_tracks,
            "Pero AI: slični izvođači",
        ))

    if len(chosen) < artist_count:
        lines.append(f"\nNašao sam {len(chosen)} od {artist_count} traženih novih izvođača.")
    if skipped:
        lines.append("Preskočeni jer ih već slušaš: " + ", ".join(skipped[:10]))
    return "\n".join(lines)


# =========================================================
# TOP ITEMS
# =========================================================

TIME_RANGES = {
    "short_term": "zadnja 4 tjedna",
    "medium_term": "zadnjih 6 mjeseci",
    "long_term": "dugoročno",
}


def top_tracks(time_range="short_term", limit=10):
    time_range = time_range if time_range in TIME_RANGES else "short_term"
    items = sp().current_user_top_tracks(limit=min(int(limit), 50), time_range=time_range)["items"]
    if not items:
        return "Spotify nema dovoljno podataka."
    return f"Top pjesme ({TIME_RANGES[time_range]}):\n" + "\n".join(
        f"{i}. {_track_line(t)}" for i, t in enumerate(items, 1)
    )


def top_artists(time_range="short_term", limit=10):
    time_range = time_range if time_range in TIME_RANGES else "short_term"
    items = sp().current_user_top_artists(limit=min(int(limit), 50), time_range=time_range)["items"]
    if not items:
        return "Spotify nema dovoljno podataka."
    return f"Top izvođači ({TIME_RANGES[time_range]}):\n" + "\n".join(
        f"{i}. {a['name']}" for i, a in enumerate(items, 1)
    )


# =========================================================
# POVIJEST SLUŠANJA
# =========================================================

def sync_history():
    global _last_history_sync

    try:
        items = sp().current_user_recently_played(limit=50).get("items", [])
    except Exception as error:
        return f"Sinkronizacija povijesti nije uspjela: {error}"

    conn = sqlite3.connect(DB_FILE)
    new = 0
    for item in items:
        track, played_at = item.get("track"), item.get("played_at")
        if not track or not played_at:
            continue
        simple = _simple(track)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO listening_history
                (track_id, track_uri, track_name, artist_name, artist_id, album_name, played_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                simple["id"], simple["uri"], simple["name"], simple["artist"],
                simple["artist_id"], (track.get("album") or {}).get("name", ""), played_at,
            ),
        )
        new += cursor.rowcount
    conn.commit()
    conn.close()

    _last_history_sync = datetime.now(timezone.utc)
    return f"Provjereno {len(items)} nedavnih slušanja, novih {new}."


def sync_history_if_needed():
    if (
        _last_history_sync is None
        or datetime.now(timezone.utc) - _last_history_sync
        >= timedelta(minutes=HISTORY_SYNC_MINUTES)
    ):
        sync_history()


def play_counts():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT track_id, COUNT(*) FROM listening_history WHERE track_id IS NOT NULL GROUP BY track_id"
    ).fetchall()
    conn.close()
    return dict(rows)


def last_play_dates():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT track_id, MAX(played_at) FROM listening_history WHERE track_id IS NOT NULL GROUP BY track_id"
    ).fetchall()
    conn.close()
    return {
        track_id: datetime.fromisoformat(value.replace("Z", "+00:00"))
        for track_id, value in rows
        if value
    }


def _utc_bound(local_date, next_day=False):
    # played_at je UTC ("...Z"), a korisnik pita po lokalnim danima. Granica
    # se pretvara u UTC; za kraj perioda uzima se početak sljedećeg dana.
    day = datetime.fromisoformat(str(local_date)[:10]).date()
    if next_day:
        day += timedelta(days=1)
    local_start = datetime.combine(day, time.min, tzinfo=_local_zone())
    return local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _period_filter(start_date, end_date):
    clause, params = "", []
    if start_date:
        clause += " AND played_at >= ?"
        params.append(_utc_bound(start_date))
    if end_date:
        clause += " AND played_at < ?"
        params.append(_utc_bound(end_date, next_day=True))
    return clause, params


def history_stats(start_date=None, end_date=None, limit=10, group_by="track"):
    sync_history_if_needed()
    clause, params = _period_filter(start_date, end_date)

    if group_by == "artist":
        select, group = "artist_name, '', COUNT(*)", "artist_name"
    else:
        select, group = "artist_name, track_name, COUNT(*)", "track_id, track_name, artist_name"

    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        f"SELECT {select} FROM listening_history WHERE 1=1 {clause} "
        f"GROUP BY {group} ORDER BY COUNT(*) DESC LIMIT ?",
        params + [min(int(limit), 50)],
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM listening_history WHERE 1=1 {clause}", params
    ).fetchone()[0]
    conn.close()

    if not rows:
        return "Nemam zabilježenih slušanja za taj period."

    lines = [f"Ukupno slušanja: {total}"]
    for i, (artist, track, plays) in enumerate(rows, 1):
        label = f"{artist} - {track}" if track else artist
        lines.append(f"{i}. {label}: {plays}×")
    return "\n".join(lines)


def history_list(start_date, end_date=None, limit=50, create=False, name=None):
    sync_history_if_needed()
    limit = max(1, min(int(limit or 50), 200))

    # Bez kraja perioda misli se na jedan dan ("što sam slušao jučer").
    end_date = end_date or start_date
    clause, params = _period_filter(start_date, end_date)

    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT played_at, track_name, artist_name, track_uri FROM listening_history "
        f"WHERE 1=1 {clause} ORDER BY played_at ASC",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return "Nemam zabilježenih slušanja za taj period."

    if create:
        label = start_date if end_date == start_date else f"{start_date} – {end_date}"
        return create_playlist(
            name or f"Slušano {label}",
            [{"uri": uri} for _, _, _, uri in rows],
            "Pero AI: iz povijesti slušanja",
        )

    zone = _local_zone()
    lines = [f"Ukupno slušanja: {len(rows)}" + (f" (zadnjih {limit})" if len(rows) > limit else "")]
    for played_at, track, artist, _ in rows[-limit:]:
        local = datetime.fromisoformat(played_at.replace("Z", "+00:00")).astimezone(zone)
        lines.append(f"{local:%d.%m. %H:%M} {artist} - {track}")
    return "\n".join(lines)
