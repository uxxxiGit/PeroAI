import os
import re
import json
import math
import random
import sqlite3
import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import spotipy
from dotenv import load_dotenv
from openai import OpenAI
from spotipy.oauth2 import SpotifyOAuth
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import calendar_service
import rijeka_news_service
import stripovi_forum_service
import tasks_service

# =========================================================
# ENV
# =========================================================

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")


# =========================================================
# CONFIG
# =========================================================

DB_FILE = "memory.db"
BOT_VERSION = "2026.11.03-history-v10"

# Kratka neposredna povijest. Trajna memorija se dohvaća odvojeno i samo kada
# je povezana s aktualnim pitanjem, kako se nepotrebno ne troše tokeni.
MAX_MESSAGES = 8
MAX_MESSAGE_CHARS = 900
MAX_MEMORY_ITEMS = 5
MAX_MEMORY_CONTEXT_CHARS = 1_200

MEMORY_STOP_WORDS = {
    "ako", "ali", "bio", "bih", "bila", "bilo", "biti", "da", "do", "ga",
    "gdje", "i", "imam", "ima", "iz", "ja", "je", "li", "mi", "na", "ne",
    "nije", "o", "od", "po", "sa", "se", "si", "su", "što", "te", "to",
    "u", "za", "će", "ću", "što", "sto", "kada", "kako", "koji", "koja", "koje",
}

MEMORY_CATEGORY_PREFIXES = {
    # Korijeni riječi pokrivaju hrvatske padeže i glagolske oblike: npr.
    # vozim/voziš/vozilo/vožnja ili pjesma/pjesme/pjesmu.
    "music": {"spotify", "glazb", "pjesm", "izvod", "bend", "playlist"},
    "vehicle": {"auto", "automobil", "voz", "dacia", "goriv", "lpg", "putov"},
    "work": {"posao", "posl", "projekt", "klijent", "edi", "sql", "racun", "ponud", "mail", "email"},
    "planning": {"kalendar", "sastan", "termin", "zadat", "podsjet", "obvez", "plan", "rodendan"},
    "preferences": {"prefer", "vol", "zel", "odgovor", "stil", "jezik"},
    "profile": {"ime", "ziv", "grad", "profil", "godin"},
    "household": {"obitelj", "suprug", "zen", "djec", "sin", "kcer", "kuc"},
    "family": {"obitelj", "suprug", "zen", "djec", "sin", "kcer"},
    "pets": {"ljubim", "pas", "mack", "zivotinj"},
}

CHEAP_MODEL = "gpt-5-nano"
SMART_MODEL = "gpt-5-mini"
RIJEKA_NEWS_MODEL = os.getenv("NK_RIJEKA_NEWS_MODEL", SMART_MODEL)
RIJEKA_SEARCH_MODEL = os.getenv("NK_RIJEKA_SEARCH_MODEL", "gpt-4.1-mini")
RIJEKA_NEWS_HOUR = 8
RIJEKA_NEWS_MINUTE = 30
STRIPOVI_FORUM_MODEL = os.getenv("STRIPOVI_FORUM_MODEL", SMART_MODEL)
STRIPOVI_FORUM_HOUR = 8
STRIPOVI_FORUM_MINUTE = 40

HISTORY_SYNC_MINUTES = 10
LAST_HISTORY_SYNC = None

# Spotify vraća samo zadnjih 50 slušanja (~2,5 h), pa se povijest mora
# dohvaćati u pozadini, neovisno o tome piše li korisnik botu.
HISTORY_BACKGROUND_SYNC_MINUTES = 20

# Discovery:
DISCOVERY_CANDIDATE_COUNT = 20

# Ako imaš >= ovoliko Liked Songs određenog izvođača,
# Discovery ga smatra "poznatim / omiljenim".
DISCOVERY_LIKED_THRESHOLD = 2

# Spotify nakon pauziranja ponekad privremeno prestane vraćati uređaj kroz
# Connect API. Zato tijekom rada bota pamtimo zadnji uređaj na kojem je sviralo.
LAST_SPOTIFY_DEVICE = None


# =========================================================
# OPENAI
# =========================================================

client = OpenAI(
    api_key=OPENAI_API_KEY
)


SYSTEM_PROMPT = """
Ti si korisnikov osobni AI agent za Spotify, Google Calendar i Google Tasks.

Odgovaraj kratko, prirodno i na hrvatskom.

Za Spotify radnje koristi alate.

GOOGLE CALENDAR:

Za pitanja o rasporedu uvijek koristi kalendarske alate i nemoj izmišljati
događaje. Za relativne datume (danas, sutra, u petak) koristi trenutni datum
iz dodatne upute. Vremena šalji kao ISO 8601 s vremenskom zonom Europe/Zagreb.
Za upit koji obuhvaća cijeli dan, mjesec ili drugo datumsko razdoblje početak
mora biti u 00:00 lokalnog prvog dana; nikada nemoj kao početak poslati
trenutačni sat jer bi cjelodnevni događaji za današnji dan bili izostavljeni.
Za upite o rođendanima uvijek koristi calendar_list_birthdays.
Ako rezultat tog alata sadrži stavke u polju today, uvijek ih navedi prve i
nemoj ih izostaviti zato što su već počele ili zato što korisnik pita za
"nadolazeće" rođendane u tekućem mjesecu.
Prije izmjene ili brisanja prvo pronađi događaj i njegov event_id.
Kreiranje, izmjenu i brisanje potvrđuje Python dio preko Telegram gumba.

GOOGLE TASKS:

Za pitanja o obvezama i podsjetnicima koristi tasks_list_open ili
tasks_get_due. Za stvaranje, izmjenu i brisanje zadataka najprije prikaži
sažetak i pričekaj potvrdu preko Telegram gumba.

TRAJNA MEMORIJA:

Ispod dodatne upute dobivaš samo trajne podatke povezane s aktualnom porukom.
Koristi ih kada su korisni za odgovor, primjerice za planiranje puta, ali ih ne
ponavljaj bez potrebe. Ako korisnik izričito kaže "zapamti", "zabilježi",
"promijeni" ili "zaboravi", koristi odgovarajući memory alat.

Možeš koristiti memory_capture bez dodatnog pitanja samo za jasnu, korisnu i
trajnu nesenzitivnu činjenicu ili preferenciju koju korisnik navede usput,
primjerice stabilnu preferenciju odgovora, glazbeni ukus, vozilo ili dugotrajan
projekt. Ne koristi ga za svaku poruku, prolazne planove, nagađanja, podatke o
drugim osobama bez potrebe ni osjetljive podatke (lozinke, tokeni, financije,
zdravlje, identifikatori, politika ili vjera). Ne obavještavaj korisnika o
tiho spremljenoj automatskoj memoriji, osim ako te pita što pamtiš. Ako korisnik
pita što pamtiš, upotrijebi memory_list. Kod promjene istog podatka uvijek
prepiši staru vrijednost novom.

DISCOVERY:

Ako korisnik želi izvođače slične nekom izvođaču,
koristi spotify_discover_artists_v2.

VAŽNO:
Nemoj sam zaključivati da izvođač nije među korisnikovim
favoritima. Python alat to provjerava preko Spotify artist_id.

Kada koristiš spotify_discover_artists_v2:

- candidate_artists mora sadržavati 15-25 kandidata
- nemoj uključiti seed artist u candidate_artists
- artist_count je broj koji korisnik želi
- tracks_per_artist je broj pjesama svakog izvođača
- exclude_favorites=true ako korisnik kaže:
  "koje ne slušam", "koje nemam", "novi bendovi",
  "nisu među favoritima", "discovery"
- exclude_top_artists=true za discovery
- min_liked_tracks_to_exclude koristi 2 po defaultu

Primjer:

"Daj mi 5 bendova kao Fontaines D.C. koje ne slušam
i dvije pjesme svakog"

→ seed_artist="Fontaines D.C."
→ candidate_artists=[oko 20 stilistički srodnih izvođača]
→ artist_count=5
→ tracks_per_artist=2
→ exclude_favorites=true
→ exclude_top_artists=true
→ min_liked_tracks_to_exclude=2

Ako korisnik traži playlistu discovery rezultata:
create_playlist=true.

SMART PLAYLIST V3:

Za zahtjeve iz korisnikovih Liked Songs sa soft kriterijima
koristi spotify_create_smart_playlist_v3.

Primjer:
"uglavnom punk, nešto post-punka, preferiraj domaće"
→ genres punk weight 5, post-punk weight 3
→ domestic_weight > 0

Za precizne datume koristi lokalnu listening history bazu:
- "što sam slušao jučer/danas/u subotu" → spotify_history_list
- "napravi playlistu od onog što sam slušao jučer" →
  spotify_history_list s create_playlist=true
- "što sam najviše slušao ovaj tjedan" → spotify_history_stats
Datumi su lokalni (Europe/Zagreb) u obliku YYYY-MM-DD.

Spotify Top Items:
short_term = približno zadnja 4 tjedna
medium_term = približno 6 mjeseci
long_term = dugoročne preference

Nikad ne tvrdi da je radnja uspjela prije rezultata alata.
"""


# =========================================================
# SPOTIFY
# =========================================================

spotify = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=(
            "user-read-playback-state "
            "user-modify-playback-state "
            "user-read-currently-playing "
            "user-read-recently-played "
            "user-top-read "
            "user-library-read "
            "user-library-modify "
            "playlist-modify-private "
            "playlist-modify-public "
            "playlist-read-private "
            "user-read-private"
        ),
        cache_path=".spotify_cache",
    )
)


# =========================================================
# DOMAĆI / REGIONALNI
# =========================================================

DOMESTIC_ARTISTS = [
    "hladno pivo",
    "prljavo kazalište",
    "parni valjak",
    "azra",
    "haustor",
    "film",
    "psihomodo pop",
    "majke",
    "let 3",
    "pips chips & videoclips",
    "elemental",
    "tbf",
    "gibonni",
    "oliver dragojević",
    "darko rundek",
    "aerodrom",
    "daleka obala",
    "vatra",
    "silente",
    "brkovi",
    "kud idijoti",
    "term iti",
    "pankrti",
    "buldožer",
    "idoli",
    "električni orgazam",
    "partibrejkers",
    "disciplina kičme",
    "ekatarina velika",
    "plavi orkestar",
    "zabranjeno pušenje",
    "bijelo dugme",
    "debeli precjednik",
    "mašinko",
    "fakofbolan",
]


GENRE_KEYWORDS = {
    "punk": [
        "punk",
        "punk rock",
        "hardcore",
        "hardcore punk",
        "garage punk",
        "pop punk",
    ],

    "post-punk": [
        "post-punk",
        "post punk",
        "gothic rock",
        "dark wave",
        "darkwave",
        "cold wave",
    ],

    "rock": [
        "rock",
        "alternative rock",
        "indie rock",
        "garage rock",
        "hard rock",
    ],

    "metal": [
        "metal",
        "heavy metal",
        "thrash",
        "doom",
        "death metal",
        "black metal",
    ],

    "electronic": [
        "electronic",
        "electronica",
        "techno",
        "house",
        "ambient",
        "synth",
    ],
}


# =========================================================
# DATABASE
# =========================================================

def init_database():

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS listening_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id TEXT,
            track_uri TEXT,
            track_name TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            artist_id TEXT,
            album_name TEXT,
            played_at TEXT NOT NULL UNIQUE
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS artist_cache (
            artist_id TEXT PRIMARY KEY,
            artist_name TEXT,
            genres TEXT,
            updated_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, category, memory_key)
        )
        """
    )

    # Migracija postojeće user_memory tablice. ALTER TABLE se izvodi samo za
    # stupce kojih nema, pa se dosadašnje zapamćene činjenice zadržavaju.
    memory_columns = {
        row[1] for row in cur.execute("PRAGMA table_info(user_memory)").fetchall()
    }
    for column_name, definition in (
        ("source", "TEXT NOT NULL DEFAULT 'explicit'"),
        ("importance", "INTEGER NOT NULL DEFAULT 3"),
        ("expires_at", "TEXT"),
        ("last_used_at", "TEXT"),
    ):
        if column_name not in memory_columns:
            cur.execute(f"ALTER TABLE user_memory ADD COLUMN {column_name} {definition}")

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_memory_lookup
        ON user_memory(user_id, category, importance DESC, updated_at DESC)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS morning_report_subscribers (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rijeka_news_subscribers (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stripovi_forum_subscribers (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rijeka_news_seen_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            first_sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, url)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rijeka_news_seen
        ON rijeka_news_seen_urls(user_id, last_sent_at DESC)
        """
    )

    # Korisnik koji već prima jutarnji izvještaj automatski dobiva i izričito
    # zatraženi HNK Rijeka pregled. Jednom isključena pretplata ostaje isključena.
    cur.execute(
        """
        INSERT OR IGNORE INTO rijeka_news_subscribers (user_id, enabled)
        SELECT user_id, 1
        FROM morning_report_subscribers
        WHERE enabled = 1
        """
    )

    # Postojeći korisnik jutarnjeg izvještaja automatski dobiva i pregled
    # zatražene sekcije Stripovi. Naredba /bezstripova ostaje zapamćena.
    cur.execute(
        """
        INSERT OR IGNORE INTO stripovi_forum_subscribers (user_id, enabled)
        SELECT user_id, 1
        FROM morning_report_subscribers
        WHERE enabled = 1
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_user
        ON messages(user_id, id)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_history_played
        ON listening_history(played_at)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_history_track
        ON listening_history(track_id)
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_history_artist
        ON listening_history(artist_name)
        """
    )

    conn.commit()
    conn.close()


