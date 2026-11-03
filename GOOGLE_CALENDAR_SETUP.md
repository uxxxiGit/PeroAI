# Google Calendar – kratke upute

1. U Google Cloud Consoleu napravi projekt i uključi **Google Calendar API**.
2. Na OAuth consent screenu dodaj svoj Google račun kao testnog korisnika.
3. Izradi OAuth Client ID vrste **Desktop app** i preuzmi JSON.
4. Preimenuj preuzetu datoteku u `credentials.json` i stavi je u mapu gdje je `Bot.py`.
5. Instaliraj pakete iz `requirements.txt`.
6. Pokreni bota i pošalji mu `/calendar_auth`. Prijava se obavlja u pregledniku, a token se lokalno sprema u `token.json`.

## Primjeri

- `Što imam u kalendaru sutra?`
- `Provjeri jesam li slobodan u petak od 10 do 11.`
- `Dodaj sastanak s Renatom u petak u 10 sati, do 11.`

Kod kreiranja, izmjene i brisanja bot uvijek prvo traži odgovor `DA` ili `NE`.

## Konfiguracija `.env`

Opcionalno:

```text
GOOGLE_CALENDAR_ID=primary
GOOGLE_CALENDAR_TIMEZONE=Europe/Zagreb
GOOGLE_CREDENTIALS_FILE=credentials.json
GOOGLE_TOKEN_FILE=token.json
```

`credentials.json` i `token.json` sadrže osjetljive autorizacijske podatke i ne smiju se slati drugima niti stavljati u javni repozitorij.
