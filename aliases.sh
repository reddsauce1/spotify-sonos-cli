# Spotify CLI
# Spotify CLI
sp() {
    local base="http://localhost:5006"
    
    case "$1" in
        search)
            local query="${@:2}"
            query="${query// /+}"
            curl -s "$base/search?q=$query" | jq -r '
                "Search results for: \(.query)\n",
                (.results[] | "\(.num). \(.name)\n   Artist: \(.artist)\n   Album: \(.album)\n")
            '
            ;;
        album)
            local query="${@:2}"
            query="${query// /+}"
            curl -s "$base/search?q=$query&type=album" | jq -r '
                "Albums matching: \(.query)\n",
                (.results[] | "\(.num). \(.name)\n   Artist: \(.artist)\n   Year: \(.year)\n")
            '
            ;;
        artist)
            local query="${@:2}"
            query="${query// /+}"
            curl -s "$base/search?q=$query&type=artist" | jq -r '
                "Artists matching: \(.query)\n",
                (.results[] | "\(.num). \(.name)\n   Genres: \(.genres)\n   Followers: \(.followers)\n")
            '
            ;;
        playlist)
            local query="${@:2}"
            query="${query// /+}"
            curl -s "$base/search?q=$query&type=playlist" | jq -r '
                "Playlists matching: \(.query)\n",
                (.results[] | "\(.num). \(.name)\n   By: \(.owner)\n   Tracks: \(.tracks)\n")
            '
            ;;
        my)
            case "$2" in
                playlists)
                    local offset="${3:-0}"
                    curl -s "$base/my/playlists?offset=$offset" | jq -r '
                        "Your Playlists (showing \(.showing) of \(.total)):\n",
                        (.your_playlists[] | "\(.num). \(.name) (\(.tracks) tracks)"),
                        "",
                        if .has_more then "Next page: sp my playlists \(.showing | split("-") | .[1] | tonumber)" else "End of list" end
                    '
                    ;;
                liked)
                    local offset="${3:-0}"
                    curl -s "$base/my/liked?offset=$offset" | jq -r '
                        "Your Liked Songs (showing \(.showing) of \(.total)):\n",
                        (.your_liked_songs[] | "\(.num). \(.name)\n    Artist: \(.artist)"),
                        "",
                        if .has_more then "Next page: sp my liked \(.showing | split("-") | .[1] | tonumber)" else "End of list" end
                    '
                    ;;
                recent)
                    curl -s "$base/my/recent" | jq -r '
                        "Recently Played:\n",
                        (.recently_played[] | "\(.num). \(.name)\n    Artist: \(.artist)")
                    '
                    ;;
                top)
                    local offset="${3:-0}"
                    curl -s "$base/my/top?offset=$offset" | jq -r '
                        "Your Top Tracks (showing \(.showing) of \(.total)):\n",
                        (.your_top_tracks[] | "\(.num). \(.name)\n    Artist: \(.artist)"),
                        "",
                        if .has_more then "Next page: sp my top \(.showing | split("-") | .[1] | tonumber)" else "End of list" end
                    '
                    ;;
                *)
                    echo "Usage: sp my [playlists|liked|recent|top] [offset]"
                    ;;
            esac
            ;;
        play)
            curl -s "$base/play?num=$2" | jq -r '
                if .error then "Error: \(.error)"
                else "Now playing: \(.item.name)"
                end
            '
            ;;
        queue)
            curl -s "$base/queue?num=$2" | jq -r '
                if .error then "Error: \(.error)"
                else "Added to queue: \(.item.name)"
                end
            '
            ;;
        like)
            curl -s "$base/like" | jq -r '
                if .error then "Error: \(.error)"
                else "Liked: \(.track)"
                end
            '
            ;;
        tracks)
            curl -s "$base/tracks?num=$2" | jq -r '
                "\(.album) - \(.artist)\n",
                (.tracks[] | "\(.num). \(.name) (\(.duration))")
            '
            ;;
        help)
            echo "Spotify Commands:"
            echo ""
            echo "  Search:"
            echo "    sp search <query>     Search for tracks"
            echo "    sp album <query>      Search for albums"
            echo "    sp artist <query>     Search for artists"
            echo "    sp playlist <query>   Search for playlists"
            echo "    sp tracks <num>       Show album tracks from search"
            echo ""
            echo "  Your Library:"
            echo "    sp my playlists       Your playlists"
            echo "    sp my liked           Your liked songs"
            echo "    sp my recent          Recently played"
            echo "    sp my top             Your top tracks"
            echo ""
            echo "  Playback:"
            echo "    sp play <num>         Play result number"
            echo "    sp queue <num>        Add result to queue"
            echo "    sp like               Like current song"
            echo ""
            ;;
        *)
            echo "Unknown command. Try: sp help"
            ;;
    esac
}


sonos() {
    if [ "$1" = "help" ] || [ -z "$1" ]; then
        echo "Sonos Commands:"
        echo ""
        echo "  Playback:"
        echo "    sonos play"
        echo "    sonos pause"
        echo "    sonos playpause"
        echo "    sonos next"
        echo "    sonos previous"
        echo ""
        echo "  Volume:"
        echo "    sonos volume/30      (set to 30)"
        echo "    sonos volume/+5      (increase by 5)"
        echo "    sonos volume/-5      (decrease by 5)"
        echo "    sonos mute"
        echo "    sonos unmute"
        echo ""
        echo "  Play modes:"
        echo "    sonos shuffle/on"
        echo "    sonos shuffle/off"
        echo "    sonos repeat/all"
        echo "    sonos repeat/one"
        echo "    sonos repeat/none"
        echo ""
        echo "  Queue:"
        echo "    sonos queue"
        echo "    sonos clearqueue"
        echo ""
        echo "  Spotify:"
        echo "    sonos spotify/now/spotify:playlist:xxxxx"
        echo "    sonos spotify/now/spotify:track:xxxxx"
        echo "    sonos spotify/now/spotify:album:xxxxx"
        echo ""
        echo "  Info:"
        echo "    sonos state"
        echo "    sonos favorites"
        echo ""
        echo "  Other:"
        echo "    sonos sleep/1800     (sleep timer in seconds)"
        echo "    sonos sleep/off"
        echo "    sonos say/Hello      (text-to-speech)"
    else
        curl -s "http://localhost:5005/Dining%20Room/$1"
        echo
    fi
}

nowplaying() {
    curl -s "http://localhost:5005/Dining%20Room/state" | jq -r '.currentTrack | "Artist--> \(.artist)\nSong--> \(.title)\nAlbum--> \(.album)"'
}
