#!/usr/bin/env zsh
# ============================================================
#  m — terminal music CLI
#  Install: sudo cp m /usr/local/bin/m && sudo chmod +x /usr/local/bin/m
# ============================================================

SOCKET="$HOME/.mpvsocket"
PLAYLIST_DIR="$HOME/.terminal_music"
YTDLP="/opt/homebrew/bin/yt-dlp"
MPV="/opt/homebrew/bin/mpv"

R=$'\e[0;31m'; G=$'\e[0;32m'; Y=$'\e[0;33m'
C=$'\e[0;36m'; W=$'\e[1;37m'; X=$'\e[0m'

_err()  { echo "${R}✖ $*${X}" >&2; exit 1 }
_ok()   { echo "${G}✔ $*${X}" }
_info() { echo "${C}→ $*${X}" }
_warn() { echo "${Y}⚠ $*${X}" }

mkdir -p "$PLAYLIST_DIR"

_start() {
  [ -S "$SOCKET" ] && return 0
  _info "starting daemon..."
  "$MPV" --no-video --idle \
    --input-ipc-server="$SOCKET" \
    --script-opts=ytdl_hook-ytdl_path="$YTDLP" \
    --audio-device=coreaudio/BuiltInSpeakerDevice \
    --volume=80 --quiet --really-quiet &
  disown
  for i in $(seq 1 10); do
    sleep 0.3
    [ -S "$SOCKET" ] && return 0
  done
  _err "mpv failed to start"
}

_cmd() {
  echo "$1" | socat - "$SOCKET" 2>/dev/null
}

_silent() {
  echo "$1" | socat - "$SOCKET" > /dev/null 2>&1
}

_need() {
  [ -S "$SOCKET" ] || _err "daemon not running — run: m start"
}

_get() {
  _cmd "{\"command\":[\"get_property\",\"$1\"]}" | jq -r '.data // empty' 2>/dev/null
}

_pick() {
  "$YTDLP" "ytsearch20:$1" \
    --print "%(title)s | %(duration_string)s | %(webpage_url)s" \
    --no-download 2>/dev/null \
  | fzf --height 50% --reverse --prompt "🎵 " --header "ENTER select · ESC cancel" \
  | awk -F ' \| ' '{print $NF}'
}

# ── subcommands ──────────────────────────────────────────────

do_play() {
  _start
  _silent '{"command":["playlist-clear"]}'
  local url; url=$(_pick "$1")
  [ -z "$url" ] && { _warn "cancelled"; return }
  _info "loading..."
  _silent "{\"command\":[\"loadfile\",\"$url\",\"append-play\"]}"
  sleep 1
  _ok "▶ $(_get media-title)"
}

do_add() {
  _start
  local url; url=$(_pick "$1")
  [ -z "$url" ] && { _warn "cancelled"; return }
  _silent "{\"command\":[\"loadfile\",\"$url\",\"append-play\"]}"
  _ok "➕ added to queue"
}

do_pause() {
  _need
  _silent '{"command":["cycle","pause"]}'
  local p; p=$(_get pause)
  [ "$p" = "true" ] && _info "⏸  paused" || _info "▶  resumed"
}

do_next() {
  _need
  _silent '{"command":["playlist-next"]}'
  sleep 0.8
  _ok "⏭  $(_get media-title)"
}

do_prev() {
  _need
  _silent '{"command":["playlist-prev"]}'
  sleep 0.8
  _ok "⏮  $(_get media-title)"
}

do_stop() {
  pkill mpv 2>/dev/null; rm -f "$SOCKET"
  _ok "stopped"
}

do_start() { _start }

do_vol() {
  _need
  case "$1" in
    +)   _silent '{"command":["add","volume",5]}' ;;
    -)   _silent '{"command":["add","volume",-5]}' ;;
    ''|*[!0-9]*) _err "usage: m vol [0-100|+|-]" ;;
    *)   _silent "{\"command\":[\"set_property\",\"volume\",$1]}" ;;
  esac
  _info "volume: $(_get volume | awk '{printf "%.0f%%", $1}')"
}

do_hp() {
  _need
  _silent '{"command":["set_property","audio-device","coreaudio/BuiltInHeadphoneOutputDevice"]}'
  _ok "🎧 headphones"
}

do_sp() {
  _need
  _silent '{"command":["set_property","audio-device","coreaudio/BuiltInSpeakerDevice"]}'
  _ok "🔊 speakers"
}

do_shuffle() { _need; _silent '{"command":["playlist-shuffle"]}'; _ok "shuffled" }
do_repeat()  { _need; _silent '{"command":["cycle","loop-playlist"]}'; _info "repeat toggled" }
do_clear()   { _need; _silent '{"command":["playlist-clear"]}'; _ok "queue cleared" }

do_now() {
  _need
  local title pos dur
  title=$(_get media-title)
  [ -z "$title" ] && { _warn "nothing playing"; return }
  pos=$(_get time-pos | awk '{printf "%d:%02d",$1/60,$1%60}' 2>/dev/null)
  dur=$(_get duration   | awk '{printf "%d:%02d",$1/60,$1%60}' 2>/dev/null)
  echo "${Y}♫  ${title}${X}  ${W}${pos}/${dur}${X}"
}

