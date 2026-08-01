#!/usr/bin/env python3
"""Run the Spotify OAuth flow and write a fresh .cache token file.

Run this on a machine with a browser. It opens Spotify's consent page,
catches the redirect on 127.0.0.1:8888, and writes the resulting tokens
to .cache next to this script.

IMPORTANT -- rotating vs. re-issuing:

Deleting .cache and re-running this issues a *new* refresh token, but it
does not invalidate the old one. A leaked refresh token stays valid until
you revoke the app's access. To genuinely rotate, first revoke at

    https://www.spotify.com/account/apps/

("DJ Assistant" or whatever you named the app -> Remove Access), then run
this script to authorise again.
"""
import json
import os
import shutil
import sys
import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'config.json')
CACHE_PATH = os.path.join(HERE, '.cache')

# Must match the scopes server.py requests, or the server will trigger its
# own re-authorisation on first use and overwrite this token.
SCOPE = (
    "user-library-read user-library-modify playlist-read-private "
    "playlist-modify-public playlist-modify-private "
    "user-read-recently-played user-top-read"
)


def main():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"No config.json at {CONFIG_PATH} -- copy config.example.json first.")

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    for key in ('client_id', 'client_secret'):
        if not config.get(key):
            sys.exit(f"config.json is missing {key}")

    # Keep the previous token until the new one is confirmed working, so a
    # failed browser flow does not leave the server with no credentials.
    backup = None
    if os.path.exists(CACHE_PATH):
        backup = f"{CACHE_PATH}.bak.{int(time.time())}"
        shutil.copy2(CACHE_PATH, backup)
        os.remove(CACHE_PATH)
        print(f"Existing token backed up to {os.path.basename(backup)} and removed.")

    auth = SpotifyOAuth(
        client_id=config['client_id'],
        client_secret=config['client_secret'],
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=SCOPE,
        cache_path=CACHE_PATH,
        open_browser=True,
    )

    try:
        sp = spotipy.Spotify(auth_manager=auth)
        me = sp.me()
    except Exception as exc:
        if backup:
            shutil.copy2(backup, CACHE_PATH)
            print(f"Auth failed -- restored previous token from {os.path.basename(backup)}")
        sys.exit(f"Authorisation failed: {exc}")

    os.chmod(CACHE_PATH, 0o600)
    print(f"\nAuthorised as {me.get('display_name') or me['id']} ({me['id']})")
    print(f"Token written to {CACHE_PATH} (mode 600)")
    if backup:
        print(f"\nOld token still on disk at {os.path.basename(backup)} -- delete it once")
        print("you have confirmed the server works:")
        print(f"    rm {backup}")
    print("\nIf you have not already revoked the previous token, do it at")
    print("    https://www.spotify.com/account/apps/")
    print("otherwise the old refresh token remains valid.")


if __name__ == '__main__':
    main()
