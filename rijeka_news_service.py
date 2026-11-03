import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


TRACKING_PARAMETERS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
}
USER_AGENT = "PeroAI/2026.09 daily HNK Rijeka digest"
DIRECT_FETCH_TIMEOUT = 15
MAX_DIRECT_ITEMS = 35


def normalize_url(url):
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
    except Exception:
        return url.strip()


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def extract_sources(response):
    sources = []
    by_url = {}

    def add_source(url, title=""):
        normalized = normalize_url(url)
        if not normalized:
            return
        if normalized in by_url:
            if title and by_url[normalized]["title"] == "Izvor":
                by_url[normalized]["title"] = title
            return
        source = {"url": normalized, "title": title or "Izvor"}
        by_url[normalized] = source
        sources.append(source)

    for item in getattr(response, "output", []) or []:
        if _field(item, "type") == "web_search_call":
            action = _field(item, "action", {}) or {}
            for source in _field(action, "sources", []) or []:
                add_source(_field(source, "url", ""), _field(source, "title", ""))

        if _field(item, "type") == "message":
            for content in _field(item, "content", []) or []:
                for annotation in _field(content, "annotations", []) or []:
                    if _field(annotation, "type") == "url_citation":
                        add_source(
                            _field(annotation, "url", ""),
                            _field(annotation, "title", ""),
                        )

    return sources


def count_web_search_calls(response):
    return sum(
        1 for item in getattr(response, "output", []) or []
        if _field(item, "type") == "web_search_call"
    )


def _clean_text(value, limit=900):
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _parse_date(value):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return None


def _download(url, accept):
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )
    with urlopen(request, timeout=DIRECT_FETCH_TIMEOUT) as response:
        return response.read(2_500_000)


def _rss_items(url, kind, period_start, period_end):
    try:
        root = ElementTree.fromstring(
            _download(url, "application/rss+xml, application/xml;q=0.9, */*;q=0.5")
        )
    except Exception:
        return []

    results = []
    for node in root.findall(".//item"):
        published = _parse_date(node.findtext("pubDate"))
        if not published or not (period_start <= published <= period_end):
            continue
        title = _clean_text(node.findtext("title"), 300)
        link = normalize_url(node.findtext("link") or "")
        description = _clean_text(node.findtext("description"), 900)
        source_node = node.find("source")
        source = _clean_text(source_node.text if source_node is not None else "", 120)
        if title and link:
            results.append({
                "kind": kind,
                "title": title,
                "url": link,
                "source": source,
                "published": published.isoformat(),
                "summary": description,
            })
    return results


def _reddit_items(period_start, period_end):
    query = quote_plus('"HNK Rijeka" OR "NK Rijeka" OR Rujevica')
    url = f"https://www.reddit.com/search.json?q={query}&sort=new&t=day&limit=25"
    try:
        payload = json.loads(_download(url, "application/json").decode("utf-8"))
    except Exception:
        return []

    results = []
    for child in payload.get("data", {}).get("children", []):
        data = child.get("data", {})
        published = datetime.fromtimestamp(data.get("created_utc", 0), timezone.utc)
        if not (period_start <= published <= period_end):
            continue
        title = _clean_text(data.get("title"), 300)
        body = _clean_text(data.get("selftext"), 900)
        permalink = data.get("permalink", "")
        url = normalize_url("https://www.reddit.com" + permalink) if permalink else ""
        if title and url:
            results.append({
                "kind": "Reddit",
                "title": title,
                "url": url,
                "source": "r/" + str(data.get("subreddit", "")),
                "published": published.isoformat(),
                "summary": (
                    f"{body} Bodovi: {data.get('score', 0)}; "
                    f"komentari: {data.get('num_comments', 0)}."
                ).strip(),
            })
    return results


def fetch_direct_items(local_now=None):
    local_now = local_now or datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    period_end = local_now.astimezone(timezone.utc)
    period_start = period_end - timedelta(hours=24)

    google_queries = [
        '"HNK Rijeka" when:1d',
        '("NK Rijeka" OR Rujevica OR "Armada Rijeka") when:1d',
    ]
    jobs = []
    for query in google_queries:
        rss_url = (
            "https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=hr&gl=HR&ceid=HR:hr"
        )
        jobs.append((_rss_items, (rss_url, "mediji", period_start, period_end)))
    jobs.append((
        _rss_items,
        ("https://nk-rijeka.hr/feed/", "službeno", period_start, period_end),
    ))
    jobs.append((_reddit_items, (period_start, period_end)))

    items = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = [executor.submit(function, *args) for function, args in jobs]
        for future in as_completed(futures):
            try:
                items.extend(future.result())
            except Exception:
                continue

    unique = []
    known_urls = set()
    known_titles = set()
    for item in sorted(items, key=lambda value: value["published"], reverse=True):
        title_key = re.sub(r"\W+", " ", item["title"].lower()).strip()
        if item["url"] in known_urls or title_key in known_titles:
            continue
        known_urls.add(item["url"])
        known_titles.add(title_key)
        unique.append(item)
    return unique[:MAX_DIRECT_ITEMS]


