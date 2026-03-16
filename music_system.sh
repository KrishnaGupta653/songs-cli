#!/bin/zsh
# ========= ULTRA ELITE MUSIC SYSTEM =========
# Save to: ~/.config/music_system.sh
# Add to ~/.zshrc: source ~/.config/music_system.sh

# --- DB & PATH ---
alias rds='mysql -h nonprod-rtdddevrds.cluster-c4oq3mi118xy.ap-south-1.rds.amazonaws.com -u app_devuser -p'
export PATH="/opt/homebrew/opt/mysql@8.0/bin:$PATH"
export PATH="/opt/homebrew/Cellar/kafka/4.1.1/libexec/bin:$PATH"

# --- CONFIG ---
SOCKET="$HOME/.mpvsocket"
PLAYLIST_DIR="$HOME/.terminal_music"
YTDLP="/opt/homebrew/bin/yt-dlp"
mkdir -p "$PLAYLIST_DIR"

# --- START DAEMON ---
music_start() {
  if [ ! -S "$SOCKET" ]; then
    mpv --no-video --idle \
        --input-ipc-server="$SOCKET" \
        --script-opts=ytdl_hook-ytdl_path="$YTDLP" \
        --audio-device=coreaudio/BuiltInSpeakerDevice \
        --volume=80 \
        --quiet --really-quiet &
    disown
    for i in $(seq 1 6); do
      sleep 0.5
      [ -S "$SOCKET" ] && break
    done
    if [ ! -S "$SOCKET" ]; then
      echo "❌ mpv failed to start"
      return 1
    fi
    echo "✅ mpv daemon started"
  fi
}

# --- SEND COMMAND TO MPV ---
music_cmd() {
  echo "$1" | socat - "$SOCKET" > /dev/null 2>&1
}

# --- SEARCH & PICK (fzf picker, returns URL) ---
music_search_pick() {
  "$YTDLP" "ytsearch20:$1" \
    --print "%(title)s | %(duration_string)s | %(webpage_url)s" \
    --no-download \
    2>/dev/null \
  | fzf --height 40% --reverse \
  | awk -F ' \| ' '{print $NF}'
}

# --- SEARCH ONLY (no play) ---
search() {
  "$YTDLP" "ytsearch20:$1" \
    --print "%(title)s | %(duration_string)s" \
    --no-download \
    2>/dev/null \
  | fzf --height 40% --reverse
}

# --- PLAY (clears queue, plays immediately) ---
music_play() {
  music_start || return 1
  music_cmd '{ "command": ["playlist-clear"] }'
  local url
  url=$(music_search_pick "$1")
  if [ -z "$url" ]; then
    echo "❌ No track selected."
    return 1
  fi
  echo "▶ Playing: $url"
  music_cmd '{ "command": ["loadfile", "'"$url"'", "append-play"] }'
}

# --- ADD to queue ---
music_add() {
  music_start || return 1
  local url
  url=$(music_search_pick "$1")
  if [ -z "$url" ]; then
    echo "❌ No track selected."
    return 1
  fi
  echo "➕ Added: $url"
  music_cmd '{ "command": ["loadfile", "'"$url"'", "append-play"] }'
}

# --- CONTROLS ---
music_pause()   { music_cmd '{ "command": ["cycle", "pause"] }'; }
music_next()    { music_cmd '{ "command": ["playlist-next"] }'; }
music_prev()    { music_cmd '{ "command": ["playlist-prev"] }'; }
music_volup()   { music_cmd '{ "command": ["add", "volume", 5] }'; }
music_voldown() { music_cmd '{ "command": ["add", "volume", -5] }'; }
music_shuffle() { music_cmd '{ "command": ["playlist-shuffle"] }'; }
music_repeat()  { music_cmd '{ "command": ["cycle", "loop-playlist"] }'; }

# --- NOW PLAYING ---
music_now() {
  echo '{ "command": ["get_property", "media-title"] }' \
  | socat - "$SOCKET" \
  | jq -r '.data // "Nothing playing"'
}

# --- SAVE PLAYLIST ---
music_save() {
  local name="$1"
  if [ -z "$name" ]; then echo "Usage: save <playlist_name>"; return 1; fi
  echo '{ "command": ["get_property", "playlist"] }' \
  | socat - "$SOCKET" \
  | jq -r '.data[].filename' > "$PLAYLIST_DIR/$name.m3u"
  echo "💾 Saved: $PLAYLIST_DIR/$name.m3u"
}

# --- LOAD PLAYLIST ---
music_load() {
  local name="$1"
  if [ -z "$name" ]; then echo "Usage: load <playlist_name>"; return 1; fi
  local file="$PLAYLIST_DIR/$name.m3u"
  if [ ! -f "$file" ]; then echo "❌ Playlist not found: $file"; return 1; fi
  music_start || return 1
  music_cmd '{ "command": ["playlist-clear"] }'
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    music_cmd '{ "command": ["loadfile", "'"$line"'", "append-play"] }'
  done < "$file"
  echo "📂 Loaded: $name"
}

# --- LIST PLAYLISTS ---
music_list_playlists() {
  echo "📋 Saved playlists:"
  ls "$PLAYLIST_DIR"/*.m3u 2>/dev/null | xargs -I{} basename {} .m3u || echo "  (none)"
}

# --- STATUS ---
music_status() {
  if [ -S "$SOCKET" ]; then
    echo "🟢 Running | Now: $(music_now)"
  else
    echo "🔴 Daemon not running — run: music_start"
  fi
}

# --- STOP ---
music_stop() {
  pkill mpv 2>/dev/null
  rm -f "$SOCKET"
  echo "⏹ Stopped"
}

# --- SWITCH TO HEADPHONES ---
music_headphones() {
  music_cmd '{ "command": ["set_property", "audio-device", "coreaudio/BuiltInHeadphoneOutputDevice"] }'
  echo "🎧 Switched to headphones"
}

# --- SWITCH TO SPEAKERS ---
music_speakers() {
  music_cmd '{ "command": ["set_property", "audio-device", "coreaudio/BuiltInSpeakerDevice"] }'
  echo "🔊 Switched to speakers"
}

# --- SHORTCUTS ---
alias mp=music_play
alias ma=music_add
alias pp=music_pause
alias mn=music_next
alias mb=music_prev
alias vu=music_volup
alias vd=music_voldown
alias ms=music_shuffle
alias rp=music_repeat
alias now=music_now
alias save=music_save
alias load=music_load
alias health=music_status
alias mstop=music_stop
alias mls=music_list_playlists
alias mhp=music_headphones
alias msp=music_speakers