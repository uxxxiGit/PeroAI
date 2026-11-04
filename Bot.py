import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
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
import spotify_service
import stripovi_forum_service
import tasks_service

# =========================================================
# ENV
# =========================================================

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()


# =========================================================
# CONFIG
# =========================================================

DB_FILE = "memory.db"
BOT_VERSION = "2026.11.04-single-v12"

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

# Spotify vraća samo zadnjih 50 slušanja (~2,5 h), pa se povijest mora
# dohvaćati u pozadini, neovisno o tome piše li korisnik botu.
HISTORY_BACKGROUND_SYNC_MINUTES = 20


# =========================================================
# OPENAI
# =========================================================

client = OpenAI(
    api_key=OPENAI_API_KEY
)


SYSTEM_PROMPT = """
Ti si korisnikov osobni AI agent za Spotify, Google Calendar i Google Tasks.

Odgovaraj kratko, prirodno i na hrvatskom. Ne završavaj odgovor ponudama
tipa "Želiš li još...", "Mogu i...". Odgovori i stani.

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

SPOTIFY:

Spotify radnje izvrši ODMAH pozivom alata. Nikad ne traži potvrdu za Spotify
i ne postavljaj dodatna pitanja kad se zahtjev razumije. Ne pitaj za ime
playliste (smisli kratko ime), javnost (uvijek privatna) ni redoslijed
(zadrži korisnikov). Pitaj samo ako uopće nije jasno koju glazbu korisnik želi.
Ako korisnik napiše ime s tipfelerom ("fontained dc"), pošalji ga takvog;
alat sam pronalazi pravog izvođača.

Koji alat:
- "playlista od 20 pjesama Fontaines D.C." / "playlista s Majke i KUD Idijoti,
  po 10 pjesama" → spotify_playlist_from_artists. U playlisti su SAMO pjesme
  navedenih izvođača. Ako je zadan ukupan broj za više izvođača, podijeli ga
  ravnomjerno.
- korisnik pošalje popis pjesama → spotify_playlist_from_tracks, svaku pjesmu
  kao "Izvođač - Naslov", istim redom.
- "dodaj X u playlistu Y" → spotify_add_to_playlist.
- "izvođači slični X", "bendovi kao X koje ne slušam", discovery →
  spotify_similar_artists. Sam predloži 15-25 stilski srodnih kandidata
  (bez X). Seed izvođač NIJE u rezultatu. only_new=true kad korisnik traži
  nove/nepoznate izvođače. create_playlist=true samo kad traži playlistu.
- playlista iz Liked Songs / "mojih pjesama" po žanru, raspoloženju, domaćem
  → spotify_playlist_from_liked. "uglavnom punk, malo post-punka" →
  genres=[punk 5, post-punk 2]. Žanrovi na engleskom, malim slovima.
- "što sam slušao jučer/danas/u subotu" → spotify_history_list;
  "napravi playlistu od onog što sam slušao jučer" → isti alat s
  create_playlist=true; "što/koga sam najviše slušao" → spotify_history_stats.
  Datumi su lokalni (Europe/Zagreb), YYYY-MM-DD.
- "pusti X" → spotify_play (kind: track, artist, album ili playlist).
- "stavi u red" → spotify_add_to_queue s popisom pjesama.
- top pjesme/izvođači sa Spotifyja → spotify_top. short_term ≈ 4 tjedna,
  medium_term ≈ 6 mjeseci, long_term = dugoročno.

Kad alat napravi playlistu, odgovori imenom, brojem pjesama i linkom, te
navedi što nije pronađeno. Nemoj nabrajati sve pjesme.

Nikad ne tvrdi da je radnja uspjela prije rezultata alata.
"""


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


LOCAL_SPOTIFY_COMMANDS = {
    spotify_service.current_track: (
        "što svira", "sto svira", "što trenutno svira", "sto trenutno svira",
        "što sad svira", "sto sad svira", "šta svira", "šta sad svira",
    ),
    spotify_service.pause: ("pauza", "pauziraj", "stop", "stani"),
    spotify_service.resume: (
        "nastavi", "play", "nastavi glazbu", "nastavi spotify", "pusti dalje",
    ),
    spotify_service.next_track: (
        "dalje", "sljedeća", "sljedeca", "sljedeća pjesma", "sljedeca pjesma",
        "preskoči", "preskoci", "preskoči pjesmu", "preskoci pjesmu", "next",
    ),
    spotify_service.previous_track: (
        "prethodna", "prethodna pjesma", "vrati prethodnu", "nazad",
    ),
    spotify_service.devices: ("uređaji", "uredaji", "spotify uređaji"),
}