# =========================================================
# LONG-TERM USER MEMORY
# =========================================================

def _normalise_memory_text(value):
    return " ".join((value or "").strip().lower().split())


def _fold_memory_text(value):
    return _normalise_memory_text(value).translate(
        str.maketrans("čćđšž", "ccdsz")
    )


def _memory_tokens(value):
    return {
        token for token in re.findall(r"[a-z0-9]{3,}", _fold_memory_text(value))
        if token not in MEMORY_STOP_WORDS
    }


def _memory_category_match(category, message_tokens):
    prefixes = MEMORY_CATEGORY_PREFIXES.get(category, set())
    return sum(
        1 for prefix in prefixes
        if any(token.startswith(prefix) for token in message_tokens)
    )


def _memory_is_sensitive(category, memory_key, value):
    text = _normalise_memory_text(f"{category} {memory_key} {value}")
    sensitive_terms = (
        "lozinka", "password", "pin", "api key", "api_key", "token", "oauth",
        "broj kartice", "iban", "oib", "jmbg", "zdravlje", "dijagnoza",
        "lijek", "medicinski", "političk", "religij",
    )
    return any(term in text for term in sensitive_terms)


def save_user_memory(user_id, category, memory_key, value, source="explicit",
                     importance=3, expires_at=None):
    category = category.strip().lower()
    memory_key = memory_key.strip().lower()
    value = value.strip()

    if not category or not memory_key or not value:
        return "Memorija nije spremljena jer nedostaje podatak."

    if _memory_is_sensitive(category, memory_key, value):
        return "Taj osjetljivi podatak ne spremam u trajnu memoriju."

    try:
        importance = max(1, min(int(importance), 5))
    except (TypeError, ValueError):
        importance = 3

    if expires_at:
        try:
            expires_at = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            ).isoformat()
        except ValueError:
            return "Memorija nije spremljena jer datum isteka nije ispravan."

    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        INSERT INTO user_memory
            (user_id, category, memory_key, value, source, importance, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, category, memory_key)
        DO UPDATE SET
            value = excluded.value,
            source = excluded.source,
            importance = excluded.importance,
            expires_at = excluded.expires_at,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, category, memory_key, value, source, importance, expires_at),
    )
    conn.commit()
    conn.close()
    if source == "explicit":
        return f"Zapamtio sam: {memory_key} — {value}"
    return "Korisna, nesenzitivna trajna informacija je spremljena."


def get_user_memory(user_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        """
        SELECT category, memory_key, value, source, importance, expires_at
        FROM user_memory
        WHERE user_id = ?
          AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY category, memory_key
        """,
        (user_id, datetime.now(timezone.utc).isoformat()),
    ).fetchall()
    conn.close()
    return rows


def get_user_memory_context(user_id):
    rows = get_user_memory(user_id)
    if not rows:
        return "Nema spremljenih trajnih podataka o korisniku."

    return "\n".join(
        f"- [{category}] {memory_key}: {value}"
        for category, memory_key, value, _, _, _ in rows
    )


def get_relevant_user_memory_context(user_id, user_message):
    """Vrati najviše nekoliko činjenica koje su povezane s ovom porukom.

    Baza se smije pretražiti cijela (to ne troši API tokene), ali u OpenAI
    uputu ide samo mali, rangirani rezultat.
    """
    query_tokens = _memory_tokens(user_message)
    rows = get_user_memory(user_id)
    ranked = []

    for category, memory_key, value, source, importance, expires_at in rows:
        fact_tokens = _memory_tokens(f"{category} {memory_key} {value}")
        overlap = len(query_tokens & fact_tokens)
        category_match = _memory_category_match(category, query_tokens)
        # Bez podudarnosti se podatak ne šalje samo zato što postoji. Iznimka
        # su vrlo važne eksplicitno spremljene preference, najviše jedna.
        if not overlap and not category_match:
            continue
        score = overlap * 10 + category_match * 4 + int(importance)
        ranked.append((score, category, memory_key, value, source, importance))

    if not ranked:
        important = [
            row for row in rows
            if row[3] == "explicit" and int(row[4]) >= 5
        ]
        ranked = [
            (int(importance), category, memory_key, value, source, importance)
            for category, memory_key, value, source, importance, _ in important[:1]
        ]

    ranked.sort(key=lambda row: row[0], reverse=True)
    selected = ranked[:MAX_MEMORY_ITEMS]
    if not selected:
        return "Nema relevantnih trajnih podataka za ovu poruku."

    lines = []
    used_keys = []
    total_chars = 0
    for _, category, memory_key, value, _, _ in selected:
        line = f"- [{category}] {memory_key}: {value}"
        if total_chars + len(line) > MAX_MEMORY_CONTEXT_CHARS:
            break
        lines.append(line)
        used_keys.append((category, memory_key))
        total_chars += len(line) + 1

    if used_keys:
        conn = sqlite3.connect(DB_FILE)
        conn.executemany(
            """
            UPDATE user_memory
            SET last_used_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND category = ? AND memory_key = ?
            """,
            [(user_id, category, memory_key) for category, memory_key in used_keys],
        )
        conn.commit()
        conn.close()

    return "\n".join(lines) or "Nema relevantnih trajnih podataka za ovu poruku."


