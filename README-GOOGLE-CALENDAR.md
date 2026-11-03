# Google Calendar integracija

## 1. Google Cloud

1. Otvori Google Cloud Console i odaberi ili kreiraj projekt.
2. Uključi **Google Calendar API** i **Google Tasks API**.
3. Konfiguriraj OAuth consent screen.
4. Kreiraj OAuth Client ID tipa **Desktop app**.
5. Preuzetu JSON datoteku spremi u mapu projekta pod nazivom
   `google_credentials.json`.

Ako je aplikacija u načinu **Testing**, dodaj svoj Google račun među testne
korisnike.

## 2. Python paketi

U aktivnom virtualnom okruženju pokreni:

```powershell
pip install -r requirements.txt
```

## 3. Konfiguracija

Postojećoj `.env` datoteci dodaj varijable iz `.env.example` koje počinju s
`GOOGLE_CALENDAR_`. Zadane vrijednosti već odgovaraju glavnom kalendaru i
vremenskoj zoni Hrvatske.

## 4. Prva autorizacija ili dodavanje Google Tasksa

Pokreni bota i pošalji mu:

> /googleauth

Ako Calendar već radi, a tek sada dodaješ Tasks, prvo obriši
`google_token.json`. Potom pošalji `/googleauth` i na Google stranici odobri
pristup i Calendaru i Tasksima. Dopuštenje za Tasks omogućuje i čitanje i
upis novih zadataka.

Otvorit će se Googleova stranica za prijavu i odobrenje pristupa. Nakon uspješne
prijave nastaje lokalna datoteka `google_token.json`. Nju, kao ni
`google_credentials.json`, nemoj slati drugima niti dodavati u Git.

## 5. Primjeri

- Što imam danas u kalendaru?
- Jesam li slobodan sutra od 10 do 11?
- Dodaj sastanak s Renatom u petak od 10 do 11.
- Pomakni taj sastanak na 12 sati.
- Izbriši sastanak s Renatom.
- Koje zadatke imam danas i koji su mi zadaci u kašnjenju?
- Prikaži sve moje otvorene zadatke.
- Dodaj zadatak „Poslati ponudu” za sutra.
- Pomakni rok zadatka „Poslati ponudu” na petak.
- Obriši zadatak „Poslati ponudu”.

## 6. Automatski jutarnji izvještaj

Pošalji `/jutarnji` za uključenje izvještaja u Telegramu:

- ponedjeljak–petak u 07:30
- subota i nedjelja u 09:00

Izvještaj sadrži današnje događaje, današnje nedovršene zadatke i zakašnjele
zadatke. Isključi ga naredbom `/bezjutarnjeg`.

Bot mora ostati pokrenut da bi izvještaj mogao biti poslan.

Kreiranje, izmjena i brisanje izvršavaju se tek nakon pritiska gumba
**Potvrdi** u Telegramu.
