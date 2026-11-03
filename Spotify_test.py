import os

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth


load_dotenv()


SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")


scope = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
])


spotify = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=scope,
        cache_path=".spotify_cache",
    )
)


user = spotify.current_user()

current = spotify.current_playback()

if current and current.get("item"):
    track = current["item"]

    artist = track["artists"][0]["name"]
    song = track["name"]

    print(f"Trenutno svira: {artist} - {song}")
else:
    print("Trenutno ništa ne svira.")

spotify.start_playback()

print("Spotify spojen!")
print("Korisnik:", user["display_name"])