def forget_user_memory(user_id, category, memory_key):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute(
        """
        DELETE FROM user_memory
        WHERE user_id = ? AND category = ? AND memory_key = ?
        """,
        (user_id, category.strip().lower(), memory_key.strip().lower()),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount:
        return f"Zaboravio sam podatak: {memory_key}."
    return f"Ne nalazim spremljen podatak: {memory_key}."


# =========================================================
# MORNING REPORT SUBSCRIPTIONS
# =========================================================

def enable_morning_report(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        INSERT INTO morning_report_subscribers (user_id, enabled)
        VALUES (?, 1)
        ON CONFLICT(user_id)
        DO UPDATE SET enabled = 1, updated_at = CURRENT_TIMESTAMP
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def disable_morning_report(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        UPDATE morning_report_subscribers
        SET enabled = 0, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_morning_report_subscribers():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT user_id FROM morning_report_subscribers WHERE enabled = 1"
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


# =========================================================
# HNK RIJEKA DAILY NEWS
# =========================================================

def enable_rijeka_news(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        INSERT INTO rijeka_news_subscribers (user_id, enabled)
        VALUES (?, 1)
        ON CONFLICT(user_id)
        DO UPDATE SET enabled = 1, updated_at = CURRENT_TIMESTAMP
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def disable_rijeka_news(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        INSERT INTO rijeka_news_subscribers (user_id, enabled)
        VALUES (?, 0)
        ON CONFLICT(user_id)
        DO UPDATE SET enabled = 0, updated_at = CURRENT_TIMESTAMP
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_rijeka_news_subscribers():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT user_id FROM rijeka_news_subscribers WHERE enabled = 1"
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_seen_rijeka_news_urls(user_id, limit=40):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        """
        SELECT url
        FROM rijeka_news_seen_urls
        WHERE user_id = ?
        ORDER BY last_sent_at DESC
        LIMIT ?
        """,
        (user_id, max(1, min(int(limit), 100))),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def save_seen_rijeka_news_sources(user_id, sources):
    if not sources:
        return
    conn = sqlite3.connect(DB_FILE)
    conn.executemany(
        """
        INSERT INTO rijeka_news_seen_urls (user_id, url, title)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, url)
        DO UPDATE SET
            title = excluded.title,
            last_sent_at = CURRENT_TIMESTAMP
        """,
        [
            (user_id, source.get("url", ""), source.get("title", ""))
            for source in sources if source.get("url")
        ],
    )
    conn.commit()
    conn.close()


def build_rijeka_news_digest(user_id):
    local_now = datetime.now(ZoneInfo(calendar_service.TIME_ZONE))
    seen_urls = get_seen_rijeka_news_urls(user_id, limit=40)
    digest = rijeka_news_service.generate_daily_digest(
        client=client,
        model=RIJEKA_NEWS_MODEL,
        search_model=RIJEKA_SEARCH_MODEL,
        seen_urls=seen_urls,
        local_now=local_now,
    )
    print(
        "HNK RIJEKA IZVORI:",
        f"izravno={digest.get('direct_items', 0)}",
        f"web_search_pozivi={digest.get('web_search_calls', 0)}",
    )
    if digest.get("social_search_error"):
        print("HNK RIJEKA DRUŠTVENA PRETRAGA:", digest["social_search_error"])
    save_seen_rijeka_news_sources(user_id, digest.get("sources", []))
    return digest.get("text", "")


# =========================================================
# STRIPOVI.COM — ONLY FORUM_ID=5
# =========================================================

def enable_stripovi_forum_report(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        INSERT INTO stripovi_forum_subscribers (user_id, enabled)
        VALUES (?, 1)
        ON CONFLICT(user_id)
        DO UPDATE SET enabled = 1, updated_at = CURRENT_TIMESTAMP
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def disable_stripovi_forum_report(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        INSERT INTO stripovi_forum_subscribers (user_id, enabled)
        VALUES (?, 0)
        ON CONFLICT(user_id)
        DO UPDATE SET enabled = 0, updated_at = CURRENT_TIMESTAMP
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_stripovi_forum_subscribers():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT user_id FROM stripovi_forum_subscribers WHERE enabled = 1"
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def build_stripovi_forum_digest():
    local_now = datetime.now(ZoneInfo(calendar_service.TIME_ZONE))
    digest = stripovi_forum_service.generate_daily_digest(
        client=client,
        model=STRIPOVI_FORUM_MODEL,
        local_now=local_now,
    )
    return digest.get("text", "")


# =========================================================
# CHAT MEMORY
# =========================================================

def save_message(user_id, role, content):

    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        INSERT INTO messages (
            user_id,
            role,
            content
        )
        VALUES (?, ?, ?)
        """,
        (user_id, role, content),
    )

    conn.commit()
    conn.close()


def get_messages(user_id):

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, MAX_MESSAGES),
    ).fetchall()

    conn.close()

    rows.reverse()

    # Dugački popisi (npr. Spotify rezultati) ne smiju sami potrošiti cijeli
    # kontekst sljedećeg odgovora.
    return [
        {
            "role": role,
            "content": content[:MAX_MESSAGE_CHARS],
        }
        for role, content in rows
    ]


def clear_messages(user_id):

    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        DELETE FROM messages
        WHERE user_id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()


# =========================================================
# HISTORY
# =========================================================

def save_history_track(track, played_at):

    if not track or not played_at:
        return

    artists = track.get("artists", [])

    artist = (
        artists[0]
        if artists
        else {}
    )

    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        INSERT OR IGNORE INTO listening_history (
            track_id,
            track_uri,
            track_name,
            artist_name,
            artist_id,
            album_name,
            played_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            track.get("id"),
            track.get("uri"),
            track.get("name", ""),
            artist.get("name", ""),
            artist.get("id"),
            track.get(
                "album",
                {}
            ).get(
                "name",
                ""
            ),
            played_at,
        ),
    )

    conn.commit()
    conn.close()


def spotify_sync_history():

    try:

        result = spotify.current_user_recently_played(
            limit=50
        )

        count = 0

        for item in result.get(
            "items",
            []
        ):

            track = item.get("track")
            played_at = item.get("played_at")

            if track and played_at:

                save_history_track(
                    track,
                    played_at
                )

                count += 1

        return (
            f"Provjereno {count} nedavnih slušanja."
        )

    except Exception as e:

        return (
            f"History sync nije uspio: {e}"
        )


def sync_history_if_needed():

    global LAST_HISTORY_SYNC

    now = datetime.now(
        timezone.utc
    )

    if LAST_HISTORY_SYNC is None:

        spotify_sync_history()

        LAST_HISTORY_SYNC = now

        return

    elapsed = (
        now - LAST_HISTORY_SYNC
    ).total_seconds()

    if elapsed >= HISTORY_SYNC_MINUTES * 60:

        spotify_sync_history()

        LAST_HISTORY_SYNC = now


def get_all_play_counts():

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT track_id, COUNT(*)
        FROM listening_history
        WHERE track_id IS NOT NULL
        GROUP BY track_id
        """
    ).fetchall()

    conn.close()

    return dict(rows)


def get_recent_play_counts(days=30):

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=int(days))
    ).isoformat()

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT track_id, COUNT(*)
        FROM listening_history
        WHERE track_id IS NOT NULL
          AND played_at >= ?
        GROUP BY track_id
        """,
        (cutoff,),
    ).fetchall()

    conn.close()

    return dict(rows)


def get_last_play_dates():

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT track_id, MAX(played_at)
        FROM listening_history
        WHERE track_id IS NOT NULL
        GROUP BY track_id
        """
    ).fetchall()

    conn.close()

    return dict(rows)


def days_since_played(value):

    if not value:
        return 9999

    try:

        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

        return (
            datetime.now(timezone.utc)
            - dt
        ).days

    except Exception:

        return 9999


# =========================================================
# DEVICES + PLAYER
# =========================================================

def remember_spotify_device(device):
    global LAST_SPOTIFY_DEVICE

    if device and device.get("id"):
        LAST_SPOTIFY_DEVICE = {
            "id": device["id"],
            "name": device.get("name", "Spotify uređaj"),
        }


def get_last_spotify_device():
    return LAST_SPOTIFY_DEVICE

def get_spotify_device():

    try:

        devices = spotify.devices().get(
            "devices",
            []
        )

        if not devices:
            return None

        for device in devices:

            if device.get("is_active"):
                remember_spotify_device(device)
                return device

        remember_spotify_device(devices[0])
        return devices[0]

    except Exception:

        return None


def spotify_devices():

    try:

        devices = spotify.devices().get(
            "devices",
            []
        )

        if not devices:
            return "Ne vidim Spotify uređaje."

        return "\n".join(
            (
                f"• {d['name']} "
                f"({d['type']}, "
                f"{'aktivni' if d.get('is_active') else 'neaktivan'})"
            )
            for d in devices
        )

    except Exception as e:

        return f"Device greška: {e}"


def spotify_current_track():

    try:

        current = spotify.current_playback()

        if not current or not current.get("item"):

            return "Trenutno ništa ne svira."

        track = current["item"]

        artists = ", ".join(
            a["name"]
            for a in track.get(
                "artists",
                []
            )
        )

        return (
            f"{artists} - {track['name']}"
        )

    except Exception as e:

        return f"Spotify greška: {e}"


def spotify_pause():

    try:

        playback = spotify.current_playback()
        device = playback.get("device") if playback else None
        remember_spotify_device(device)

        spotify.pause_playback(
            device_id=device.get("id") if device else None
        )

        return "Spotify pauziran."

    except Exception as e:

        return f"Pause greška: {e}"


def spotify_resume():

    try:

        device = get_spotify_device() or get_last_spotify_device()

        if not device:

            return (
                "Ne vidim Spotify uređaj. Otvori Spotify na željenom uređaju "
                "i pokušaj ponovno."
            )

        spotify.start_playback(
            device_id=device["id"]
        )

        return (
            f"Nastavljam na {device['name']}."
        )

    except Exception as e:

        return f"Resume greška: {e}"


def spotify_next():

    try:

        spotify.next_track()

        return "Sljedeća pjesma."

    except Exception as e:

        return f"Next greška: {e}"


def spotify_previous():

    try:

        spotify.previous_track()

        return "Prethodna pjesma."

    except Exception as e:

        return f"Previous greška: {e}"


def spotify_set_volume(percent):

    try:

        percent = max(
            0,
            min(int(percent), 100)
        )

        spotify.volume(percent)

        return f"Glasnoća {percent}%."

    except Exception as e:

        return f"Volume greška: {e}"


def spotify_shuffle(enabled):

    try:

        spotify.shuffle(
            bool(enabled)
        )

        return (
            "Shuffle uključen."
            if enabled
            else "Shuffle isključen."
        )

    except Exception as e:

        return f"Shuffle greška: {e}"


def spotify_repeat(mode):

    try:

        if mode not in [
            "track",
            "context",
            "off",
        ]:

            mode = "off"

        spotify.repeat(mode)

        return f"Repeat: {mode}."

    except Exception as e:

        return f"Repeat greška: {e}"


# =========================================================
# SEARCH
# =========================================================

def normalize_name(name):

    return (
        name
        .lower()
        .strip()
        .replace("’", "'")
    )


def spotify_find_artist(artist_name):

    try:

        result = spotify.search(
            q=f'artist:"{artist_name}"',
            type="artist",
            limit=10,
        )

        artists = (
            result
            .get("artists", {})
            .get("items", [])
        )

        if not artists:
            return None

        target = normalize_name(
            artist_name
        )

        # Prefer exact match.
        for artist in artists:

            if normalize_name(
                artist.get(
                    "name",
                    ""
                )
            ) == target:

                return artist

        # Fallback na prvi Spotify rezultat.
        return artists[0]

    except Exception as e:

        print(
            "Artist search:",
            e
        )

        return None


def spotify_search_track(query):

    try:

        result = spotify.search(
            q=query,
            type="track",
            limit=10,
        )

        tracks = (
            result
            .get("tracks", {})
            .get("items", [])
        )

        return (
            tracks[0]
            if tracks
            else None
        )

    except Exception:

        return None


def spotify_search_and_play(query):

    try:

        track = spotify_search_track(
            query
        )

        if not track:

            return f"Nisam pronašao {query}."

        device = get_spotify_device()

        if not device:

            return (
                "Otvori Spotify na nekom uređaju."
            )

        remember_spotify_device(device)

        spotify.start_playback(
            device_id=device["id"],
            uris=[
                track["uri"]
            ],
        )

        artists = ", ".join(
            a["name"]
            for a in track["artists"]
        )

        return (
            f"Puštam {artists} - {track['name']}."
        )

    except Exception as e:

        return f"Play greška: {e}"


# =========================================================
# TRACKS FOR DISCOVERY ARTIST
# =========================================================

def spotify_tracks_for_artist(
    artist,
    count=2
):
    """
    Artist Top Tracks endpoint više nije pouzdan
    za Development Mode, zato koristimo Search.

    Rezultat filtriramo po stvarnom Spotify artist_id.
    """

    artist_id = artist.get("id")
    artist_name = artist.get("name", "")

    if not artist_id:
        return []

    wanted = max(
        1,
        min(int(count), 5)
    )

    found = []

    seen_track_ids = set()
    seen_names = set()

    # Search max limit je 10.
    # Probamo nekoliko stranica.
    for offset in [
        0,
        10,
        20,
    ]:

        try:

            result = spotify.search(
                q=f'artist:"{artist_name}"',
                type="track",
                limit=10,
                offset=offset,
            )

        except Exception:

            continue

        tracks = (
            result
            .get("tracks", {})
            .get("items", [])
        )

        for track in tracks:

            track_id = track.get("id")

            if not track_id:
                continue

            artist_ids = {
                a.get("id")
                for a in track.get(
                    "artists",
                    []
                )
            }

            # HARD validation:
            # pjesma stvarno mora pripadati izvođaču.
            if artist_id not in artist_ids:
                continue

            normalized_track = normalize_name(
                track.get(
                    "name",
                    ""
                )
            )

            # Izbaci duplikate singlova/album verzija
            # istog naziva.
            if (
                track_id in seen_track_ids
                or normalized_track in seen_names
            ):
                continue

            seen_track_ids.add(
                track_id
            )

            seen_names.add(
                normalized_track
            )

            found.append(
                track
            )

            if len(found) >= wanted:
                return found

    return found


# =========================================================
# QUEUE
# =========================================================

def spotify_get_queue(limit=10):

    try:

        queue = spotify.queue().get(
            "queue",
            []
        )

        if not queue:
            return "Queue je prazan."

        lines = []

        for i, track in enumerate(
            queue[:int(limit)],
            1
        ):

            artists = ", ".join(
                a["name"]
                for a in track.get(
                    "artists",
                    []
                )
            )

            lines.append(
                f"{i}. {artists} - {track['name']}"
            )

        return "\n".join(lines)

    except Exception as e:

        return f"Queue greška: {e}"


def spotify_add_to_queue(query):

    try:

        track = spotify_search_track(
            query
        )

        if not track:

            return (
                f"Nisam pronašao {query}."
            )

        spotify.add_to_queue(
            track["uri"]
        )

        return (
            f"Dodano u queue: {track['name']}."
        )

    except Exception as e:

        return f"Queue greška: {e}"


# =========================================================
# LIKED SONGS
# =========================================================

def spotify_get_liked_tracks(
    max_tracks=2000
):

    tracks = []

    offset = 0

    try:

        while len(tracks) < max_tracks:

            result = (
                spotify
                .current_user_saved_tracks(
                    limit=50,
                    offset=offset,
                )
            )

            items = result.get(
                "items",
                []
            )

            if not items:
                break

            for item in items:

                track = item.get("track")

                if not track:
                    continue

                artists = track.get(
                    "artists",
                    []
                )

                tracks.append(
                    {
                        "id": track.get("id"),
                        "uri": track.get("uri"),
                        "name": track.get(
                            "name",
                            ""
                        ),
                        "artist": (
                            artists[0]["name"]
                            if artists
                            else ""
                        ),
                        "artist_id": (
                            artists[0]["id"]
                            if artists
                            else None
                        ),
                    }
                )

                if len(tracks) >= max_tracks:
                    break

            if not result.get("next"):
                break

            offset += 50

        return tracks

    except Exception as e:

        print(
            "Liked Songs:",
            e
        )

        return []


def spotify_list_liked(limit=20):

    tracks = spotify_get_liked_tracks(
        max_tracks=min(
            int(limit),
            100
        )
    )

    if not tracks:
        return "Nema Liked Songs."

    return "\n".join(
        (
            f"{i}. {t['artist']} - "
            f"{t['name']}"
        )
        for i, t in enumerate(
            tracks,
            1
        )
    )


# =========================================================
# DISCOVERY FAVORITES CACHE
# =========================================================

def get_liked_artist_counts():

    counts = {}

    for track in spotify_get_liked_tracks(
        max_tracks=2000
    ):

        artist_id = track.get(
            "artist_id"
        )

        if not artist_id:
            continue

        counts[artist_id] = (
            counts.get(
                artist_id,
                0
            )
            + 1
        )

    return counts


def get_top_artist_ids():

    result_ids = set()

    for time_range in [
        "short_term",
        "medium_term",
        "long_term",
    ]:

        try:

            result = (
                spotify
                .current_user_top_artists(
                    limit=50,
                    time_range=time_range,
                )
            )

            for artist in result.get(
                "items",
                []
            ):

                artist_id = artist.get(
                    "id"
                )

                if artist_id:

                    result_ids.add(
                        artist_id
                    )

        except Exception as e:

            print(
                "Top artist discovery filter:",
                e
            )

    return result_ids


# =========================================================
# DISCOVERY V2
# =========================================================

def spotify_discover_artists_v2(
    seed_artist,
    candidate_artists,
    artist_count=5,
    tracks_per_artist=2,
    exclude_favorites=True,
    exclude_top_artists=True,
    min_liked_tracks_to_exclude=2,
    create_discovery_playlist=False,
    playlist_name=None,
):
    """
    GPT daje candidate_artists.
    Python odlučuje koji su stvarno novi.

    HARD FILTER radi po Spotify artist_id.
    """

    try:

        artist_count = max(
            1,
            min(
                int(artist_count),
                10
            )
        )

        tracks_per_artist = max(
            1,
            min(
                int(tracks_per_artist),
                5
            )
        )

        min_liked_tracks_to_exclude = max(
            1,
            int(
                min_liked_tracks_to_exclude
            )
        )

        # ---------------------------------------------
        # RESOLVE SEED
        # ---------------------------------------------

        seed = spotify_find_artist(
            seed_artist
        )

        seed_id = (
            seed.get("id")
            if seed
            else None
        )

        # ---------------------------------------------
        # BUILD HARD BLACKLIST
        # ---------------------------------------------

        liked_counts = {}

        top_artist_ids = set()

        if exclude_favorites:

            liked_counts = (
                get_liked_artist_counts()
            )

        if exclude_top_artists:

            top_artist_ids = (
                get_top_artist_ids()
            )

        # ---------------------------------------------
        # RESOLVE CANDIDATES
        # ---------------------------------------------

        selected_artists = []

        seen_ids = set()

        excluded_debug = []

        for candidate_name in candidate_artists:

            if (
                len(selected_artists)
                >= artist_count
            ):
                break

            artist = spotify_find_artist(
                candidate_name
            )

            if not artist:
                continue

            artist_id = artist.get(
                "id"
            )

            if not artist_id:
                continue

            artist_name = artist.get(
                "name",
                candidate_name
            )

            # -----------------------------------------
            # SEED
            # -----------------------------------------

            if (
                seed_id
                and artist_id == seed_id
            ):

                excluded_debug.append(
                    (
                        artist_name,
                        "seed artist"
                    )
                )

                continue

            # -----------------------------------------
            # DUPLICATE
            # -----------------------------------------

            if artist_id in seen_ids:

                continue

            seen_ids.add(
                artist_id
            )

            # -----------------------------------------
            # LIKED SONG HARD FILTER
            # -----------------------------------------

            liked_count = (
                liked_counts.get(
                    artist_id,
                    0
                )
            )

            if (
                exclude_favorites
                and liked_count
                >= min_liked_tracks_to_exclude
            ):

                excluded_debug.append(
                    (
                        artist_name,
                        f"{liked_count} Liked Songs"
                    )
                )

                print(
                    "DISCOVERY EXCLUDED:",
                    artist_name,
                    "- liked:",
                    liked_count,
                )

                continue

            # -----------------------------------------
            # TOP ARTIST HARD FILTER
            # -----------------------------------------

            if (
                exclude_top_artists
                and artist_id
                in top_artist_ids
            ):

                excluded_debug.append(
                    (
                        artist_name,
                        "Top Artist"
                    )
                )

                print(
                    "DISCOVERY EXCLUDED:",
                    artist_name,
                    "- Top Artist"
                )

                continue

            # -----------------------------------------
            # TRACKS
            # -----------------------------------------

            tracks = (
                spotify_tracks_for_artist(
                    artist,
                    count=tracks_per_artist,
                )
            )

            if not tracks:
                continue

            selected_artists.append(
                {
                    "artist": artist,
                    "tracks": tracks,
                    "liked_count": liked_count,
                }
            )

        # ---------------------------------------------
        # NOT ENOUGH?
        # ---------------------------------------------

        if not selected_artists:

            return (
                "Nisam pronašao dovoljno novih izvođača "
                "nakon provjere tvoje biblioteke i "
                "Top Artists. Probaj širi opis ili drugi "
                "seed artist."
            )

        # ---------------------------------------------
        # FORMAT TELEGRAM RESULT
        # ---------------------------------------------

        lines = [
            (
                f"Discovery prema {seed_artist}:"
            )
        ]

        playlist_tracks = []

        for index, item in enumerate(
            selected_artists,
            1
        ):

            artist = item[
                "artist"
            ]

            tracks = item[
                "tracks"
            ]

            lines.append(
                f"\n{index}. {artist['name']}"
            )

            for track in tracks:

                lines.append(
                    f"   • {track['name']}"
                )

                playlist_tracks.append(
                    {
                        "id": track.get("id"),
                        "uri": track.get("uri"),
                        "name": track.get(
                            "name",
                            ""
                        ),
                        "artist": artist[
                            "name"
                        ],
                        "artist_id": artist[
                            "id"
                        ],
                    }
                )

        # ---------------------------------------------
        # OPTIONAL PLAYLIST
        # ---------------------------------------------

        if create_discovery_playlist:

            if not playlist_name:

                playlist_name = (
                    f"Discovery: {seed_artist}"
                )

            playlist_result = create_playlist(
                name=playlist_name,
                tracks=playlist_tracks,
                description=(
                    "AI Discovery V2. "
                    f"Seed: {seed_artist}. "
                    "Izvođači provjereni protiv "
                    "Liked Songs i Top Artists."
                ),
            )

            lines.append(
                "\n" + playlist_result
            )

        # ---------------------------------------------
        # DEBUG
        # ---------------------------------------------

        print(
            "\nDISCOVERY SELECTED:"
        )

        for item in selected_artists:

            print(
                " +",
                item["artist"]["name"],
                "liked=",
                item["liked_count"]
            )

        print(
            "\nDISCOVERY EXCLUDED:"
        )

        for artist_name, reason in excluded_debug:

            print(
                " -",
                artist_name,
                ":",
                reason
            )

        return "\n".join(lines)

    except Exception as e:

        return (
            f"Discovery V2 nije uspio: {e}"
        )


# =========================================================
# TOP ITEMS
# =========================================================

def normalize_time_range(value):

    mapping = {
        "month": "short_term",
        "mjesec": "short_term",
        "short_term": "short_term",

        "6months": "medium_term",
        "6 mjeseci": "medium_term",
        "medium_term": "medium_term",

        "year": "long_term",
        "godina": "long_term",
        "long_term": "long_term",
    }

    return mapping.get(
        str(value).lower(),
        "medium_term"
    )


def spotify_top_tracks(
    time_range="medium_term",
    limit=10
):

    try:

        result = spotify.current_user_top_tracks(
            limit=max(
                1,
                min(
                    int(limit),
                    50
                )
            ),
            time_range=normalize_time_range(
                time_range
            ),
        )

        return "\n".join(
            (
                f"{i}. "
                f"{', '.join(a['name'] for a in track['artists'])}"
                f" - {track['name']}"
            )
            for i, track in enumerate(
                result.get(
                    "items",
                    []
                ),
                1
            )
        )

    except Exception as e:

        return f"Top tracks greška: {e}"


def spotify_top_artists(
    time_range="medium_term",
    limit=10
):

    try:

        result = spotify.current_user_top_artists(
            limit=max(
                1,
                min(
                    int(limit),
                    50
                )
            ),
            time_range=normalize_time_range(
                time_range
            ),
        )

        return "\n".join(
            f"{i}. {artist['name']}"
            for i, artist in enumerate(
                result.get(
                    "items",
                    []
                ),
                1
            )
        )

    except Exception as e:

        return f"Top artists greška: {e}"


# =========================================================
# ARTIST GENRE CACHE
# =========================================================

def get_artist_genres(
    artist_id,
    artist_name=""
):

    if not artist_id:
        return []

    conn = sqlite3.connect(DB_FILE)

    row = conn.execute(
        """
        SELECT genres
        FROM artist_cache
        WHERE artist_id = ?
        """,
        (artist_id,),
    ).fetchone()

    if row:

        conn.close()

        try:

            return json.loads(
                row[0]
            )

        except Exception:

            return []

    try:

        artist = spotify.artist(
            artist_id
        )

        genres = [
            g.lower()
            for g in artist.get(
                "genres",
                []
            )
        ]

        conn.execute(
            """
            INSERT OR REPLACE INTO artist_cache (
                artist_id,
                artist_name,
                genres,
                updated_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                artist_id,
                artist.get(
                    "name",
                    artist_name
                ),
                json.dumps(genres),
                datetime.now(
                    timezone.utc
                ).isoformat(),
            ),
        )

        conn.commit()
        conn.close()

        return genres

    except Exception:

        conn.close()

        return []


def is_domestic_track(track):

    artist = track.get(
        "artist",
        ""
    ).lower()

    return any(
        domestic in artist
        for domestic in DOMESTIC_ARTISTS
    )


def track_genre_matches(
    track,
    genre
):

    genres = get_artist_genres(
        track.get("artist_id"),
        track.get("artist", ""),
    )

    genre_text = " ".join(
        genres
    )

    genre = genre.lower().strip()

    keywords = GENRE_KEYWORDS.get(
        genre,
        [genre]
    )

    return any(
        keyword in genre_text
        for keyword in keywords
    )


# =========================================================
# PLAYLIST CREATION
# =========================================================

def create_playlist(
    name,
    tracks,
    description="Telegram AI playlist",
):

    if not tracks:

        return (
            "Nema pjesama za playlistu."
        )

    try:

        playlist = (
            spotify
            .current_user_playlist_create(
                name=name,
                public=False,
                description=description[:300],
            )
        )

        uris = [
            track["uri"]
            for track in tracks
            if track.get("uri")
        ]

        # Spotipy će ovisno o verziji mapirati
        # odgovarajući aktualni playlist endpoint.
        for i in range(
            0,
            len(uris),
            100
        ):

            spotify.playlist_add_items(
                playlist["id"],
                uris[i:i + 100],
            )

        url = (
            playlist
            .get(
                "external_urls",
                {}
            )
            .get(
                "spotify",
                ""
            )
        )

        return (
            f"Playlist '{name}' "
            f"({len(uris)} pjesama)\n{url}"
        )

    except Exception as e:

        return (
            f"Playlist nije uspjela: {e}"
        )


# =========================================================
# SMART PLAYLIST V3
# =========================================================

def spotify_create_smart_playlist_v3(
    name,
    max_tracks=30,
    genres=None,
    domestic_weight=0,
    domestic_required=False,
    favorite_weight=0,
    forgotten_weight=0,
    avoid_recent_days=None,
    recent_penalty=0,
    max_recent_plays=None,
    recent_period_days=30,
    max_per_artist=2,
    avoid_consecutive_artist=True,
    randomness=1.0,
):

    try:

        sync_history_if_needed()

        genres = genres or []

        max_tracks = max(
            1,
            min(
                int(max_tracks),
                100
            )
        )

        max_per_artist = max(
            1,
            int(
                max_per_artist or 2
            )
        )

        liked = spotify_get_liked_tracks(
            max_tracks=2000
        )

        if not liked:

            return "Nema Liked Songs."

        total_counts = (
            get_all_play_counts()
        )

        recent_counts = (
            get_recent_play_counts(
                recent_period_days
            )
        )

        last_dates = (
            get_last_play_dates()
        )

        max_total = max(
            total_counts.values(),
            default=1
        )

        candidates = []

        for track in liked:

            track_id = track.get("id")

            if not track_id:
                continue

            domestic = (
                is_domestic_track(
                    track
                )
            )

            if (
                domestic_required
                and not domestic
            ):
                continue

            recent_plays = (
                recent_counts.get(
                    track_id,
                    0
                )
            )

            if (
                max_recent_plays
                is not None
                and recent_plays
                > int(max_recent_plays)
            ):
                continue

            total_plays = (
                total_counts.get(
                    track_id,
                    0
                )
            )

            days_old = days_since_played(
                last_dates.get(
                    track_id
                )
            )

            score = 0.0

            # Genres
            for genre_spec in genres:

                genre_name = (
                    genre_spec
                    .get(
                        "name",
                        ""
                    )
                )

                weight = float(
                    genre_spec.get(
                        "weight",
                        1
                    )
                )

                if (
                    genre_name
                    and track_genre_matches(
                        track,
                        genre_name
                    )
                ):

                    score += weight

            # Domestic
            if domestic:

                score += float(
                    domestic_weight
                )

            # Favorites
            if favorite_weight:

                factor = (
                    math.log1p(
                        total_plays
                    )
                    /
                    math.log1p(
                        max_total
                    )
                )

                score += (
                    factor
                    * float(
                        favorite_weight
                    )
                )

            # Forgotten
            if forgotten_weight:

                forgotten_factor = min(
                    days_old / 180.0,
                    1.0
                )

                score += (
                    forgotten_factor
                    * float(
                        forgotten_weight
                    )
                )

            # Recent penalty
            if (
                avoid_recent_days
                and recent_penalty
                and days_old
                < int(avoid_recent_days)
            ):

                factor = (
                    1
                    - days_old
                    / int(
                        avoid_recent_days
                    )
                )

                score -= (
                    factor
                    * float(
                        recent_penalty
                    )
                )

            score += random.uniform(
                0,
                max(
                    0,
                    float(randomness)
                )
            )

            candidates.append(
                {
                    **track,
                    "_score": score,
                }
            )

        candidates.sort(
            key=lambda x:
                x["_score"],
            reverse=True
        )

        selected = []

        artist_counts = {}

        previous_artist = None

        for track in candidates:

            if len(selected) >= max_tracks:
                break

            artist = track.get(
                "artist",
                ""
            ).lower()

            if (
                artist_counts.get(
                    artist,
                    0
                )
                >= max_per_artist
            ):
                continue

            if (
                avoid_consecutive_artist
                and previous_artist
                == artist
            ):
                continue

            selected.append(
                track
            )

            artist_counts[artist] = (
                artist_counts.get(
                    artist,
                    0
                )
                + 1
            )

            previous_artist = artist

        return create_playlist(
            name=name,
            tracks=selected,
            description=(
                "Telegram AI Smart Playlist V3"
            ),
        )

    except Exception as e:

        return (
            f"Smart Playlist V3 greška: {e}"
        )


# =========================================================
# HISTORY STATS
# =========================================================

def _history_utc_bound(local_date, next_day=False):

    # played_at je UTC ("...Z"), a korisnik pita po lokalnim danima. Granica
    # se pretvara u UTC; za kraj perioda uzima se početak sljedećeg dana.
    day = datetime.fromisoformat(
        str(local_date)[:10]
    ).date()

    if next_day:
        day += timedelta(days=1)

    local_start = datetime.combine(
        day,
        time.min,
        tzinfo=ZoneInfo(calendar_service.TIME_ZONE),
    )

    return local_start.astimezone(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S")


def _history_period_filter(start_date, end_date):

    clause = ""
    params = []

    if start_date:
        clause += " AND played_at >= ?"
        params.append(_history_utc_bound(start_date))

    if end_date:
        clause += " AND played_at < ?"
        params.append(_history_utc_bound(end_date, next_day=True))

    return clause, params


def spotify_history_stats(
    start_date=None,
    end_date=None,
    limit=10
):

    sync_history_if_needed()

    conn = sqlite3.connect(DB_FILE)

    query = """
        SELECT
            track_name,
            artist_name,
            COUNT(*) AS plays
        FROM listening_history
        WHERE 1=1
    """

    clause, params = _history_period_filter(
        start_date,
        end_date
    )

    query += clause

    query += """
        GROUP BY
            track_id,
            track_name,
            artist_name
        ORDER BY plays DESC
        LIMIT ?
    """

    params.append(
        min(
            int(limit),
            50
        )
    )

    rows = conn.execute(
        query,
        params
    ).fetchall()

    conn.close()

    if not rows:

        return (
            "Nemam dovoljno lokalne povijesti "
            "za taj period."
        )

    return "\n".join(
        (
            f"{i}. {artist} - {track}: "
            f"{plays} slušanja"
        )
        for i, (
            track,
            artist,
            plays
        ) in enumerate(
            rows,
            1
        )
    )


def spotify_history_list(
    start_date=None,
    end_date=None,
    limit=50,
    create_playlist_from_history=False,
    playlist_name=None,
):

    sync_history_if_needed()

    limit = max(1, min(int(limit or 50), 200))

    # Bez kraja perioda misli se na jedan dan ("što sam slušao jučer").
    end_date = end_date or start_date

    clause, params = _history_period_filter(
        start_date,
        end_date
    )

    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT played_at, track_name, artist_name, track_uri
        FROM listening_history
        WHERE 1=1
        """
        + clause
        + " ORDER BY played_at ASC",
        params,
    ).fetchall()

    conn.close()

    if not rows:

        return (
            "Nemam zabilježenih slušanja "
            "za taj period."
        )

    if create_playlist_from_history:

        # Svaka pjesma samo jednom, redom prvog slušanja.
        tracks = []
        seen = set()

        for _, track_name, artist_name, uri in rows:

            if not uri or uri in seen:
                continue

            seen.add(uri)
            tracks.append({"uri": uri})

        name = playlist_name or (
            f"Slušano {start_date}"
            if not end_date or end_date == start_date
            else f"Slušano {start_date} – {end_date}"
        )

        return create_playlist(
            name=name,
            tracks=tracks,
            description="Telegram AI playlist iz povijesti slušanja",
        )

    zone = ZoneInfo(calendar_service.TIME_ZONE)

    lines = []

    for played_at, track_name, artist_name, _ in rows[-limit:]:

        local = datetime.fromisoformat(
            played_at.replace("Z", "+00:00")
        ).astimezone(zone)

        lines.append(
            f"{local:%d.%m. %H:%M} {artist_name} - {track_name}"
        )

    header = f"Ukupno slušanja: {len(rows)}"

    if len(rows) > limit:
        header += f" (prikazano zadnjih {limit})"

    return header + "\n" + "\n".join(lines)


# =========================================================
# LOCAL COMMANDS
# =========================================================

def _calendar_event_local_date(event):
    value = event.get("start")
    if not value:
        return None
    if "T" not in value:
        return datetime.fromisoformat(value).date()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(calendar_service.TIME_ZONE))
    return parsed.astimezone(ZoneInfo(calendar_service.TIME_ZONE)).date()


def _try_local_birthdays_command(message):
    folded = _fold_memory_text(message)
    if "rodendan" not in folded:
        return None

    local_zone = ZoneInfo(calendar_service.TIME_ZONE)
    today = datetime.now(local_zone).date()

    if "danas" in folded:
        start_date = today
        end_date = today + timedelta(days=1)
        heading = "Rođendani danas:"
    elif any(value in folded for value in ("mjesec", "mjesec", "mesec")):
        start_date = today.replace(day=1)
        if start_date.month == 12:
            end_date = start_date.replace(year=start_date.year + 1, month=1)
        else:
            end_date = start_date.replace(month=start_date.month + 1)
        heading = f"Rođendani u {start_date.strftime('%m.%Y.')}:"
    else:
        return None

    start = datetime.combine(start_date, time.min, tzinfo=local_zone)
    end = datetime.combine(end_date, time.min, tzinfo=local_zone)
    birthdays = calendar_service.list_birthdays(
        start.isoformat(), end.isoformat(), limit=250
    )

    if not birthdays:
        return heading + "\n• Nema pronađenih rođendana."

    lines = [heading]
    for birthday in birthdays:
        birthday_date = _calendar_event_local_date(birthday)
        date_text = birthday_date.strftime("%d.%m.") if birthday_date else "bez datuma"
        today_marker = " — DANAS" if birthday_date == today else ""
        lines.append(
            f"• {date_text} {birthday.get('title', '(bez naziva)')}{today_marker}"
        )
    return "\n".join(lines)


def try_local_command(message):

    text = message.lower().strip()

    birthdays = _try_local_birthdays_command(message)
    if birthdays is not None:
        return birthdays

    if any(
        x in text
        for x in [
            "što trenutno svira",
            "sto trenutno svira",
            "što svira",
            "sto svira",
        ]
    ):

        return spotify_current_track()

    if any(
        x in text
        for x in [
            "pauziraj",
            "pauza",
        ]
    ):

        return spotify_pause()

    if any(
        x in text
        for x in [
            "nastavi glazbu",
            "nastavi spotify",
        ]
    ):

        return spotify_resume()

    if any(
        x in text
        for x in [
            "sljedeća pjesma",
            "sljedeca pjesma",
            "preskoči pjesmu",
        ]
    ):

        return spotify_next()

    if any(
        x in text
        for x in [
            "prethodna pjesma",
            "vrati prethodnu",
        ]
    ):

        return spotify_previous()

    volume = re.search(
        r"(?:glasnoća|glasnoca|volume).*?(\d{1,3})",
        text
    )

    if volume:

        return spotify_set_volume(
            volume.group(1)
        )

    if "shuffle" in text:

        return spotify_shuffle(
            not any(
                x in text
                for x in [
                    "isključi",
                    "iskljuci",
                    "off",
                ]
            )
        )

    return None


# =========================================================
# MODEL ROUTER
# =========================================================

def choose_model(message):

    text = message.lower()

    smart = [
        "playlist",
        "playlista",
        "slič",
        "slic",
        "bendova kao",
        "bandova kao",
        "izvođača kao",
        "izvodaca kao",
        "discovery",
        "koje ne slušam",
        "koje ne slusam",
        "nisu među",
        "nisu medu",
        "analiziraj",
        "usporedi",
        "punk",
        "post-punk",
        "preferiraj",
    ]

    if any(
        keyword in text
        for keyword in smart
    ):

        return SMART_MODEL

    return CHEAP_MODEL


# =========================================================
# OPENAI TOOLS
# =========================================================

TOOLS = [

    {
        "type": "function",
        "name": "spotify_current_track",
        "description": "Provjeri što trenutno svira.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_search_and_play",
        "description": "Pretraži Spotify i pusti pjesmu.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string"
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_get_queue",
        "description": "Prikaži Spotify queue.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer"
                }
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_add_to_queue",
        "description": "Dodaj pjesmu u queue.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string"
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_top_tracks",
        "description": "Korisnikove Spotify top pjesme.",
        "parameters": {
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string"
                },
                "limit": {
                    "type": "integer"
                }
            },
            "required": [
                "time_range",
                "limit"
            ],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_top_artists",
        "description": "Korisnikovi Spotify top izvođači.",
        "parameters": {
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string"
                },
                "limit": {
                    "type": "integer"
                }
            },
            "required": [
                "time_range",
                "limit"
            ],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_list_liked",
        "description": "Prikaži Liked Songs.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer"
                }
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
    },

    # =====================================================
    # DISCOVERY V2
    # =====================================================

    {
        "type": "function",
        "name": "spotify_discover_artists_v2",

        "description": (
            "Pronađi izvođače slične seed izvođaču. "
            "GPT daje širi popis kandidata, a Python hard-filtrira "
            "izvođače koji su već među korisnikovim Top Artists "
            "ili imaju dovoljno Liked Songs."
        ),

        "parameters": {

            "type": "object",

            "properties": {

                "seed_artist": {
                    "type": "string"
                },

                "candidate_artists": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    },
                    "description": (
                        "15-25 stilistički srodnih izvođača. "
                        "Navedi više kandidata nego što korisnik traži."
                    )
                },

                "artist_count": {
                    "type": "integer"
                },

                "tracks_per_artist": {
                    "type": "integer"
                },

                "exclude_favorites": {
                    "type": "boolean"
                },

                "exclude_top_artists": {
                    "type": "boolean"
                },

                "min_liked_tracks_to_exclude": {
                    "type": "integer"
                },

                "create_discovery_playlist": {
                    "type": "boolean"
                },

                "playlist_name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
            },

            "required": [
                "seed_artist",
                "candidate_artists",
                "artist_count",
                "tracks_per_artist",
                "exclude_favorites",
                "exclude_top_artists",
                "min_liked_tracks_to_exclude",
                "create_discovery_playlist",
                "playlist_name"
            ],

            "additionalProperties": False,
        },
    },

    # =====================================================
    # SMART PLAYLIST V3
    # =====================================================

    {
        "type": "function",
        "name": "spotify_create_smart_playlist_v3",

        "description": (
            "Kreiraj pametnu playlistu iz korisnikovih "
            "Liked Songs pomoću soft scoring kriterija."
        ),

        "parameters": {

            "type": "object",

            "properties": {

                "name": {
                    "type": "string"
                },

                "max_tracks": {
                    "type": "integer"
                },

                "genres": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string"
                            },
                            "weight": {
                                "type": "number"
                            }
                        },
                        "required": [
                            "name",
                            "weight"
                        ],
                        "additionalProperties": False,
                    }
                },

                "domestic_weight": {
                    "type": "number"
                },

                "domestic_required": {
                    "type": "boolean"
                },

                "favorite_weight": {
                    "type": "number"
                },

                "forgotten_weight": {
                    "type": "number"
                },

                "avoid_recent_days": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },

                "recent_penalty": {
                    "type": "number"
                },

                "max_recent_plays": {
                    "type": [
                        "integer",
                        "null"
                    ]
                },

                "recent_period_days": {
                    "type": "integer"
                },

                "max_per_artist": {
                    "type": "integer"
                },

                "avoid_consecutive_artist": {
                    "type": "boolean"
                },

                "randomness": {
                    "type": "number"
                },
            },

            "required": [
                "name",
                "max_tracks",
                "genres",
                "domestic_weight",
                "domestic_required",
                "favorite_weight",
                "forgotten_weight",
                "avoid_recent_days",
                "recent_penalty",
                "max_recent_plays",
                "recent_period_days",
                "max_per_artist",
                "avoid_consecutive_artist",
                "randomness"
            ],

            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_history_stats",
        "description": (
            "Top pjesme iz lokalne history baze "
            "za točan datumski period."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "end_date": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
                "limit": {
                    "type": "integer"
                }
            },
            "required": [
                "start_date",
                "end_date",
                "limit"
            ],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "spotify_history_list",
        "description": (
            "Kronološki popis pjesama koje je korisnik slušao u lokalnom "
            "datumskom periodu (npr. jučer). Opcionalno od njih napravi "
            "playlistu."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Lokalni datum YYYY-MM-DD.",
                },
                "end_date": {
                    "type": [
                        "string",
                        "null"
                    ],
                    "description": "Lokalni datum YYYY-MM-DD, uključivo.",
                },
                "limit": {
                    "type": "integer"
                },
                "create_playlist": {
                    "type": "boolean"
                },
                "playlist_name": {
                    "type": [
                        "string",
                        "null"
                    ]
                },
            },
            "required": [
                "start_date",
                "end_date",
                "limit",
                "create_playlist",
                "playlist_name"
            ],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_list_calendars",
        "description": "Prikaži korisnikove Google kalendare i njihove ID-eve.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_list_events",
        "description": (
            "Prikaži događaje u zadanom vremenskom razdoblju. Za cijeli dan "
            "ili mjesec postavi start_from_beginning_of_day=true kako današnji "
            "cjelodnevni događaji ne bi bili izostavljeni."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "limit": {"type": "integer"},
                "calendar_id": {"type": ["string", "null"]},
                "start_from_beginning_of_day": {"type": "boolean"},
            },
            "required": [
                "time_min", "time_max", "limit", "calendar_id",
                "start_from_beginning_of_day"
            ],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_list_birthdays",
        "description": (
            "Prikaži sve rođendane u zadanom datumskom rasponu. Pregledava "
            "Googleov kalendar rođendana i ručno upisane rođendane u glavnom "
            "kalendaru. Početak raspona uvijek se računa od 00:00."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["time_min", "time_max", "limit"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_get_event",
        "description": "Dohvati detalje događaja prema ID-u.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": ["string", "null"]},
            },
            "required": ["event_id", "calendar_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_check_free_busy",
        "description": "Provjeri zauzetost kalendara u zadanom razdoblju.",
        "parameters": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "calendar_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
            },
            "required": ["time_min", "time_max", "calendar_ids"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_create_event",
        "description": "Predloži kreiranje događaja; korisnik mora potvrditi u Telegramu.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
                "calendar_id": {"type": ["string", "null"]},
            },
            "required": ["title", "start", "end", "description", "location", "calendar_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_update_event",
        "description": "Predloži izmjenu postojećeg događaja; korisnik mora potvrditi u Telegramu.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title": {"type": ["string", "null"]},
                "start": {"type": ["string", "null"]},
                "end": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
                "calendar_id": {"type": ["string", "null"]},
            },
            "required": ["event_id", "title", "start", "end", "description", "location", "calendar_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "calendar_delete_event",
        "description": "Predloži brisanje događaja; korisnik mora potvrditi u Telegramu.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title": {"type": "string"},
                "calendar_id": {"type": ["string", "null"]},
            },
            "required": ["event_id", "title", "calendar_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "tasks_get_due",
        "description": "Prikaži nedovršene Google Tasks zadatke kojima je rok danas ili je već prošao.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "tasks_list_open",
        "description": "Prikaži sve otvorene Google Tasks zadatke, uključujući one bez zadanog roka.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "task_list_id": {"type": ["string", "null"]},
            },
            "required": ["limit", "task_list_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "tasks_create",
        "description": "Predloži stvaranje Google Tasks zadatka; korisnik mora potvrditi u Telegramu.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
                "task_list_id": {"type": ["string", "null"]},
            },
            "required": ["title", "due", "notes", "task_list_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "tasks_update",
        "description": "Predloži izmjenu Google Tasks zadatka; korisnik mora potvrditi u Telegramu.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": ["string", "null"]},
                "due": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
                "task_list_id": {"type": ["string", "null"]},
            },
            "required": ["task_id", "title", "due", "notes", "task_list_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "tasks_delete",
        "description": "Predloži brisanje Google Tasks zadatka; korisnik mora potvrditi u Telegramu.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "task_list_id": {"type": ["string", "null"]},
            },
            "required": ["task_id", "title", "task_list_id"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "memory_save",
        "description": "Trajno lokalno zapamti izričito navedenu činjenicu o korisniku. Ne koristi bez jasnog zahtjeva korisnika da se podatak zapamti ili promijeni.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "memory_key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["category", "memory_key", "value"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "memory_capture",
        "description": (
            "Tiho spremi jasnu, stabilnu i nesenzitivnu činjenicu ili "
            "preferenciju koju je korisnik naveo usput. Koristi rijetko, "
            "samo kada će podatak vjerojatno pomoći u budućim razgovorima."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "profile", "preferences", "work", "planning",
                        "vehicle", "music", "household", "pets"
                    ],
                },
                "memory_key": {"type": "string"},
                "value": {"type": "string"},
                "importance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                },
                "expires_at": {"type": ["string", "null"]},
            },
            "required": ["category", "memory_key", "value", "importance", "expires_at"],
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "memory_list",
        "description": "Prikaži sve trajno spremljene podatke o korisniku.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },

    {
        "type": "function",
        "name": "memory_forget",
        "description": "Obriši jedan trajno spremljeni podatak samo ako korisnik to izričito traži.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "memory_key": {"type": "string"},
            },
            "required": ["category", "memory_key"],
            "additionalProperties": False,
        },
    },
]


