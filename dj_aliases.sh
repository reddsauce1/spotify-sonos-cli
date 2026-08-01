# DJ Server CLI - add to ~/.bashrc or source this file
# Usage: dj <command> [args]

DJ_SERVER="http://localhost:5006"

# The server authenticates every API endpoint. Read the shared token out of
# config.json at source time; re-source this file if you rotate it.
DJ_CONFIG="$(dirname "${BASH_SOURCE[0]}")/config.json"
DJ_TOKEN="$(jq -r '.cli_token // empty' "$DJ_CONFIG" 2>/dev/null)"

if [ -z "$DJ_TOKEN" ]; then
    echo "dj: warning: no cli_token in $DJ_CONFIG - API calls will return 401" >&2
fi

# All CLI requests go through here so the token is attached exactly once.
_dj_curl() {
    curl -s -H "X-DJ-Token: $DJ_TOKEN" "$@"
}

dj() {
    case "$1" in
        search)
            local query="${@:2}"
            query="${query// /+}"
            _dj_curl "$DJ_SERVER/search?q=$query" | jq -r '
                "🔍 Results for: \(.query)\n",
                (.results[] | "\(.num). \(.name)\n   \(.artist) • \(.album)")
            '
            ;;
        play)
            _dj_curl "$DJ_SERVER/play?num=$2" | jq -r '
                if .item then "▶️ Playing: \(.item.name) by \(.item.artist)"
                elif .error then "❌ \(.error)"
                else "✓ \(.status)" end
            '
            ;;
        queue|q)
            _dj_curl "$DJ_SERVER/queue?num=$2" | jq -r '
                if .item then "➕ Queued: \(.item.name) by \(.item.artist)"
                elif .error then "❌ \(.error)"
                else "✓ \(.status)" end
            '
            ;;
        next|n)
            _dj_curl "$DJ_SERVER/next?num=$2" | jq -r '
                if .item then "⏭️ Playing next: \(.item.name) by \(.item.artist)"
                elif .error then "❌ \(.error)"
                else "✓ \(.status)" end
            '
            ;;
        pause)
            _dj_curl "$DJ_SERVER/pause" | jq -r '"⏸️ \(.status)"'
            ;;
        resume|r)
            _dj_curl "$DJ_SERVER/resume" | jq -r '"▶️ \(.status)"'
            ;;
        skip|s)
            _dj_curl "$DJ_SERVER/skip" | jq -r '"⏭️ \(.status)"'
            ;;
        prev)
            _dj_curl "$DJ_SERVER/previous" | jq -r '"⏮️ \(.status)"'
            ;;
        vol)
            if [ -z "$2" ]; then
                _dj_curl "$DJ_SERVER/volume" | jq -r '"🔊 Volume: \(.volume)"'
            elif [ "$2" = "up" ]; then
                _dj_curl "$DJ_SERVER/volume?change=%2B10" | jq -r '"🔊 \(.status)"'
            elif [ "$2" = "down" ]; then
                _dj_curl "$DJ_SERVER/volume?change=-10" | jq -r '"🔉 \(.status)"'
            else
                _dj_curl "$DJ_SERVER/volume?level=$2" | jq -r '"🔊 \(.status)"'
            fi
            ;;
        np|nowplaying)
            _dj_curl "$DJ_SERVER/nowplaying" | jq -r '
                if .title != "Nothing playing" then "🎵 \(.title)\n   \(.artist) • \(.album)\n   Volume: \(.volume) • \(.playbackState)"
                else "🔇 Nothing playing" end
            '
            ;;
        showqueue|sq)
            # Check .error first: an error response has no .queue, and jq
            # reports null|length as 0, so a failure used to render as
            # "Queue is empty" -- which is how a permanently timing-out
            # getqueue went unnoticed.
            _dj_curl "$DJ_SERVER/getqueue" | jq -r '
                if .error then "❌ \(.error)"
                elif (.queue | length) > 0 then
                    "📋 Queue (\(.queue | length)\(if .limit and (.queue | length) >= .limit then "+" else "" end) tracks):\n",
                    (.queue[:10][] | "  • \(.title) - \(.artist)")
                else "📭 Queue is empty" end
            '
            ;;
        clear)
            _dj_curl "$DJ_SERVER/clearqueue" | jq -r '"🗑️ \(.status)"'
            ;;
        like)
            _dj_curl "$DJ_SERVER/like" | jq -r '
                if .track then "❤️ Liked: \(.track)"
                else "❌ \(.error)" end
            '
            ;;
        playlists)
            _dj_curl "$DJ_SERVER/my/playlists" | jq -r '
                "📚 Your Playlists:\n",
                (.your_playlists[] | "\(.num). \(.name) (\(.tracks) tracks)")
            '
            ;;
        liked)
            _dj_curl "$DJ_SERVER/my/liked" | jq -r '
                "❤️ Liked Songs:\n",
                (.your_liked_songs[] | "\(.num). \(.name) - \(.artist)")
            '
            ;;
        help|*)
            echo "🎵 DJ Commands:"
            echo ""
            echo "  Search & Play:"
            echo "    dj search <query>  Search for tracks"
            echo "    dj play <num>      Play result now"
            echo "    dj queue <num>     Add to end of queue (alias: dj q)"
            echo "    dj next <num>      Play after current song (alias: dj n)"
            echo ""
            echo "  Playback:"
            echo "    dj pause           Pause"
            echo "    dj resume          Resume (alias: dj r)"
            echo "    dj skip            Skip track (alias: dj s)"
            echo "    dj prev            Previous track"
            echo ""
            echo "  Volume:"
            echo "    dj vol             Show volume"
            echo "    dj vol <0-100>     Set volume"
            echo "    dj vol up/down     Adjust volume"
            echo ""
            echo "  Queue:"
            echo "    dj np              Now playing"
            echo "    dj showqueue       Show queue (alias: dj sq)"
            echo "    dj clear           Clear queue"
            echo ""
            echo "  Library:"
            echo "    dj like            Like current song"
            echo "    dj playlists       Your playlists"
            echo "    dj liked           Your liked songs"
            ;;
    esac
}