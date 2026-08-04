#!/usr/bin/env bash
#
# setup-sonos.sh -- install and wire up the node-sonos-http-api side.
#
# Usage: scripts/setup-sonos.sh [path-to-node-sonos-http-api]
#        (default: ~/Projects/node-sonos-http-api)
#
# The bridge is a separate upstream project and is deliberately not vendored
# here -- this repo owns only the two custom actions that extend it. That
# leaves four manual steps between a clean machine and a working system, and
# one of them fails *silently*: without settings.json the UI still works, it
# just quietly goes back to polling and being up to ten seconds stale. This
# script is those four steps in one command.
#
# Idempotent: safe to re-run. Re-run it after updating node-sonos-http-api,
# because lib/actions/ lives inside that clone and a fresh checkout drops the
# custom actions.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-$HOME/Projects/node-sonos-http-api}"
UPSTREAM="https://github.com/jishi/node-sonos-http-api.git"

CONFIG="$REPO/config.json"

die() { printf 'setup-sonos: %s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }

# --- What we need before touching anything ----------------------------------

command -v node >/dev/null || die "node is not installed"
command -v npm  >/dev/null || die "npm is not installed"
[ -f "$CONFIG" ] || die "no config.json at $CONFIG -- copy config.example.json first"

# python3 only for reading and rewriting JSON. Any 3.x will do; this uses
# nothing newer than the json module.
PY="$REPO/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)" || die "python3 is not installed"

CLI_TOKEN="$("$PY" -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('cli_token', ''))
except ValueError as exc:
    sys.exit('config.json is not valid JSON: %s' % exc)
" "$CONFIG")"

# The webhook authenticates with the same credential the CLI uses, so without
# it every push from Sonos would be rejected and the UI would fall back to
# polling with nothing in the log to explain why.
[ -n "$CLI_TOKEN" ] || die "cli_token is empty in config.json -- generate one with:
    python3 -c 'import secrets; print(secrets.token_hex(32))'"

SERVER_PORT="$("$PY" -c "
import json, sys
print(json.load(open(sys.argv[1])).get('server_port', 5006))
" "$CONFIG")"

# --- 1. The clone -----------------------------------------------------------

step "node-sonos-http-api at $TARGET"
if [ -d "$TARGET" ]; then
    [ -f "$TARGET/package.json" ] \
        || die "$TARGET exists but has no package.json -- not a node-sonos-http-api clone"
    printf '    already present\n'
else
    command -v git >/dev/null || die "git is not installed"
    git clone "$UPSTREAM" "$TARGET"
fi

# --- 2. Dependencies --------------------------------------------------------

step "npm install"
if [ -d "$TARGET/node_modules" ]; then
    printf '    node_modules present, skipping (delete it to force a reinstall)\n'
else
    (cd "$TARGET" && npm install)
fi

# --- 3. The custom actions --------------------------------------------------
#
# queuemove/queueremove drive the UI's drag-and-drop; relvolume asks the
# speaker to apply a relative change rather than resolving it against a cached
# value. They live on this side because macOS grants Local Network access per
# process: the launchd-run Python server cannot reach the speaker directly at
# all, while this one talks to it constantly.

step "custom actions"
mkdir -p "$TARGET/lib/actions"
for action in "$REPO"/sonos-actions/*.js; do
    cp "$action" "$TARGET/lib/actions/"
    printf '    %s\n' "$(basename "$action")"
done

# --- 4. The webhook ---------------------------------------------------------
#
# Merged rather than written wholesale: settings.json is also where port, ip
# and announceVolume would live, and clobbering someone's existing tuning to
# add three keys would be a rude way to fail.

step "webhook -> http://localhost:$SERVER_PORT/sonos_event"
SETTINGS="$TARGET/settings.json" WEBHOOK_PORT="$SERVER_PORT" TOKEN="$CLI_TOKEN" "$PY" - <<'PYEOF'
import json, os, pathlib

path = pathlib.Path(os.environ['SETTINGS'])
settings = {}
if path.exists():
    try:
        settings = json.loads(path.read_text())
    except ValueError as exc:
        raise SystemExit(f"setup-sonos: {path} exists but is not valid JSON: {exc}")

before = dict(settings)
settings.update({
    'webhook': f"http://localhost:{os.environ['WEBHOOK_PORT']}/sonos_event",
    'webhookHeaderName': 'X-DJ-Token',
    'webhookHeaderContents': os.environ['TOKEN'],
})
path.write_text(json.dumps(settings, indent=2) + "\n")
path.chmod(0o600)   # it holds the token

kept = [k for k in before if k not in ('webhook', 'webhookHeaderName', 'webhookHeaderContents')]
print("    written, chmod 600" + (f", kept {', '.join(sorted(kept))}" if kept else ""))
PYEOF

# --- Done -------------------------------------------------------------------

step "next"
cat <<EOF
    Restart the bridge so it loads the actions and the webhook:

        launchctl kickstart -k gui/\$(id -u)/com.laserbox.sonos-api

    Then check it came up with speakers discovered -- a 200 with an empty
    zone list means discovery has not landed yet:

        curl -s http://localhost:5005/zones | head -c 200

    And that the new action is registered:

        curl -s http://localhost:5005/\$(python3 -c "import json,urllib.parse;print(urllib.parse.quote(json.load(open('$CONFIG')).get('sonos_room','Dining Room')))")/relvolume/0
EOF
