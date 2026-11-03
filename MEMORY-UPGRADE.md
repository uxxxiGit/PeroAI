# Poboljšana memorija

## Što je promijenjeno

- Bot sada u OpenAI upit šalje najviše pet trajnih činjenica koje su povezane
  s aktualnom porukom, umjesto cijele memorije.
- Svaka činjenica ima izvor (`explicit` ili `automatic`), važnost, mogući datum
  isteka i datum zadnje upotrebe.
- AI može tiho spremiti samo jasne, stabilne i nesenzitivne preference ili
  činjenice. Izričito `zapamti` i dalje uvijek radi kao i prije.
- Lozinke, tokeni, PIN-ovi, financijski, zdravstveni i identifikacijski podaci
  odbijaju se iz trajne memorije.
- Posljednjih osam poruka ostaje neposredni kontekst, ali je svaka ograničena
  na 900 znakova kako dugački rezultati ne bi povećavali potrošnju tokena.
- Lokalne Spotify radnje i potvrđene Calendar/Tasks radnje sada se zapisuju u
  chat povijest.
- Prepoznavanje relevantne memorije koristi korijene hrvatskih riječi, pa isti
  podatak pronalazi za izraze poput `što vozim`, `koji auto imam`, `vozilo` i
  `potrošnja goriva`.
- Upiti za rođendane koriste poseban dohvat iz Googleova kalendara rođendana i
  glavnog kalendara. Početak dana/mjeseca prisilno se postavlja na 00:00, pa se
  današnji cjelodnevni rođendan više ne preskače.
- Jutarnji izvještaj ima zaseban odjeljak `Rođendani`.
- Rođendani se prepoznaju i po Googleovu polju `eventType="birthday"`, čak i
  kada se nalaze u običnom/sekundarnom kalendaru i naslov im ne sadrži riječ
  `rođendan`. Pregledavaju se svi čitljivi kalendari, uključujući skrivene.
- Upiti za današnje rođendane i rođendane tijekom tekućeg mjeseca obrađuju se
  izravno u Pythonu. Današnji unos dobiva oznaku `DANAS`, pa ga AI odgovor ne
  može slučajno izostaviti.

## Instalacija

1. Zaustavi bota.
2. Iz ove arhive kopiraj `Bot.py` preko postojećeg `Bot.py` u mapi projekta.
3. Ne kopiraj niti ne briši postojeće `.env`, `google_credentials.json`,
   `google_token.json` i `memory.db` datoteke.
4. Ponovno pokreni bota. Pri prvom pokretanju sam će dopuniti postojeću
   `memory.db` bazu, bez brisanja već spremljenih činjenica.

## Kontrola memorije

- `Zapamti da ...` i dalje izričito sprema podatak.
- `/memorija` prikazuje sve trajne podatke.
- `Zaboravi ...` briše točno navedeni spremljeni podatak.