# =========================================================
# TOOL EXECUTION
# =========================================================

def execute_tool(name, args, user_id=None):

    if name == "tasks_get_due":
        return tasks_service.get_due_tasks()

    if name == "tasks_list_open":
        return tasks_service.list_open_tasks(
            args.get("limit", 50), args.get("task_list_id")
        )

    if name == "tasks_create":
        return tasks_service.create_task(
            args.get("title", ""), args.get("due"), args.get("notes"),
            args.get("task_list_id"),
        )

    if name == "tasks_update":
        return tasks_service.update_task(
            args.get("task_id", ""), args.get("title"), args.get("due"),
            args.get("notes"), args.get("task_list_id"),
        )

    if name == "tasks_delete":
        return tasks_service.delete_task(
            args.get("task_id", ""), args.get("task_list_id")
        )

    if name == "memory_save":
        return save_user_memory(
            user_id,
            args.get("category", "općenito"),
            args.get("memory_key", "podatak"),
            args.get("value", ""),
        )

    if name == "memory_capture":
        return save_user_memory(
            user_id,
            args.get("category", "profile"),
            args.get("memory_key", "podatak"),
            args.get("value", ""),
            source="automatic",
            importance=args.get("importance", 3),
            expires_at=args.get("expires_at"),
        )

    if name == "memory_list":
        context = get_user_memory_context(user_id)
        return context if context else "Nema spremljenih trajnih podataka."

    if name == "memory_forget":
        return forget_user_memory(
            user_id,
            args.get("category", "općenito"),
            args.get("memory_key", "podatak"),
        )

    if name == "calendar_list_calendars":
        return calendar_service.list_calendars()

    if name == "calendar_list_events":
        return calendar_service.list_events(
            args.get("time_min"), args.get("time_max"),
            args.get("limit", 20), args.get("calendar_id"),
            args.get("start_from_beginning_of_day", False)
        )

    if name == "calendar_list_birthdays":
        birthdays = calendar_service.list_birthdays(
            args.get("time_min"), args.get("time_max"), args.get("limit", 100)
        )
        today = datetime.now(ZoneInfo(calendar_service.TIME_ZONE)).date()
        today_items = []
        other_items = []
        for birthday in birthdays:
            if _calendar_event_local_date(birthday) == today:
                today_items.append(birthday)
            else:
                other_items.append(birthday)
        return {
            "today": today_items,
            "other_birthdays_in_range": other_items,
            "total": len(birthdays),
            "instruction": "Stavke iz today obavezno navedi prve.",
        }

    if name == "calendar_get_event":
        return calendar_service.get_event(
            args.get("event_id", ""), args.get("calendar_id")
        )

    if name == "calendar_check_free_busy":
        return calendar_service.check_free_busy(
            args.get("time_min"), args.get("time_max"),
            args.get("calendar_ids")
        )

    if name == "calendar_create_event":
        return calendar_service.create_event(
            args.get("title", ""), args.get("start"), args.get("end"),
            args.get("description"), args.get("location"),
            args.get("calendar_id")
        )

    if name == "calendar_update_event":
        return calendar_service.update_event(
            args.get("event_id", ""), args.get("title"),
            args.get("start"), args.get("end"), args.get("description"),
            args.get("location"), args.get("calendar_id")
        )

    if name == "calendar_delete_event":
        return calendar_service.delete_event(
            args.get("event_id", ""), args.get("calendar_id")
        )

    if name == "spotify_current_track":

        return spotify_current_track()

    if name == "spotify_search_and_play":

        return spotify_search_and_play(
            args.get("query", "")
        )

    if name == "spotify_get_queue":

        return spotify_get_queue(
            args.get("limit", 10)
        )

    if name == "spotify_add_to_queue":

        return spotify_add_to_queue(
            args.get("query", "")
        )

    if name == "spotify_top_tracks":

        return spotify_top_tracks(
            args.get(
                "time_range",
                "medium_term"
            ),
            args.get(
                "limit",
                10
            ),
        )

    if name == "spotify_top_artists":

        return spotify_top_artists(
            args.get(
                "time_range",
                "medium_term"
            ),
            args.get(
                "limit",
                10
            ),
        )

    if name == "spotify_list_liked":

        return spotify_list_liked(
            args.get(
                "limit",
                20
            )
        )

    if name == "spotify_history_stats":

        return spotify_history_stats(
            args.get("start_date"),
            args.get("end_date"),
            args.get("limit", 10),
        )

    if name == "spotify_history_list":

        return spotify_history_list(
            args.get("start_date"),
            args.get("end_date"),
            args.get("limit", 50),
            args.get("create_playlist", False),
            args.get("playlist_name"),
        )

    # -----------------------------------------------------
    # DISCOVERY V2
    # -----------------------------------------------------

    if name == "spotify_discover_artists_v2":

        return spotify_discover_artists_v2(

            seed_artist=args.get(
                "seed_artist",
                ""
            ),

            candidate_artists=args.get(
                "candidate_artists",
                []
            ),

            artist_count=args.get(
                "artist_count",
                5
            ),

            tracks_per_artist=args.get(
                "tracks_per_artist",
                2
            ),

            exclude_favorites=args.get(
                "exclude_favorites",
                True
            ),

            exclude_top_artists=args.get(
                "exclude_top_artists",
                True
            ),

            min_liked_tracks_to_exclude=args.get(
                "min_liked_tracks_to_exclude",
                DISCOVERY_LIKED_THRESHOLD
            ),

            create_discovery_playlist=args.get(
                "create_discovery_playlist",
                False
            ),

            playlist_name=args.get(
                "playlist_name"
            ),
        )

    # -----------------------------------------------------
    # SMART PLAYLIST V3
    # -----------------------------------------------------

    if name == "spotify_create_smart_playlist_v3":

        return spotify_create_smart_playlist_v3(

            name=args.get(
                "name",
                "AI Smart Playlist"
            ),

            max_tracks=args.get(
                "max_tracks",
                30
            ),

            genres=args.get(
                "genres",
                []
            ),

            domestic_weight=args.get(
                "domestic_weight",
                0
            ),

            domestic_required=args.get(
                "domestic_required",
                False
            ),

            favorite_weight=args.get(
                "favorite_weight",
                0
            ),

            forgotten_weight=args.get(
                "forgotten_weight",
                0
            ),

            avoid_recent_days=args.get(
                "avoid_recent_days"
            ),

            recent_penalty=args.get(
                "recent_penalty",
                0
            ),

            max_recent_plays=args.get(
                "max_recent_plays"
            ),

            recent_period_days=args.get(
                "recent_period_days",
                30
            ),

            max_per_artist=args.get(
                "max_per_artist",
                2
            ),

            avoid_consecutive_artist=args.get(
                "avoid_consecutive_artist",
                True
            ),

            randomness=args.get(
                "randomness",
                1.0
            ),
        )

    return "Nepoznat alat."


# =========================================================
# TELEGRAM
# =========================================================

def _report_time(value):
    if not value:
        return "cijeli dan"
    if "T" not in value:
        return "cijeli dan"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo(calendar_service.TIME_ZONE)).strftime("%H:%M")


def build_morning_report():
    local_zone = ZoneInfo(calendar_service.TIME_ZONE)
    now = datetime.now(local_zone)
    start = datetime.combine(now.date(), time.min, tzinfo=local_zone)
    end = start + timedelta(days=1)

    events = calendar_service.list_events(
        start.isoformat(), end.isoformat(), limit=50
    )
    birthdays = calendar_service.list_birthdays(
        start.isoformat(), end.isoformat(), limit=50
    )
    birthday_keys = {
        (birthday.get("title"), birthday.get("start")) for birthday in birthdays
    }
    events = [
        event for event in events
        if (event.get("title"), event.get("start")) not in birthday_keys
    ]
    tasks = tasks_service.get_due_tasks(now.date())

    lines = [f"Dobro jutro. Danas je {now.strftime('%d.%m.%Y.')}", ""]

    lines.append("Kalendar:")
    if events:
        for event in events:
            start_text = _report_time(event.get("start"))
            end_text = _report_time(event.get("end"))
            period = start_text if start_text == "cijeli dan" else f"{start_text}–{end_text}"
            lines.append(f"• {period} {event.get('title', '(bez naziva)')}")
    else:
        lines.append("• Nema događaja.")

    lines.extend(["", "Rođendani:"])
    if birthdays:
        for birthday in birthdays:
            lines.append(f"• {birthday.get('title', '(bez naziva)')}")
    else:
        lines.append("• Danas nema rođendana.")

    lines.extend(["", "Zadaci za danas:"])
    if tasks["today"]:
        for task in tasks["today"]:
            lines.append(f"• {task['title']} ({task['task_list']})")
    else:
        lines.append("• Nema zadataka s rokom danas.")

    lines.extend(["", "Zakašnjeli zadaci:"])
    if tasks["overdue"]:
        for task in tasks["overdue"]:
            due = datetime.fromisoformat(task["due"]).strftime("%d.%m.%Y.")
            lines.append(f"• {task['title']} — rok {due} ({task['task_list']})")
    else:
        lines.append("• Nema zakašnjelih zadataka.")

    return "\n".join(lines)


