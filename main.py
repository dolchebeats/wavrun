#!/usr/bin/env python3
import os
import sys
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
from vlc_player import VLCMusic
import random
import threading
import time
import json
from pathlib import Path
import signal

# Cross-platform keyboard input
try:
    import msvcrt  # Windows
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False
    try:
        import termios
        import tty
        import select
        HAS_TERMIOS = True
    except ImportError:
        HAS_TERMIOS = False

try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except Exception:
    HAS_MUTAGEN = False

CONFIG_FILE = Path("wavrun_config.json")
SPLASH_FILE = Path("splash.txt")
player = VLCMusic()

def load_saved_position():
    """Load last played song position from config file"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_position(folder, current_index):
    """Save current song position to config file"""
    try:
        config = load_saved_position()
        config["last_folder"] = os.path.abspath(folder)
        config["last_index"] = current_index
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f)
    except Exception:
        pass


def get_metadata(file_path):
    """Extract metadata from audio file if available using mutagen. Return dict or None."""
    if not HAS_MUTAGEN:
        return None
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            return None
        metadata = {}
        # try common easy tags
        for key in ("title", "artist", "album"):
            val = audio.get(key)
            if val:
                # mutagen easy returns lists for tags; take first
                metadata[key] = val[0] if isinstance(val, (list, tuple)) else str(val)
        # duration
        if hasattr(audio, "info") and getattr(audio.info, "length", None) is not None:
            metadata["length"] = float(audio.info.length)
        return metadata if metadata else None
    except Exception:
        return None


def format_time(seconds):
    """Format seconds as MM:SS"""
    if seconds is None:
        return "??:??"
    secs = int(seconds)
    mins = secs // 60
    secs = secs % 60
    return f"{mins:02d}:{secs:02d}"


def get_key():
    """
    Non-blocking single-key read. Returns:
      - 'LEFT' or 'RIGHT' for arrow keys
      - single-character (already uppercased for letters)
      - ' ' for space
      - None when no key available
    """
    if HAS_MSVCRT:
        if msvcrt.kbhit():
            key = msvcrt.getch()
            # handle special sequences
            if key == b'\r':
                return '\n'
            if key == b'\x00' or key == b'\xe0':
                # special key; read next char
                key2 = msvcrt.getch()
                if key2 == b'K':
                    return 'LEFT'
                if key2 == b'M':
                    return 'RIGHT'
                return None
            try:
                ch = key.decode("utf-8")
            except Exception:
                return None
            return ch.upper()
    elif HAS_TERMIOS:
        # Use select to check stdin readiness
        try:
            if select.select([sys.stdin], [], [], 0)[0]:
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    ch = sys.stdin.read(1)
                    if ch == '\x1b':
                        # possible arrow sequence
                        if select.select([sys.stdin], [], [], 0.05)[0]:
                            ch2 = sys.stdin.read(1)
                            if ch2 == '[' and select.select([sys.stdin], [], [], 0.05)[0]:
                                ch3 = sys.stdin.read(1)
                                if ch3 == 'D':
                                    return 'LEFT'
                                if ch3 == 'C':
                                    return 'RIGHT'
                        return None
                    return ch.upper()
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            return None
    return None


def find_closest_match(song_files, search_term):
    """Find the closest matching filename to search term (simple heuristic)."""
    if not search_term:
        return None
    st = search_term.lower()
    best, best_score = None, 0.0
    for s in song_files:
        lowered = s.lower()
        if st in lowered:
            # score: proportion of match length to filename length
            score = len(st) / max(1, len(lowered))
            if score > best_score:
                best_score = score
                best = s
        # check words
        for token in lowered.replace("_", " ").split():
            if token.startswith(st):
                score = len(st) / max(1, len(token))
                if score > best_score:
                    best_score = score
                    best = s
    return best


def play_music(folder, song_files, current_index, shuffle_mode=False, repeat_mode="off", queue=None, filter_term=""):
    """
    Core playback loop. Returns tuple ("quit" or "stop", current_index) when exiting.
    """
    if queue is None:
        queue = []
    queue_list = list(queue)
    shuffle_state = [bool(shuffle_mode)]
    filter_state = [filter_term or ""]

    def rebuild_play_order():
        if filter_state[0]:
            filtered = [i for i, s in enumerate(song_files) if filter_state[0].lower() in s.lower()]
            if shuffle_state[0]:
                out = filtered.copy()
                random.shuffle(out)
                return out
            return filtered
        else:
            idxs = list(range(len(song_files)))
            if shuffle_state[0]:
                random.shuffle(idxs)
            return idxs

    play_order = rebuild_play_order()
    # locate start index in play_order; if not present start at 0
    if current_index in play_order:
        play_index = play_order.index(current_index)
    else:
        play_index = 0

    volume = 0.7
    player.set_volume(volume)

    autoplay = [True]
    playing = False
    stop_progress = threading.Event()
    current_pos = [0.0]
    song_ended_flag = threading.Event()
    first_load = True

    def show_progress():
        while not stop_progress.is_set():
            if playing:
                pos = player.get_pos()
                if pos is not None and pos >= 0:
                    try:
                        current_pos[0] = pos / 1000.0
                    except Exception:
                        pass
                else:
                    # get_pos() may be -1 when not available; don't treat that as end immediately
                    if not player.get_busy():
                        if not song_ended_flag.is_set():
                            song_ended_flag.set()
            time.sleep(0.4)

    while True:
        if play_index >= len(play_order):
            if repeat_mode == "all":
                play_index = 0
                if shuffle_state[0]:
                    random.shuffle(play_order)
            else:
                print("\nEnd of playlist")
                return ("stop", current_index)

        song_index = play_order[play_index]
        current_index = song_index
        filepath = song_files[song_index]
        file_path = os.path.join(folder, filepath)

        if not os.path.exists(file_path):
            print(f"File '{file_path}' missing — skipping")
            play_index += 1
            continue

        try:
            player.load(file_path)
            player.play()
            if first_load:
                player.pause()
                playing = False
                first_load = False
            elif not autoplay[0]:
                player.pause()
                playing = False
            else:
                playing = True
        except Exception as e:
            print(f"Error loading '{file_path}': {e}, skipping")
            play_index += 1
            continue

        metadata = get_metadata(file_path)

        print("\n" + "=" * 60)
        if metadata and metadata.get("title"):
            print(f"Title: {metadata.get('title')}")
            if metadata.get("artist"):
                print(f"Artist: {metadata.get('artist')}")
            if metadata.get("album"):
                print(f"Album: {metadata.get('album')}")
        else:
            print(f"Now playing: {filepath}")
        if metadata and metadata.get("length"):
            print(f"Duration: {format_time(metadata['length'])}")
        print("=" * 60)

        def display_time_position():
            pos = current_pos[0]
            if metadata and metadata.get("length"):
                total = metadata["length"]
                percent = int((pos / total) * 100) if total > 0 else 0
                bar_len = 30
                filled = int(bar_len * pos / total) if total > 0 else 0
                bar = "█" * filled + "░" * (bar_len - filled)
                print(f"[{bar}] {format_time(pos)} / {format_time(total)} ({percent}%)")
            else:
                print(f"Position: {format_time(current_pos[0])}")

        # Commands summary (consistent keys)
        print("Commands: [Space] Play/Pause, [S]top, [Q]uit, [+]/[-] Volume, [N]ext, [B]ack")
        print("          [R]epeat, [H]uffle, [A]utoplay, [/] Search, [F]ilter, [I]nfo")
        print("          [1/2/3] Seek -5/-10/-15s (info only), [7/8/9] Seek +5/+10/+15s (info only)")
        if queue_list:
            print(f"          Queue: {len(queue_list)} song(s)")
        if filter_state[0]:
            print(f"          Filter: '{filter_state[0]}'")
        print(f"          Autoplay: {'ON' if autoplay[0] else 'OFF'}")

        # start progress thread
        stop_progress.clear()
        song_ended_flag.clear()
        current_pos[0] = 0.0
        progress_thread = threading.Thread(target=show_progress, daemon=True)
        progress_thread.start()

        time.sleep(0.1)
        if not playing:
            print("\n[PAUSED] Press Space or Enter to play")
        else:
            print("\n[PLAYING]")
        display_time_position()

        last_display_time = time.time()
        display_interval = 0.5

        while True:
            # periodic redraw
            now = time.time()
            if now - last_display_time >= display_interval:
                pos = current_pos[0]
                status = "PLAYING" if playing and player.get_busy() else "PAUSED"
                if metadata and metadata.get("length"):
                    total = metadata["length"]
                    bar_len = 30
                    filled = int(bar_len * pos / total) if total > 0 else 0
                    bar = "█" * filled + "░" * (bar_len - filled)
                    print(f"\r[{bar}] {format_time(pos)} / {format_time(total)} [{status}]", end="", flush=True)
                else:
                    print(f"\rPosition: {format_time(pos)} [{status}]", end="", flush=True)
                last_display_time = now

            # check end flag
            if song_ended_flag.is_set() and playing:
                stop_progress.set()
                if repeat_mode == "one":
                    try:
                        player.load(file_path)
                        player.play()
                        current_pos[0] = 0.0
                        song_ended_flag.clear()
                        # continue playing same
                        continue
                    except Exception:
                        pass
                else:
                    # auto-advance
                    if queue_list:
                        current_index = queue_list.pop(0)
                        if current_index in play_order:
                            play_index = play_order.index(current_index)
                        else:
                            play_index += 1
                    else:
                        play_index += 1
                    break

            # non-blocking key check
            key = get_key()
            command = None
            if key is not None:
                # treat Enter as play/pause as well
                if key == '\n':
                    command = " "
                    print()  # newline after progress line
                else:
                    # convert to expected form
                    command = key.upper()
                    print()  # newline

            if command is None:
                # fall back to blocking input — user might prefer typing full commands
                # clear short animated line first
                try:
                    print("\r" + " " * 80 + "\r", end="", flush=True)
                    user_input = input("> ").strip()
                    if not user_input:
                        command = " "
                    else:
                        command = user_input.upper()
                except (EOFError, KeyboardInterrupt):
                    player.stop()
                    stop_progress.set()
                    return ("quit", current_index)

            # reset end flag so commands process cleanly
            song_ended_flag.clear()

            # command handling
            if command in ("", " ", "P"):
                if playing:
                    player.pause()
                    playing = False
                    print("Paused")
                    display_time_position()
                else:
                    player.unpause()
                    playing = True
                    print("Playing")
                    display_time_position()

            elif command in ("S", "STOP"):
                player.stop()
                playing = False
                stop_progress.set()
                print("Stopped")
                return ("stop", current_index)

            elif command in ("Q", "QUIT"):
                player.stop()
                playing = False
                stop_progress.set()
                print("Quitting...")
                return ("quit", current_index)

            elif command in ("+", "VOLUMEUP"):
                volume = min(1.0, volume + 0.1)
                player.set_volume(volume)
                print(f"Volume: {int(volume * 100)}%")

            elif command in ("-", "VOLUMEDOWN"):
                volume = max(0.0, volume - 0.1)
                player.set_volume(volume)
                print(f"Volume: {int(volume * 100)}%")

            elif command in ("N", "NEXT"):
                player.stop()
                playing = False
                stop_progress.set()
                if queue_list:
                    current_index = queue_list.pop(0)
                    if current_index in play_order:
                        play_index = play_order.index(current_index)
                    else:
                        play_index += 1
                else:
                    play_index += 1
                break

            elif command in ("B", "BACK", "PREVIOUS"):
                player.stop()
                playing = False
                stop_progress.set()
                play_index = max(0, play_index - 1)
                break

            elif command in ("R", "REPEAT"):
                if repeat_mode == "off":
                    repeat_mode = "one"
                    print("Repeat: current song")
                elif repeat_mode == "one":
                    repeat_mode = "all"
                    print("Repeat: all")
                else:
                    repeat_mode = "off"
                    print("Repeat: off")

            elif command in ("H", "SHUFFLE"):
                shuffle_state[0] = not shuffle_state[0]
                current_song_idx = play_order[play_index] if play_index < len(play_order) else None
                play_order = rebuild_play_order()
                if current_song_idx is not None and current_song_idx in play_order:
                    play_index = play_order.index(current_song_idx)
                print(f"Shuffle: {'ON' if shuffle_state[0] else 'OFF'}")

            elif command in ("A", "AUTOPLAY"):
                autoplay[0] = not autoplay[0]
                print(f"Autoplay: {'ON' if autoplay[0] else 'OFF'}")
                if autoplay[0]:
                    print("Next songs will auto-play")
                else:
                    print("Next songs will start paused")

            elif command in ("I", "INFO"):
                print(f"\nFile: {filepath}")
                if metadata:
                    for k, v in metadata.items():
                        if k != "length":
                            print(f"{k.capitalize()}: {v}")
                    if metadata.get("length"):
                        print(f"Length: {format_time(metadata['length'])}")
                print(f"Volume: {int(volume * 100)}%")
                print(f"Repeat: {repeat_mode}")
                print(f"Shuffle: {'On' if shuffle_state[0] else 'Off'}")
                print(f"Autoplay: {'On' if autoplay[0] else 'Off'}")
                if playing and player.get_busy():
                    print()
                    display_time_position()

            elif command.startswith("/"):
                if command == "/":
                    print("Enter search term: ", end="", flush=True)
                    term = input().strip()
                else:
                    term = command[1:].strip()
                if term:
                    match = find_closest_match(song_files, term)
                    if match:
                        mindex = song_files.index(match)
                        print(f"Found: {match}")
                        print("Press [P] to play now, [Q] to queue, [Enter] to cancel: ", end="", flush=True)
                        action = input().strip().upper()
                        if action == "P":
                            current_index = mindex
                            if current_index in play_order:
                                play_index = play_order.index(current_index)
                            else:
                                play_order.insert(play_index + 1, current_index)
                                play_index += 1
                            player.stop()
                            stop_progress.set()
                            break
                        elif action == "Q":
                            queue_list.append(mindex)
                            print(f"Queued: {match}")
                    else:
                        print("No match found")

            elif command in ("F", "FILTER"):
                print("Enter filter term (empty to clear): ", end="", flush=True)
                filt = input().strip()
                filter_state[0] = filt
                if filt:
                    print(f"Filter set to '{filt}'")
                    current_song_idx = play_order[play_index] if play_index < len(play_order) else None
                    play_order = rebuild_play_order()
                    if current_song_idx is not None and current_song_idx in play_order:
                        play_index = play_order.index(current_song_idx)
                    elif play_order:
                        play_index = 0
                    else:
                        print("No songs match filter!")
                else:
                    print("Filter cleared")
                    play_order = rebuild_play_order()
                    if current_index in play_order:
                        play_index = play_order.index(current_index)

            elif command in ("1", "2", "3", "7", "8", "9"):
                # Seeking is limited by pygame; show info
                seek_map = {"1": -5, "2": -10, "3": -15, "7": 5, "8": 10, "9": 15}
                secs = seek_map.get(command, 0)
                if secs < 0:
                    print(f"Seek {abs(secs)}s backward (informational: pygame has limited seeking support).")
                else:
                    print(f"Seek {secs}s forward (informational: pygame has limited seeking support).")

            else:
                print("Invalid command. Type [I]nfo for details.")

            # if playback stopped unexpectedly (end of song), handle auto-advance
            if not player.get_busy() and playing:
                if repeat_mode == "one":
                    try:
                        player.load(file_path)
                        player.play()
                        current_pos[0] = 0.0
                    except Exception:
                        pass
                else:
                    stop_progress.set()
                    if queue_list:
                        current_index = queue_list.pop(0)
                        if current_index in play_order:
                            play_index = play_order.index(current_index)
                        else:
                            play_index += 1
                    else:
                        play_index += 1
                    break



def main():
    try:
        True
    except Exception as e:
        print("Audio engine initialization failed:", e)
        return

    print(" ***  wavrun  *** ")
    saved_config = load_saved_position()
    default_folder = saved_config.get("last_folder", "music")
    if not os.path.isdir(default_folder):
        default_folder = "music"

    folder_input = input(f"Enter music folder path (or press Enter for '{default_folder}'): ").strip()
    folder = folder_input or default_folder

    if not os.path.isdir(folder):
        print(f"Folder '{folder}' does not exist")
        return

    print("Scanning for audio files...")
    song_files = []
    for root, dirs, files in os.walk(folder):
        for file in files:
            if file.lower().endswith((".flac", ".mp3", ".wav", ".ogg", ".m4a")):
                rel_path = os.path.relpath(os.path.join(root, file), folder)
                song_files.append(rel_path)
    song_files.sort()

    if not song_files:
        print("No audio files found")
        return

    print(f"Found {len(song_files)} audio file(s)")

    current_index = 0
    if saved_config.get("last_folder") == os.path.abspath(folder):
        saved_index = saved_config.get("last_index", 0)
        if 0 <= saved_index < len(song_files):
            saved_path = os.path.join(folder, song_files[saved_index])
            if os.path.exists(saved_path):
                current_index = saved_index
                print(f"Resuming from: {song_files[saved_index]}")
            else:
                print("Last played file not found, starting at first song")

    # Handle SIGINT/SIGTERM to save position
    def handle_signal(sig, frame):
        print("\nSignal received — saving position and exiting...")
        save_position(folder, current_index)
        try:
            player.stop()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    shuffle_mode = False
    queue = []
    filter_term = ""
    try:
        result = play_music(folder, song_files, current_index, shuffle_mode, "off", queue, filter_term)
    finally:
        # try to save on normal exit
        try:
            if isinstance(result, tuple):
                _, new_index = result
                save_position(folder, new_index)
            else:
                save_position(folder, current_index)
        except Exception:
            pass


if __name__ == "__main__":
    main()