def try_local_command(message):

    birthdays = _try_local_birthdays_command(message)
    if birthdays is not None:
        return birthdays

    # Brze naredbe reagiraju samo kad je fraza cijela poruka, da npr.
    # "dodaj pauza za ručak u kalendar" ne zaustavi glazbu.
    text = re.sub(r"[?!.,]+$", "", message.lower().strip()).strip()

    for action, phrases in LOCAL_SPOTIFY_COMMANDS.items():
        if text in phrases:
            return action()

    volume = re.fullmatch(r"(?:glasnoća|glasnoca|volume|vol)\s*(\d{1,3})\s*%?", text)
    if volume:
        return spotify_service.set_volume(volume.group(1))

    if text in ("shuffle", "shuffle on", "uključi shuffle", "ukljuci shuffle"):
        return spotify_service.set_shuffle(True)

    if text in ("shuffle off", "isključi shuffle", "iskljuci shuffle", "shuffle isključi"):
        return spotify_service.set_shuffle(False)

    return None


# =========================================================
# MODEL ROUTER
# =========================================================

# Spotify zahtjevi traže dobro razumijevanje glazbe (žanrovi, slični
# izvođači, raspodjela pjesama), pa za njih nano model nije dovoljan.
SMART_KEYWORDS = (
    "playlist", "pjesm", "izvođ", "izvod", "bend", "grup", "album",
    "slič", "slic", "discovery", "slušao", "slusao", "slušam", "slusam",
    "liked", "žanr", "zanr", "punk", "rock", "jazz", "pusti", "queue",
    "u red", "spotify", "glazb", "analiziraj", "usporedi", "preferiraj",
)


def choose_model(message):

    text = message.lower()

    if any(keyword in text for keyword in SMART_KEYWORDS):
        return SMART_MODEL

    return CHEAP_MODEL


# =========================================================
# OPENAI TOOLS
# =========================================================

