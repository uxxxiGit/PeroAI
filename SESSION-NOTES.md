# Pero AI — bilješke o radu

Sažetak sesije 3.–4. 11. 2026. (rad s Claude Code). Služi kao kontekst za
buduće izmjene.

## Što je projekt

Osobni AI agent kojim se upravlja preko Telegrama (`python-telegram-bot`,
polling + JobQueue). Mozak je OpenAI Responses API s tool-callingom
(`gpt-5-mini` za Spotify i složenije upite, `gpt-5-nano` za ostalo).

| Datoteka | Sadržaj |
|---|---|
| `Bot.py` | Telegram handleri, prompt, definicije alata, memorija, jutarnji izvještaj, jobovi |
| `spotify_service.py` | Sva Spotify logika (reprodukcija, playliste, povijest, žanrovi) |
| `calendar_service.py` | Google Calendar + OAuth (`google_token.json`) |
| `tasks_service.py` | Google Tasks |
| `rijeka_news_service.py`, `stripovi_forum_service.py` | Dnevni pregledi |
| `memory.db` | SQLite: poruke, trajna memorija, povijest slušanja, cache žanrova |

Git: https://github.com/uxxxiGit/PeroAI (privatni), grana `main`.
Tajne (`.env`, tokeni, `memory.db`, `Backup*/`, `*.zip`, `bot.lock`) su u
`.gitignore`.

## Gdje bot radi

- Bot radi na **drugom računalu** (Windows korisnik `Ribit`), iz
  `D:\Pero-ai\.venv\Scripts\python.exe -u Bot.py`, u pozadini (session 0),
  vjerojatno preko skripte ili zakazanog zadatka. Na računalu `VLADO-WIN11`
  je samo kopija za razvoj; tamošnji `.venv` ne radi (pokazuje na
  `C:\Users\Ribit\...`).
- Za testiranje na `VLADO-WIN11`:
  `PYTHONPATH=.venv/Lib/site-packages python -s ...` (sistemski Python 3.13).
- 4. 11. stigla su 3 jutarnja izvještaja jer su radile 3 instance odjednom.
  Svaka instanca ima vlastiti JobQueue. Dodan je zaključavajući `bot.lock`
  (samo jedna instanca po računalu) i upozorenje na Telegram `Conflict`.

## Napravljene izmjene

1. **Povijest slušanja** — Spotify vraća samo zadnjih 50 pjesama, a bot ih je
   povlačio samo kad mu se piše; u bazi su dani s točno 50 zapisa (izgubljeni
   podaci). Sada job svakih 20 min (`HISTORY_BACKGROUND_SYNC_MINUTES`).
   Datumi se računaju u Europe/Zagreb (prije UTC). Novi alat
   `spotify_history_list` ("što sam slušao jučer", opcionalno playlista).
2. **Sigurnost** — `TELEGRAM_ALLOWED_USER_ID` se sada stvarno provjerava
   (`TypeHandler` u grupi -1); bez njega se bot ne pokreće.
3. **Potvrde** — više kalendarskih/Tasks radnji u jednom koraku potvrđuje se
   redom (`pending_calendar_actions`). Teški pozivi idu kroz
   `asyncio.to_thread`.
4. **Spotify prepisan** u `spotify_service.py`. Uzroci starih problema:
   - nije postojao alat "playlista od izvođača X" ni "od popisa pjesama";
     model je koristio discovery (koji namjerno izbacuje X) ili Liked Songs;
   - nakon prvog alata slala se samo kratka uputa umjesto system prompta
     (Responses API ne prenosi `instructions` kroz `previous_response_id`),
     pa je model zaboravljao pravila i stalno tražio potvrde;
   - Spotify Development Mode (stanje 11/2026): search max 10 po stranici,
     `artist top-tracks` i batch `/artists` → 403, izvođač nema `genres`
     ni `popularity`. Svi žanrovi u cacheu bili su prazni, pa su žanrovske
     playliste bile nasumične.
   Novi alati: `spotify_playlist_from_artists`, `_from_tracks`,
   `_add_to_playlist`, `_similar_artists`, `_playlist_from_liked`, `_top`,
   `_play`, `_add_to_queue` (više pjesama), `_history_list`, `_history_stats`.
   Pjesme izvođača: pretraga + albumi, filtrirano po artist_id, bez
   duplikata i live/remix verzija. Žanrove procjenjuje model (`GENRE_MODEL`)
   i sprema u `artist_cache`; domaći = oznaka `ex-yu`.
   Brze naredbe (pauza, dalje, glasnoća 30...) reagiraju samo kad su cijela
   poruka.
5. **Jutarnji izvještaj** — prikazuje i "Uskoro (7 dana)" i "Zadaci bez
   roka" (prije su se zadaci bez roka dohvaćali, ali ne ispisivali). Na dnu
   je verzija bota.

## Provjereno / nije provjereno

- Provjereno uživo (bez stvaranja playlista): pronalaženje izvođača s
  tipfelerom, 20 pjesama Fontaines D.C., više izvođača, popis od 10 pjesama,
  povijest i statistika, jutarnji izvještaj s pravim zadacima, `bot.lock`.
- **Nije provjereno:** playlista iz Liked Songs po žanru (ispravljen
  OpenAI JSON poziv, test prekinut) i cijeli tok preko Telegrama.

## Otvoreno

- Verzija `2026.11.04-single-v12` je na GitHubu (commit `24d2ec5`), ali je
  drugo računalo na kojem bot radi treba preuzeti (`git pull` ili kopija)
  i ponovno pokrenuti.
- Utvrditi što na drugom računalu pokreće bota (roditeljski proces 13864,
  zakazani zadatak?) da se izbjegne dvostruko pokretanje.
- U trajnoj memoriji ostale su bilješke o playlisti "laganitza"
  (Hotel Costes) — mogu se obrisati porukom "Zaboravi laganitza".
- Na `VLADO-WIN11` mape `Backup/`, `Backup2/` i `.zip` arhive sadrže kopije
  tajni; preporučeno ih je maknuti iz projekta.