async def morning_report_job(context: ContextTypes.DEFAULT_TYPE):
    subscribers = get_morning_report_subscribers()
    if not subscribers:
        return

    try:
        report = await asyncio.to_thread(build_morning_report)
    except Exception as error:
        print("JUTARNJI IZVJEŠTAJ:", repr(error))
        report = f"Jutarnji izvještaj nije moguće pripremiti: {error}"

    for user_id in subscribers:
        try:
            await context.bot.send_message(chat_id=user_id, text=report)
        except Exception as error:
            print("SLANJE JUTARNJEG IZVJEŠTAJA:", repr(error))


async def rijeka_news_job(context: ContextTypes.DEFAULT_TYPE):
    subscribers = get_rijeka_news_subscribers()
    if not subscribers:
        return

    for user_id in subscribers:
        try:
            digest = await asyncio.to_thread(build_rijeka_news_digest, user_id)
            chunks = rijeka_news_service.split_telegram_text(digest)
            for index, chunk in enumerate(chunks, 1):
                prefix = ""
                if len(chunks) > 1:
                    prefix = f"HNK Rijeka — dio {index}/{len(chunks)}\n\n"
                await context.bot.send_message(chat_id=user_id, text=prefix + chunk)
        except Exception as error:
            print("HNK RIJEKA DNEVNI PREGLED:", repr(error))
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "Dnevni pregled HNK Rijeke jutros nije izrađen zbog "
                    "tehničke pogreške. Pokušaj ponovno naredbom /rijekasada."
                ),
            )


async def history_sync_job(context: ContextTypes.DEFAULT_TYPE):
    global LAST_HISTORY_SYNC

    result = await asyncio.to_thread(spotify_sync_history)
    LAST_HISTORY_SYNC = datetime.now(timezone.utc)
    print("SPOTIFY HISTORY:", result)


async def stripovi_forum_job(context: ContextTypes.DEFAULT_TYPE):
    subscribers = get_stripovi_forum_subscribers()
    if not subscribers:
        return

    try:
        digest = await asyncio.to_thread(build_stripovi_forum_digest)
    except Exception as error:
        print("STRIPOVI.COM DNEVNI PREGLED:", repr(error))
        digest = (
            "Dnevni pregled sekcije Stripovi jutros nije izrađen zbog "
            "tehničke pogreške. Pokušaj ponovno naredbom /stripovisada."
        )

    chunks = rijeka_news_service.split_telegram_text(digest)
    for user_id in subscribers:
        try:
            for index, chunk in enumerate(chunks, 1):
                prefix = ""
                if len(chunks) > 1:
                    prefix = f"Stripovi.com — dio {index}/{len(chunks)}\n\n"
                await context.bot.send_message(chat_id=user_id, text=prefix + chunk)
        except Exception as error:
            print("SLANJE STRIPOVI.COM PREGLEDA:", repr(error))

WRITE_CONFIRMATION_TOOLS = {
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "tasks_create",
    "tasks_update",
    "tasks_delete",
}


def calendar_confirmation_text(name, args):
    if name == "tasks_create":
        return (
            "Potvrdi stvaranje zadatka:\n\n"
            f"Zadatak: {args.get('title')}\n"
            f"Rok: {args.get('due') or '-'}"
        )

    if name == "tasks_update":
        changed = []
        for key, label in (("title", "Zadatak"), ("due", "Rok"), ("notes", "Bilješka")):
            if args.get(key) is not None:
                changed.append(f"{label}: {args.get(key)}")
        return "Potvrdi izmjenu zadatka:\n\n" + "\n".join(changed)

    if name == "tasks_delete":
        return f"Potvrdi brisanje zadatka:\n\n{args.get('title', '(bez naziva)')}"

    if name == "calendar_create_event":
        return (
            "Potvrdi kreiranje događaja:\n\n"
            f"Naziv: {args.get('title')}\n"
            f"Početak: {args.get('start')}\n"
            f"Kraj: {args.get('end')}\n"
            f"Lokacija: {args.get('location') or '-'}"
        )

    if name == "calendar_update_event":
        changed = []
        for key, label in (
            ("title", "Naziv"), ("start", "Početak"), ("end", "Kraj"),
            ("location", "Lokacija"), ("description", "Opis")
        ):
            if args.get(key) is not None:
                changed.append(f"{label}: {args.get(key)}")
        return "Potvrdi izmjenu događaja:\n\n" + "\n".join(changed)

    return f"Potvrdi brisanje događaja:\n\n{args.get('title', '(bez naziva)')}"


async def send_next_confirmation(message, context: ContextTypes.DEFAULT_TYPE):
    pending_actions = context.user_data.get("pending_calendar_actions") or []
    if not pending_actions:
        return

    pending = pending_actions[0]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Potvrdi", callback_data="calendar_confirm"
        ),
        InlineKeyboardButton(
            "Odustani", callback_data="calendar_cancel"
        ),
    ]])
    confirmation_text = calendar_confirmation_text(pending["name"], pending["args"])
    if len(pending_actions) > 1:
        confirmation_text += f"\n\n(još {len(pending_actions) - 1} radnji čeka potvrdu)"
    save_message(pending["user_id"], "assistant", confirmation_text)
    await message.reply_text(
        confirmation_text,
        reply_markup=keyboard,
    )


async def calendar_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending_actions = context.user_data.get("pending_calendar_actions") or []
    pending = pending_actions.pop(0) if pending_actions else None

    if not pending:
        await query.edit_message_text("Ovaj zahtjev više nije aktivan.")
        return

    try:
        await _run_confirmed_action(query, pending)
    finally:
        await send_next_confirmation(query.message, context)


async def _run_confirmed_action(query, pending):
    if query.data == "calendar_cancel":
        message = "Radnja je otkazana."
        save_message(pending["user_id"], "assistant", message)
        await query.edit_message_text(message)
        return

    try:
        result = await asyncio.to_thread(
            execute_tool,
            pending["name"], pending["args"], pending["user_id"]
        )
        if pending["name"] == "calendar_delete_event":
            message = "Događaj je obrisan."
        elif pending["name"] == "tasks_delete":
            message = "Zadatak je obrisan."
        elif pending["name"].startswith("tasks_"):
            message = "Radnja nad zadatkom je izvršena.\n\n" + result.get("title", "")
        else:
            message = (
                "Radnja je izvršena.\n\n"
                f"{result.get('title', '')}\n"
                f"{result.get('start', '')} – {result.get('end', '')}"
            ).strip()
        save_message(pending["user_id"], "assistant", message)
        await query.edit_message_text(message)
    except Exception as e:
        print("CALENDAR POTVRDA:", repr(e))
        await query.edit_message_text(f"Google Calendar greška: {e}")

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        f"Osobni AI agent za Spotify i Google Calendar radi.\n"
        f"Verzija: {BOT_VERSION}\n\n"

        "Primjeri:\n\n"

        "• Pusti A Forest od The Cure\n"
        "• Pauziraj\n"
        "• Glasnoća 30%\n"
        "• Dodaj London Calling u queue\n"
        "• Top 10 pjesama zadnjih mjesec dana\n"
        "• Što sam slušao jučer?\n"
        "• Napravi playlistu od onog što sam slušao jučer\n\n"

        "Discovery:\n\n"

        "• Daj mi 5 bendova sličnih Fontaines D.C. "
        "koje nemam među favoritima i 2 pjesme svakog\n\n"

        "• Daj mi 8 bendova kao IDLES koje ne slušam\n\n"

        "• Napravi discovery playlistu s bendovima "
        "sličnim Joy Divisionu koje nemam među favoritima, "
        "2 pjesme po bendu\n\n"

        "Smart Playlist:\n\n"

        "• Napravi 40 mojih Liked Songs, uglavnom punk, "
        "malo post-punka, preferiraj domaće i izbjegni "
        "ono što sam slušao zadnjih 30 dana\n\n"

        "Google Calendar:\n\n"
        "• /googleauth za prijavu u Google račun\n"
        "• Što imam sutra u kalendaru?\n"
        "• Jesam li slobodan u petak od 10 do 11?\n"
        "• Dodaj sastanak u petak od 10 do 11\n\n"

        "HNK Rijeka vijesti:\n\n"
        "• /rijeka — uključi dnevni pregled u 08:30\n"
        "• /rijekasada — pripremi pregled odmah\n"
        "• /bezrijeke — isključi dnevni pregled\n\n"

        "Stripovi.com — sekcija Stripovi:\n\n"
        "• /stripovi — uključi dnevni pregled u 08:40\n"
        "• /stripovisada — pripremi pregled odmah\n"
        "• /bezstripova — isključi dnevni pregled\n\n"

        "Memorija:\n\n"
        "• Zapamti da vozim Dacia Duster na LPG.\n"
        "• Što pamtiš o meni?\n"
        "• /memorija"
    )


async def clear_memory(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    clear_messages(
        update.effective_user.id
    )

    await update.message.reply_text(
        "Chat memorija obrisana."
    )


async def show_version(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        f"Pero AI verzija: {BOT_VERSION}\n"
        f"Datoteka: {os.path.abspath(__file__)}"
    )


async def debug_birthdays_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Provjeravam sirove Calendar podatke za jučer, danas i sutra..."
    )
    try:
        debug = await asyncio.to_thread(calendar_service.diagnose_birthdays)
        lines = [
            f"Lokalno vrijeme: {debug['local_now']}",
            f"Ciljani datum: {debug['target_date']}",
            "",
            "Kalendari:",
        ]
        for calendar in debug["calendars"]:
            marker = " [kalendar rođendana]" if calendar["birthdays_calendar"] else ""
            lines.append(
                f"• {calendar['name']} ({calendar['access']}){marker}"
            )

        lines.extend(["", "Događaji koje Google API vraća oko današnjeg dana:"])
        event_rows = debug["events_around_target"]
        for event in event_rows[:40]:
            if event.get("error"):
                lines.append(f"• {event['calendar']}: GREŠKA {event['error']}")
                continue
            today_marker = " DANAS" if event["is_target_date"] else ""
            birthday_marker = " ROĐENDAN" if event["recognized_as_birthday"] else ""
            lines.append(
                f"• {event['start']} | {event['calendar']} | {event['title']} | "
                f"type={event['event_type']}{today_marker}{birthday_marker}"
            )
        if len(event_rows) > 40:
            lines.append(f"• ... još {len(event_rows) - 40} događaja")

        lines.extend(["", "Rođendani prepoznati za danas:"])
        detected = debug["detected_birthdays_today"]
        if detected:
            for birthday in detected:
                lines.append(
                    f"• {birthday.get('start')} | {birthday.get('title')} | "
                    f"{birthday.get('calendar')} | type={birthday.get('event_type')}"
                )
        else:
            lines.append("• NITI JEDAN")

        output = "\n".join(lines)
        await update.message.reply_text(output[:4096])
    except Exception as error:
        print("DEBUG ROĐENDANI:", repr(error))
        await update.message.reply_text(f"Debug rođendana nije uspio: {error}")