SPOTIFY_TOOLS = [
    {
        "type": "function",
        "name": "spotify_now_playing",
        "description": "Što trenutno svira na Spotifyju.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_play",
        "description": "Pusti pjesmu, izvođača, album ili playlistu na aktivnom uređaju.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Za pjesmu: 'Izvođač - Naslov'."}, "kind": {"type": "string", "enum": ["track", "artist", "album", "playlist"]}}, "required": ["query", "kind"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_get_queue",
        "description": "Prikaži Spotify red reprodukcije.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_add_to_queue",
        "description": "Dodaj jednu ili više pjesama u red, zadanim redoslijedom.",
        "parameters": {"type": "object", "properties": {"tracks": {"type": "array", "items": {"type": "string"}, "description": "Pjesme kao 'Izvođač - Naslov'."}}, "required": ["tracks"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_playlist_from_artists",
        "description": "Napravi playlistu isključivo od pjesama navedenih izvođača (najpoznatije prve). Npr. '20 pjesama Fontaines D.C.'.",
        "parameters": {"type": "object", "properties": {"artists": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "count": {"type": "integer", "description": "Broj pjesama tog izvođača."}}, "required": ["name", "count"], "additionalProperties": False}}, "name": {"type": ["string", "null"], "description": "Ime playliste; null = automatski."}, "mix": {"type": "boolean", "description": "Izvođači naizmjence (true) ili redom po izvođaču (false)."}}, "required": ["artists", "name", "mix"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_playlist_from_tracks",
        "description": "Napravi playlistu od zadanog popisa pjesama, istim redom.",
        "parameters": {"type": "object", "properties": {"tracks": {"type": "array", "items": {"type": "string"}, "description": "Pjesme kao 'Izvođač - Naslov'."}, "name": {"type": ["string", "null"], "description": "Ime playliste; null = automatski."}}, "required": ["tracks", "name"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_add_to_playlist",
        "description": "Dodaj pjesme u postojeću korisnikovu playlistu.",
        "parameters": {"type": "object", "properties": {"playlist_name": {"type": "string"}, "tracks": {"type": "array", "items": {"type": "string"}, "description": "Pjesme kao 'Izvođač - Naslov'."}}, "required": ["playlist_name", "tracks"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_similar_artists",
        "description": "Izvođači slični zadanom (discovery). Ti predlažeš kandidate, alat provjerava postoje li na Spotifyju i sluša li ih korisnik već.",
        "parameters": {"type": "object", "properties": {"seed_artist": {"type": "string"}, "candidate_artists": {"type": "array", "items": {"type": "string"}, "description": "15-25 stilski srodnih izvođača, bez seed izvođača."}, "artist_count": {"type": "integer"}, "tracks_per_artist": {"type": "integer"}, "only_new": {"type": "boolean", "description": "Izbaci izvođače koje korisnik već sluša."}, "create_playlist": {"type": "boolean"}, "playlist_name": {"type": ["string", "null"], "description": "Ime playliste; null = automatski."}}, "required": ["seed_artist", "candidate_artists", "artist_count", "tracks_per_artist", "only_new", "create_playlist", "playlist_name"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_playlist_from_liked",
        "description": "Napravi playlistu iz korisnikovih Liked Songs prema žanrovima, domaćim izvođačima ili raspoloženju.",
        "parameters": {"type": "object", "properties": {"name": {"type": ["string", "null"], "description": "Ime playliste; null = automatski."}, "track_count": {"type": "integer"}, "genres": {"type": "array", "description": "Udio pjesama po žanru; prazno = bez žanrovskog filtra.", "items": {"type": "object", "properties": {"name": {"type": "string"}, "weight": {"type": "number"}}, "required": ["name", "weight"], "additionalProperties": False}}, "domestic": {"type": "string", "enum": ["any", "prefer", "only", "exclude"], "description": "Domaći = izvođači s prostora bivše Jugoslavije."}, "artists": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Samo pjesme ovih izvođača iz Liked Songs."}, "mode": {"type": "string", "enum": ["mixed", "favorites", "forgotten"], "description": "favorites = najslušanije, forgotten = dugo neslušane."}, "avoid_recent_days": {"type": ["integer", "null"], "description": "Izbaci pjesme slušane u zadnjih N dana."}, "max_per_artist": {"type": "integer"}}, "required": ["name", "track_count", "genres", "domestic", "artists", "mode", "avoid_recent_days", "max_per_artist"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_top",
        "description": "Korisnikove top pjesme ili top izvođači prema Spotifyju.",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["tracks", "artists"]}, "time_range": {"type": "string", "enum": ["short_term", "medium_term", "long_term"]}, "limit": {"type": "integer"}}, "required": ["kind", "time_range", "limit"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_list_liked",
        "description": "Zadnje dodane Liked Songs.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_history_list",
        "description": "Kronološki popis pjesama koje je korisnik slušao u lokalnom datumskom periodu (npr. jučer). Opcionalno od njih napravi playlistu.",
        "parameters": {"type": "object", "properties": {"start_date": {"type": "string", "description": "YYYY-MM-DD"}, "end_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, uključivo; null = isti dan."}, "limit": {"type": "integer"}, "create_playlist": {"type": "boolean"}, "playlist_name": {"type": ["string", "null"], "description": "Ime playliste; null = automatski."}}, "required": ["start_date", "end_date", "limit", "create_playlist", "playlist_name"], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "spotify_history_stats",
        "description": "Najslušanije pjesme ili izvođači iz lokalne povijesti za period.",
        "parameters": {"type": "object", "properties": {"start_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"}, "end_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, uključivo"}, "limit": {"type": "integer"}, "group_by": {"type": "string", "enum": ["track", "artist"]}}, "required": ["start_date", "end_date", "limit", "group_by"], "additionalProperties": False},
    },
]


TOOLS = [
    *SPOTIFY_TOOLS,

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

def execute_spotify_tool(name, args):

    if name == "spotify_now_playing":
        return spotify_service.current_track()

    if name == "spotify_play":
        return spotify_service.play(args.get("query", ""), args.get("kind", "track"))

    if name == "spotify_get_queue":
        return spotify_service.get_queue(args.get("limit", 10))

    if name == "spotify_add_to_queue":
        return spotify_service.add_to_queue(args.get("tracks") or [])

    if name == "spotify_playlist_from_artists":
        return spotify_service.playlist_from_artists(
            args.get("artists") or [], args.get("name"), args.get("mix", True)
        )

    if name == "spotify_playlist_from_tracks":
        return spotify_service.playlist_from_tracks(
            args.get("tracks") or [], args.get("name")
        )

    if name == "spotify_add_to_playlist":
        return spotify_service.add_tracks_to_existing_playlist(
            args.get("playlist_name", ""), args.get("tracks") or []
        )

    if name == "spotify_similar_artists":
        return spotify_service.similar_artists(
            args.get("seed_artist", ""),
            args.get("candidate_artists") or [],
            args.get("artist_count", 5),
            args.get("tracks_per_artist", 2),
            args.get("only_new", True),
            args.get("create_playlist", False),
            args.get("playlist_name"),
        )

    if name == "spotify_playlist_from_liked":
        return spotify_service.playlist_from_liked(
            name=args.get("name"),
            track_count=args.get("track_count", 30),
            genres=args.get("genres") or [],
            domestic=args.get("domestic", "any"),
            artists=args.get("artists"),
            mode=args.get("mode", "mixed"),
            avoid_recent_days=args.get("avoid_recent_days"),
            max_per_artist=args.get("max_per_artist", 3),
        )

    if name == "spotify_top":
        top = (
            spotify_service.top_artists
            if args.get("kind") == "artists"
            else spotify_service.top_tracks
        )
        return top(args.get("time_range", "short_term"), args.get("limit", 10))

    if name == "spotify_list_liked":
        return spotify_service.list_liked(args.get("limit", 20))

    if name == "spotify_history_list":
        return spotify_service.history_list(
            args.get("start_date"),
            args.get("end_date"),
            args.get("limit", 50),
            args.get("create_playlist", False),
            args.get("playlist_name"),
        )

    if name == "spotify_history_stats":
        return spotify_service.history_stats(
            args.get("start_date"),
            args.get("end_date"),
            args.get("limit", 10),
            args.get("group_by", "track"),
        )

    return "Nepoznat Spotify alat."


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

    if name.startswith("spotify_"):
        return execute_spotify_tool(name, args)

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

    if tasks.get("upcoming"):
        lines.extend(["", "Uskoro (7 dana):"])
        for task in tasks["upcoming"]:
            due = datetime.fromisoformat(task["due"]).strftime("%d.%m.")
            lines.append(f"• {due} {task['title']} ({task['task_list']})")

    if tasks.get("without_due"):
        lines.extend(["", "Zadaci bez roka:"])
        for task in tasks["without_due"]:
            lines.append(f"• {task['title']} ({task['task_list']})")

    # Verzija pomaže prepoznati koja je instanca bota poslala izvještaj.
    lines.extend(["", f"Pero AI {BOT_VERSION}"])

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
    result = await asyncio.to_thread(spotify_service.sync_history)
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
        "• pauza / dalje / glasnoća 30\n"
        "• Stavi u red London Calling i Rudie Can't Fail\n"
        "• Top 10 pjesama zadnjih mjesec dana\n"
        "• Što sam slušao jučer?\n\n"

        "Playliste:\n\n"
        "• Napravi playlistu od 20 pjesama Fontaines D.C.\n"
        "• Playlista: Majke i KUD Idijoti, po 10 pjesama\n"
        "• Napravi playlistu od ovih pjesama: ...\n"
        "• Napravi playlistu od onog što sam slušao jučer\n"
        "• 5 bendova sličnih IDLES koje ne slušam, 2 pjesme svakog, kao playlistu\n"
        "• 40 mojih Liked Songs, uglavnom punk, malo post-punka, "
        "preferiraj domaće, bez onog što sam slušao zadnjih 30 dana\n\n"

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
        await asyncio.to_thread(spotify_service.sync_history)
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
            spotify_service.sync_history_if_needed
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

    # Responses API ne prenosi instructions kroz previous_response_id, pa se
    # isti prompt šalje u svakom koraku; inače model nakon prvog alata
    # zaboravi pravila (npr. da za Spotify ne traži potvrdu).
    instructions = (
        SYSTEM_PROMPT
        + "\nTrenutni lokalni datum i vrijeme: "
        + datetime.now(ZoneInfo(calendar_service.TIME_ZONE)).isoformat()
        + "\n\nTrajna memorija korisnika:\n"
        + get_relevant_user_memory_context(user_id, user_message)
    )

    try:

        response = await asyncio.to_thread(
            client.responses.create,
            model=model_name,
            instructions=instructions,
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
                instructions=instructions,
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

LOCK_FILE = "bot.lock"


def acquire_single_instance_lock():

    # Svaka pokrenuta instanca ima vlastiti JobQueue, pa bi dvije instance
    # poslale dva jutarnja izvještaja. Zaključana datoteka ostaje zauzeta
    # dok proces radi i oslobađa se sama kad se proces zatvori.
    lock = open(LOCK_FILE, "a+")

    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            "Pero AI već radi na ovom računalu. Zatvori postojeći prozor "
            "bota (Ctrl+C) prije novog pokretanja."
        )

    return lock


async def handle_error(update, context: ContextTypes.DEFAULT_TYPE):

    if isinstance(context.error, Conflict):
        print(
            "UPOZORENJE: isti Telegram bot radi još negdje (drugi prozor ili "
            "drugo računalo). Ostavi pokrenutu samo jednu instancu."
        )
        return

    print("TELEGRAM GREŠKA:", repr(context.error))


def main():

    instance_lock = acquire_single_instance_lock()

    init_database()

    print(f"PERO AI VERZIJA: {BOT_VERSION}")
    print(f"BOT DATOTEKA: {os.path.abspath(__file__)}")

    try:

        print(
            spotify_service.sync_history()
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
        "Pero AI radi..."
    )

    app.add_error_handler(handle_error)

    app.run_polling()

    instance_lock.close()


if __name__ == "__main__":

    main()