do_queue() {
  _need
  local pl; pl=$(_cmd '{"command":["get_property","playlist"]}')
  local n; n=$(echo "$pl" | jq '.data|length' 2>/dev/null)
  [ -z "$n" ] || [ "$n" = "0" ] && { _warn "queue empty"; return }
  echo "${C}queue — ${n} tracks:${X}"
  echo "$pl" | jq -r '.data[]|"\(.id+1). \(.title // .filename)"' 2>/dev/null \
    | sed 's/^/  /'
}

do_status() {
  if [ ! -S "$SOCKET" ]; then
    echo "${R}● stopped${X}  —  m start"
    return
  fi
  local title paused vol
  title=$(_get media-title)
  paused=$(_get pause)
  vol=$(_get volume | awk '{printf "%.0f",$1}')
  echo "${G}● running${X}  vol:${vol}%"
  if [ -n "$title" ]; then
    [ "$paused" = "true" ] \
      && echo "  ${Y}⏸  ${title}${X}" \
      || echo "  ${G}▶  ${title}${X}"
  else
    echo "  ${W}idle${X}"
  fi
}

do_save() {
  _need
  [ -z "$1" ] && _err "usage: m save <name>"
  _cmd '{"command":["get_property","playlist"]}' \
    | jq -r '.data[].filename' > "$PLAYLIST_DIR/$1.m3u"
  _ok "saved: $1"
}

do_load() {
  [ -z "$1" ] && _err "usage: m load <name>"
  local f="$PLAYLIST_DIR/$1.m3u"
  [ -f "$f" ] || _err "not found: $1"
  _start
  _silent '{"command":["playlist-clear"]}'
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    _silent "{\"command\":[\"loadfile\",\"$line\",\"append-play\"]}"
  done < "$f"
  _ok "loaded: $1"
}

do_playlists() {
  echo "${C}playlists:${X}"
  ls "$PLAYLIST_DIR"/*.m3u 2>/dev/null | xargs -I{} basename {} .m3u | sed 's/^/  /' || echo "  (none)"
}

do_devices() {
  "$MPV" --audio-device=help 2>&1 | grep "'"
}

do_help() {
  cat <<EOF
${C}m — music CLI${X}

  ${W}m "query"${X}          search & play
  ${W}m "query" -a${X}       add to queue
  ${W}m "query" -hp${X}      play via headphones
  ${W}m "query" -sp${X}      play via speakers
  ${W}m "query" -a -hp${X}   add + headphones

  ${W}pause${X}  next  prev  stop  start  shuffle  repeat  clear
  ${W}now${X}    queue  status  devices
  ${W}hp${X}     sp
  ${W}vol${X} [0-100 | + | -]
  ${W}save${X} <name>   load <name>   playlists
EOF
}

# ── main ─────────────────────────────────────────────────────

# No args → status
[ $# -eq 0 ] && { do_status; exit 0 }

# Known subcommands (first arg, exact match)
case "$1" in
  pause|pp)            do_pause;            exit 0 ;;
  next|mn)             do_next;             exit 0 ;;
  prev|mb)             do_prev;             exit 0 ;;
  stop)                do_stop;             exit 0 ;;
  start)               do_start;            exit 0 ;;
  shuffle)             do_shuffle;          exit 0 ;;
  repeat|rp)           do_repeat;           exit 0 ;;
  clear)               do_clear;            exit 0 ;;
  now)                 do_now;              exit 0 ;;
  queue)               do_queue;            exit 0 ;;
  status)              do_status;           exit 0 ;;
  hp|headphones)       do_hp;               exit 0 ;;
  sp|speakers)         do_sp;               exit 0 ;;
  devices)             do_devices;          exit 0 ;;
  playlists|pls)       do_playlists;        exit 0 ;;
  save)                do_save "$2";        exit 0 ;;
  load)                do_load "$2";        exit 0 ;;
  vol|volume)          do_vol "$2";         exit 0 ;;
  help|-h|--help)      do_help;             exit 0 ;;
esac

# Otherwise: treat all non-flag args as query, parse flags
QUERY=""
FLAG_ADD=0
FLAG_HP=0
FLAG_SP=0

for arg in "$@"; do
  case "$arg" in
    -a|--add)          FLAG_ADD=1 ;;
    -hp|--headphones)  FLAG_HP=1 ;;
    -sp|--speakers)    FLAG_SP=1 ;;
    -*)                _err "unknown flag: $arg (run: m help)" ;;
    *)                 QUERY="$QUERY $arg" ;;
  esac
done

QUERY="${QUERY## }"
[ -z "$QUERY" ] && _err "no query — usage: m \"song name\""

if [ $FLAG_ADD -eq 1 ]; then
  do_add "$QUERY"
else
  do_play "$QUERY"
fi

[ $FLAG_HP -eq 1 ] && sleep 0.5 && do_hp
[ $FLAG_SP -eq 1 ] && sleep 0.5 && do_sp