async def show_memory(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Trajna memorija:\n" + get_user_memory_context(update.effective_user.id)
    )


async def enable_morning_report_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    enable_morning_report(update.effective_user.id)
    await update.message.reply_text(
        "Jutarnji izvještaj je uključen: pon–pet u 07:30, vikendom u 09:00."
    )


async def disable_morning_report_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    disable_morning_report(update.effective_user.id)
    await update.message.reply_text("Jutarnji izvještaj je isključen.")


async def enable_rijeka_news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    enable_rijeka_news(update.effective_user.id)
    await update.message.reply_text(
        "Dnevni HNK Rijeka pregled je uključen: svaki dan u 08:30."
    )


async def disable_rijeka_news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    disable_rijeka_news(update.effective_user.id)
    await update.message.reply_text("Dnevni HNK Rijeka pregled je isključen.")


async def rijeka_news_now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Dohvaćam vijesti i Reddit izravno te jednom pretražujem X i forume. "
        "Detaljan pregled može potrajati nekoliko minuta."
    )
    try:
        digest = await asyncio.to_thread(build_rijeka_news_digest, user_id)
        chunks = rijeka_news_service.split_telegram_text(digest)
        for index, chunk in enumerate(chunks, 1):
            prefix = ""
            if len(chunks) > 1:
                prefix = f"HNK Rijeka — dio {index}/{len(chunks)}\n\n"
            await update.message.reply_text(prefix + chunk)
    except Exception as error:
        print("HNK RIJEKA RUČNI PREGLED:", repr(error))
        await update.message.reply_text(
            "Pregled HNK Rijeke trenutačno nije izrađen zbog tehničke "
            "pogreške. Pokušaj ponovno za nekoliko minuta."
        )


async def enable_stripovi_forum_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    enable_stripovi_forum_report(update.effective_user.id)
    await update.message.reply_text(
        "Dnevni pregled sekcije Stripovi uključen je: svaki dan u 08:40."
    )


async def disable_stripovi_forum_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    disable_stripovi_forum_report(update.effective_user.id)
    await update.message.reply_text(
        "Dnevni pregled sekcije Stripovi je isključen."
    )


async def stripovi_forum_now_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Čitam teme i poruke iz posljednja 24 sata samo u sekciji Stripovi "
        "(FORUM_ID=5). Pregled može potrajati nekoliko minuta."
    )
    try:
        digest = await asyncio.to_thread(build_stripovi_forum_digest)
        chunks = rijeka_news_service.split_telegram_text(digest)
        for index, chunk in enumerate(chunks, 1):
            prefix = ""
            if len(chunks) > 1:
                prefix = f"Stripovi.com — dio {index}/{len(chunks)}\n\n"
            await update.message.reply_text(prefix + chunk)
    except Exception as error:
        print("STRIPOVI.COM RUČNI PREGLED:", repr(error))
        await update.message.reply_text(
            "Pregled sekcije Stripovi trenutačno nije izrađen zbog tehničke "
            "pogreške. Pokušaj ponovno za nekoliko minuta."
        )


async def google_auth_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Otvaram Google prijavu u pregledniku. Potvrdi pristup Calendaru i Tasksima."
    )
    try:
        await asyncio.to_thread(calendar_service.get_credentials)
        await update.message.reply_text("Google Calendar i Google Tasks su autorizirani.")
    except Exception as error:
        await update.message.reply_text(f"Google autorizacija nije uspjela: {error}")


async def sync_spotify(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        spotify_sync_history()
    )


# =========================================================
# CHAT
# =========================================================

async def chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    user_message = (
        update.message.text
    )

    # History samo povremeno.
    try:

        await asyncio.to_thread(
            sync_history_if_needed
        )

    except Exception:

        pass

    # Besplatne lokalne komande.
    try:

        local_result = await asyncio.to_thread(
            try_local_command,
            user_message
        )

        if local_result is not None:

            # I lokalne Spotify radnje su dio razgovora: bez ovoga bi model
            # već u sljedećoj poruci izgubio činjenicu da je radnja izvršena.
            save_message(user_id, "user", user_message)
            save_message(user_id, "assistant", str(local_result))

            await update.message.reply_text(
                local_result
            )

            return

    except Exception as e:

        print(
            "Local command:",
            e
        )

    save_message(
        user_id,
        "user",
        user_message,
    )

    messages = get_messages(
        user_id
    )

    model_name = choose_model(
        user_message
    )

    print(
        "\nMODEL:",
        model_name
    )

    try:

        response = await asyncio.to_thread(
            client.responses.create,
            model=model_name,
            instructions=(
                SYSTEM_PROMPT
                + "\nTrenutni lokalni datum i vrijeme: "
                + datetime.now(ZoneInfo(calendar_service.TIME_ZONE)).isoformat()
                + "\n\nTrajna memorija korisnika:\n"
                + get_relevant_user_memory_context(user_id, user_message)
            ),
            input=messages,
            tools=TOOLS,
        )

        for _ in range(6):

            tool_results = []
            write_actions = []

            for item in response.output:

                if item.type != "function_call":
                    continue

                try:

                    args = json.loads(
                        item.arguments
                    )

                except Exception:

                    args = {}

                print(
                    "\n================================="
                )

                print(
                    "TOOL:",
                    item.name
                )

                print(
                    "ARGS:"
                )

                print(
                    json.dumps(
                        args,
                        ensure_ascii=False,
                        indent=2,
                    )
                )

                if item.name in WRITE_CONFIRMATION_TOOLS:
                    write_actions.append({
                        "name": item.name,
                        "args": args,
                        "user_id": user_id,
                    })
                    continue

                result = await asyncio.to_thread(
                    execute_tool,
                    item.name,
                    args,
                    user_id,
                )

                print(
                    "\nRESULT:"
                )

                print(
                    result
                )

                print(
                    "=================================\n"
                )

                tool_results.append(
                    {
                        "type":
                            "function_call_output",

                        "call_id":
                            item.call_id,

                        "output":
                            str(result),
                    }
                )

            if write_actions:
                # Svaka radnja dobiva vlastiti gumb Potvrdi, jedna za drugom.
                context.user_data["pending_calendar_actions"] = write_actions
                await send_next_confirmation(
                    update.message,
                    context
                )
                return

            if not tool_results:
                break

            response = await asyncio.to_thread(
                client.responses.create,
                model=model_name,
                previous_response_id=response.id,
                input=tool_results,
                tools=TOOLS,
                instructions=(
                    "Odgovori kratko i prirodno na hrvatskom. "
                    "Ne izmišljaj rezultat. "
                    "Trenutni lokalni datum i vrijeme je "
                    + datetime.now(ZoneInfo(calendar_service.TIME_ZONE)).isoformat()
                    + ".\n\nTrajna memorija korisnika:\n"
                    + get_relevant_user_memory_context(user_id, user_message)
                    + "\n"
                    "Ako je discovery alat izbacio poznate "
                    "izvođače, nemoj ih ponovno preporučiti."
                ),
            )

        answer = response.output_text

        if not answer:

            answer = "Radnja je obrađena."

        save_message(
            user_id,
            "assistant",
            answer,
        )

        await update.message.reply_text(
            answer
        )

    except Exception as e:

        print(
            "GLAVNA GREŠKA:",
            repr(e)
        )

        await update.message.reply_text(
            "Dogodila se greška. "
            "Provjeri Command Prompt."
        )


async def reject_unknown_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if user and str(user.id) == TELEGRAM_ALLOWED_USER_ID:
        return

    print(
        "ODBIJEN KORISNIK:",
        user.id if user else None
    )

    raise ApplicationHandlerStop


# =========================================================
# MAIN
# =========================================================

def main():

    init_database()

    print(f"PERO AI VERZIJA: {BOT_VERSION}")
    print(f"BOT DATOTEKA: {os.path.abspath(__file__)}")

    try:

        print(
            spotify_sync_history()
        )

    except Exception as e:

        print(
            "Startup history:",
            e
        )

    if not TELEGRAM_ALLOWED_USER_ID.isdigit():
        raise RuntimeError(
            "U .env postavi TELEGRAM_ALLOWED_USER_ID na svoj Telegram ID."
        )

    app = (
        ApplicationBuilder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    # Grupa -1 se izvršava prije svih ostalih handlera: poruke i gumbi
    # drugih korisnika se odbacuju prije nego dođu do Spotifyja ili Googlea.
    app.add_handler(
        TypeHandler(
            Update,
            reject_unknown_user
        ),
        group=-1,
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "verzija",
            show_version
        )
    )

    app.add_handler(
        CommandHandler(
            "debugrodendani",
            debug_birthdays_command
        )
    )

    app.add_handler(
        CommandHandler(
            "clear",
            clear_memory
        )
    )

    app.add_handler(
        CommandHandler(
            "memorija",
            show_memory
        )
    )

    app.add_handler(
        CommandHandler(
            "jutarnji",
            enable_morning_report_command
        )
    )

    app.add_handler(
        CommandHandler(
            "bezjutarnjeg",
            disable_morning_report_command
        )
    )

    app.add_handler(
        CommandHandler(
            "rijeka",
            enable_rijeka_news_command
        )
    )

    app.add_handler(
        CommandHandler(
            "bezrijeke",
            disable_rijeka_news_command
        )
    )

    app.add_handler(
        CommandHandler(
            "rijekasada",
            rijeka_news_now_command
        )
    )

    app.add_handler(
        CommandHandler(
            "stripovi",
            enable_stripovi_forum_command
        )
    )

    app.add_handler(
        CommandHandler(
            "bezstripova",
            disable_stripovi_forum_command
        )
    )

    app.add_handler(
        CommandHandler(
            "stripovisada",
            stripovi_forum_now_command
        )
    )

    app.add_handler(
        CommandHandler(
            "googleauth",
            google_auth_command
        )
    )

    app.add_handler(
        CommandHandler(
            "syncspotify",
            sync_spotify
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            calendar_confirmation,
            pattern=r"^calendar_(confirm|cancel)$",
        )
    )

    if not app.job_queue:
        raise RuntimeError(
            "Nedostaje JobQueue. Pokreni: pip install -r requirements.txt"
        )

    app.job_queue.run_repeating(
        history_sync_job,
        interval=HISTORY_BACKGROUND_SYNC_MINUTES * 60,
        first=HISTORY_BACKGROUND_SYNC_MINUTES * 60,
        name="spotify_history_sync",
    )

    local_zone = ZoneInfo(calendar_service.TIME_ZONE)
    app.job_queue.run_daily(
        morning_report_job,
        time=time(hour=7, minute=30, tzinfo=local_zone),
        days=(1, 2, 3, 4, 5),
        name="jutarnji_radni_dan",
    )
    app.job_queue.run_daily(
        morning_report_job,
        time=time(hour=9, minute=0, tzinfo=local_zone),
        days=(0, 6),
        name="jutarnji_vikend",
    )
    app.job_queue.run_daily(
        rijeka_news_job,
        time=time(
            hour=RIJEKA_NEWS_HOUR,
            minute=RIJEKA_NEWS_MINUTE,
            tzinfo=local_zone,
        ),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="hnk_rijeka_dnevni_pregled",
    )
    app.job_queue.run_daily(
        stripovi_forum_job,
        time=time(
            hour=STRIPOVI_FORUM_HOUR,
            minute=STRIPOVI_FORUM_MINUTE,
            tzinfo=local_zone,
        ),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="stripovi_forum_dnevni_pregled",
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            chat
        )
    )

    print(
        "Osobni AI agent za Spotify i Google Calendar radi..."
    )

    app.run_polling()


if __name__ == "__main__":

    main()