def _search_social_sources(client, search_model, local_now):
    period_start = local_now - timedelta(hours=24)
    prompt = f"""
Izvrši TOČNO JEDNU objedinjenu web-pretragu za javno indeksirane objave i
rasprave o HNK Rijeci / NK Rijeci / Rujevici / Armadi na X-u (Twitteru),
navijačkim i nogometnim forumima u razdoblju od {period_start.isoformat()} do
{local_now.isoformat()}. Ne traži obične novinske članke jer su već dohvaćeni
izravno. Vrati kratak dokazni sažetak na hrvatskom: gdje se raspravljalo, o
čemu, raspoloženje i različita mišljenja. Jasno reci ako u tom razdoblju nema
pouzdano datiranih javno indeksiranih objava. Ne izmišljaj sadržaj ni datume.
""".strip()

    try:
        response = client.responses.create(
            model=search_model,
            tools=[{
                "type": "web_search",
                "external_web_access": True,
                "search_context_size": "low",
            }],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            input=prompt,
            max_output_tokens=1_800,
            store=False,
        )
        return {
            "text": (response.output_text or "").strip(),
            "sources": extract_sources(response),
            "web_search_calls": count_web_search_calls(response),
            "error": "",
        }
    except Exception as error:
        return {
            "text": "Društvena web-pretraga trenutno nije dostupna.",
            "sources": [],
            "web_search_calls": 0,
            "error": str(error),
        }


def _serialize_items(items, seen_urls):
    seen = {normalize_url(url) for url in seen_urls or []}
    blocks = []
    for index, item in enumerate(items, 1):
        already_seen = "DA" if normalize_url(item["url"]) in seen else "NE"
        blocks.append(
            f"[{index}] Vrsta: {item['kind']}\n"
            f"Naslov: {item['title']}\n"
            f"Izvor: {item['source'] or 'nepoznat'}\n"
            f"Objavljeno: {item['published']}\n"
            f"Već poslano: {already_seen}\n"
            f"Sažetak: {item['summary'] or 'nema opisa'}\n"
            f"URL: {item['url']}"
        )
    return "\n\n".join(blocks) or "Nema izravno dohvaćenih stavki u zadanom razdoblju."


def _fallback_digest(items, social_text):
    if not items:
        return (
            "HNK Rijeka — posljednja 24 sata\n\n"
            "U posljednja 24 sata nisam pronašao dovoljno pouzdanih i novih "
            "informacija za sadržajan izvještaj. Starije vijesti nisam "
            "uvrstio kao nove samo kako bih popunio pregled.\n\n"
            "Što govore navijači\n\n" + social_text
        )
    headlines = "; ".join(item["title"] for item in items[:8])
    return (
        "HNK Rijeka — posljednja 24 sata\n\n"
        "Dohvaćene su sljedeće nove teme: " + headlines + ".\n\n"
        "Što govore navijači\n\n" + social_text + "\n\n"
        "Što pratiti danas\n\nPratiti nove službene potvrde kluba."
    )


def generate_daily_digest(client, model, seen_urls, local_now=None,
                          search_model="gpt-4.1-mini"):
    local_now = local_now or datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    period_start = local_now - timedelta(hours=24)

    direct_items = fetch_direct_items(local_now)
    social = _search_social_sources(client, search_model, local_now)
    source_material = _serialize_items(direct_items, seen_urls)

    synthesis_prompt = f"""
Napiši gotov dnevni izvještaj na hrvatskom o HNK Rijeci za TOČNO razdoblje od
{period_start.isoformat()} do {local_now.isoformat()}.

Ispod su već dohvaćeni izvori. Ne koristi web-pretragu i ne dodaj informacije
kojih nema u materijalu. Stavke označene "Već poslano: DA" preskoči, osim ako
iz opisa jasno proizlazi nova informacija. Razlikuj službene objave, medijske
navode i komentare navijača. Ne predstavljaj glasine ni raspoloženje malog
broja komentatora kao činjenice.

IZRAVNO DOHVAĆENE VIJESTI I REDDIT (bez OpenAI web-search troška):
{source_material}

JEDNA OBJEDINJENA WEB-PRETRAGA X-a I FORUMA:
{social['text']}

Oblikuj povezan, čitljiv tekst poput kratkog novinskog pregleda, ne popis
rezultata i ne zbirku poveznica. Koristi naslov "HNK Rijeka — posljednja 24
sata", zatim odjeljke "Što se događalo", "Što govore navijači" i "Što pratiti
danas". Uključi samo stvarne teme za koje materijal daje dokaz. Ako je dan bio
mirniji, reci to prirodno i napiši kraći tekst. Ne dodaj bibliografiju, popis
izvora ni gole URL-ove; izvor možeš kratko imenovati u rečenici.
""".strip()

    try:
        response = client.responses.create(
            model=model,
            input=synthesis_prompt,
            max_output_tokens=4_500,
            store=False,
        )
        text = (response.output_text or "").strip()
    except Exception:
        text = ""
    if not text:
        text = _fallback_digest(direct_items, social["text"])

    sources = [
        {"url": item["url"], "title": item["title"]}
        for item in direct_items
    ]
    known = {source["url"] for source in sources}
    for source in social["sources"]:
        if source["url"] not in known:
            sources.append(source)
            known.add(source["url"])

    return {
        "text": text,
        "sources": sources,
        "source_urls": [source["url"] for source in sources],
        "direct_items": len(direct_items),
        "web_search_calls": social["web_search_calls"],
        "social_search_error": social["error"],
    }


def split_telegram_text(text, limit=3800):
    text = (text or "").strip()
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > limit:
            split_at = paragraph.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(paragraph[:split_at].strip())
            paragraph = paragraph[split_at:].strip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks
