import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
import pygame
import random
import threading
import time
import json
from pathlib import Path
try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

def get_metadata(file_path):
    """Extract metadata from audio file if available"""
    if not HAS_MUTAGEN:
        return None
    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return None
        metadata = {}
        if 'TIT2' in audio or 'TITLE' in audio:
            metadata['title'] = str(audio.get('TIT2', audio.get('TITLE', [''])[0]))
        if 'TPE1' in audio or 'ARTIST' in audio:
            metadata['artist'] = str(audio.get('TPE1', audio.get('ARTIST', [''])[0]))
        if 'TALB' in audio or 'ALBUM' in audio:
            metadata['album'] = str(audio.get('TALB', audio.get('ALBUM', [''])[0]))
        if hasattr(audio, 'info') and hasattr(audio.info, 'length'):
            metadata['length'] = audio.info.length
        return metadata if metadata else None
    except:
        return None

def format_time(seconds):
    """Format seconds as MM:SS"""
    if seconds is None:
        return "??:??"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"

def play_music(folder, song_files, current_index, shuffle_mode=False, repeat_mode="off", queue=None):
    """Enhanced music player with all features"""
    if queue is None:
        queue = []
    # Make queue mutable by using a list reference
    queue_list = queue if isinstance(queue, list) else list(queue) if queue else []
    
    # Make shuffle_mode mutable so it can be changed during playback
    if isinstance(shuffle_mode, list):
        shuffle_state = shuffle_mode
    else:
        shuffle_state = [shuffle_mode]
    
    # Create a working list (shuffled if needed)
    if shuffle_state[0]:
        play_order = list(range(len(song_files)))
        random.shuffle(play_order)
    else:
        play_order = list(range(len(song_files)))
    
    # Start from current_index in play_order
    if current_index in play_order:
        play_index = play_order.index(current_index)
    else:
        play_index = 0
    
    volume = 0.7  # Default volume (70%)
    pygame.mixer.music.set_volume(volume)
    
    playing = False
    progress_thread = None
    stop_progress = threading.Event()
    current_pos = [0.0]  # Use list for mutable reference in thread
    
    song_ended_flag = threading.Event()
    
    def show_progress():
        """Thread to update current position and detect song end"""
        while not stop_progress.is_set():
            if playing:
                if pygame.mixer.music.get_busy():
                    try:
                        pos = pygame.mixer.music.get_pos() / 1000.0  # Convert to seconds
                        if pos >= 0:  # pygame returns -1 when not available
                            current_pos[0] = pos
                    except:
                        pass
                else:
                    # Song ended while playing
                    if not song_ended_flag.is_set():
                        song_ended_flag.set()
            time.sleep(0.5)
    
    while True:
        # Get current song
        if play_index >= len(play_order):
            if repeat_mode == "all":
                play_index = 0
                if shuffle_state[0]:
                    random.shuffle(play_order)
            else:
                print("\nEnd of playlist")
                return ("stop", current_index)
        
        song_index = play_order[play_index]
        current_index = song_index  # Update current_index
        filepath = song_files[song_index]
        file_path = os.path.join(folder, filepath)
        
        if not os.path.exists(file_path):
            print(f"File '{file_path}' does not exist, skipping...")
            play_index += 1
            continue
        
        # Try to load and play
        try:
            pygame.mixer.music.load(file_path)
            pygame.mixer.music.play()
            playing = True
        except Exception as e:
            print(f"Error loading file: {e}, skipping...")
            play_index += 1
            continue
        
        # Get metadata
        metadata = get_metadata(file_path)
        
        # Display song info
        print(f"\n{'='*60}")
        if metadata and 'title' in metadata:
            print(f"Title: {metadata['title']}")
            if 'artist' in metadata:
                print(f"Artist: {metadata['artist']}")
            if 'album' in metadata:
                print(f"Album: {metadata['album']}")
        else:
            print(f"Now playing: {filepath}")
        if metadata and 'length' in metadata:
            print(f"Duration: {format_time(metadata['length'])}")
        print(f"{'='*60}")
        
        # Define display function with access to current metadata
        def display_time_position():
            """Display current playback position with progress bar"""
            pos = current_pos[0]
            if metadata and 'length' in metadata:
                total = metadata['length']
                percent = int((pos / total * 100)) if total > 0 else 0
                # Create a progress bar
                bar_length = 30
                filled = int(bar_length * pos / total) if total > 0 else 0
                bar = "█" * filled + "░" * (bar_length - filled)
                print(f"[{bar}] {format_time(pos)} / {format_time(total)} ({percent}%)")
            else:
                print(f"Position: {format_time(pos)}")
        
        # Show commands
        print("Commands: [Space] Play/Pause, [S]top, [Q]uit, [+] Volume Up, [-] Volume Down")
        print("         [N]ext, [B]ack, [R]epeat, [T]ime, [I]nfo, [H]uffle")
        if queue_list:
            print(f"         Queue: {len(queue_list)} song(s) waiting")
        
        # Start progress thread to track position
        stop_progress.clear()
        song_ended_flag.clear()
        current_pos[0] = 0.0
        progress_thread = threading.Thread(target=show_progress, daemon=True)
        progress_thread.start()
        
        # Show initial position
        time.sleep(0.1)  # Brief delay to get initial position
        if playing:
            display_time_position()
        
        # Command loop
        while pygame.mixer.music.get_busy() or not playing:
            # Check if song ended (non-blocking check before input)
            if song_ended_flag.is_set() and playing:
                # Song ended naturally, handle auto-advance
                stop_progress.set()
                if repeat_mode == "one":
                    # Restart current song
                    pygame.mixer.music.load(file_path)
                    pygame.mixer.music.play()
                    current_pos[0] = 0.0
                    song_ended_flag.clear()
                    continue
                else:
                    # Auto-advance to next song
                    # Check queue first
                    if queue_list:
                        current_index = queue_list.pop(0)
                        # Find this index in play_order
                        if current_index in play_order:
                            play_index = play_order.index(current_index)
                        else:
                            play_index += 1
                    else:
                        play_index += 1
                    break  # Break to load next song
            
            try:
                command = input("\n> ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                pygame.mixer.music.stop()
                stop_progress.set()
                return ("quit", current_index)
            
            # Clear the flag if it was set (we're processing a command now)
            song_ended_flag.clear()
            
            if command == "" or command == " " or command == "P":
                # Play/Pause (spacebar or P)
                if playing:
                    pygame.mixer.music.pause()
                    playing = False
                    print("Paused")
                    display_time_position()
                else:
                    pygame.mixer.music.unpause()
                    playing = True
                    print("Playing")
                    display_time_position()
            elif command == "S" or command == "STOP":
                pygame.mixer.music.stop()
                playing = False
                stop_progress.set()
                print("Stopped")
                return ("stop", current_index)
            elif command == "Q" or command == "QUIT":
                pygame.mixer.music.stop()
                playing = False
                stop_progress.set()
                print("Quitting...")
                return ("quit", current_index)
            elif command == "+" or command == "VOLUMEUP":
                volume = min(1.0, volume + 0.1)
                pygame.mixer.music.set_volume(volume)
                print(f"Volume: {int(volume * 100)}%")
            elif command == "-" or command == "VOLUMEDOWN":
                volume = max(0.0, volume - 0.1)
                pygame.mixer.music.set_volume(volume)
                print(f"Volume: {int(volume * 100)}%")
            elif command == "N" or command == "NEXT":
                pygame.mixer.music.stop()
                playing = False
                stop_progress.set()
                # Check queue first
                if queue_list:
                    current_index = queue_list.pop(0)
                    # Find this index in play_order
                    if current_index in play_order:
                        play_index = play_order.index(current_index)
                    else:
                        play_index += 1
                else:
                    play_index += 1
                break  # Break to load next song
            elif command == "B" or command == "BACK" or command == "PREVIOUS":
                pygame.mixer.music.stop()
                playing = False
                stop_progress.set()
                play_index = max(0, play_index - 1)
                break  # Break to load previous song
            elif command == "R" or command == "REPEAT":
                if repeat_mode == "off":
                    repeat_mode = "one"
                    print("Repeat: Current song")
                elif repeat_mode == "one":
                    repeat_mode = "all"
                    print("Repeat: All")
                else:
                    repeat_mode = "off"
                    print("Repeat: Off")
            elif command == "T" or command == "TIME":
                display_time_position()
            elif command == "H" or command == "SHUFFLE":
                shuffle_state[0] = not shuffle_state[0]
                if shuffle_state[0]:
                    # Reshuffle the remaining songs
                    remaining_indices = play_order[play_index+1:]
                    random.shuffle(remaining_indices)
                    play_order = play_order[:play_index+1] + remaining_indices
                    print(f"Shuffle: ON")
                else:
                    # Reorder to sequential from current position
                    current_song = play_order[play_index]
                    play_order = list(range(len(song_files)))
                    if current_song in play_order:
                        play_index = play_order.index(current_song)
                    print(f"Shuffle: OFF")
            elif command == "I" or command == "INFO":
                print(f"\nFile: {filepath}")
                if metadata:
                    for key, value in metadata.items():
                        if key != 'length':
                            print(f"{key.capitalize()}: {value}")
                    if 'length' in metadata:
                        print(f"Length: {format_time(metadata['length'])}")
                print(f"Volume: {int(volume * 100)}%")
                print(f"Repeat: {repeat_mode.capitalize()}")
                print(f"Shuffle: {'On' if shuffle_state[0] else 'Off'}")
                # Also show current position in info
                if playing and pygame.mixer.music.get_busy():
                    print()
                    display_time_position()
            else:
                print("Invalid command. Type [I]nfo for help.")
            
            # Check if song ended after command (for auto-advance)
            if not pygame.mixer.music.get_busy() and playing:
                if repeat_mode == "one":
                    # Restart current song
                    pygame.mixer.music.load(file_path)
                    pygame.mixer.music.play()
                    current_pos[0] = 0.0
                else:
                    # Auto-advance to next song
                    stop_progress.set()
                    # Check queue first
                    if queue_list:
                        current_index = queue_list.pop(0)
                        # Find this index in play_order
                        if current_index in play_order:
                            play_index = play_order.index(current_index)
                        else:
                            play_index += 1
                    else:
                        play_index += 1
                    break  # Break to load next song
def load_saved_position():
    """Load last played song position from config file"""
    config_file = Path("wavrun_config.json")
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_position(folder, current_index):
    """Save current song position to config file"""
    config_file = Path("wavrun_config.json")
    try:
        config = load_saved_position()
        config['last_folder'] = os.path.abspath(folder)
        config['last_index'] = current_index
        with open(config_file, 'w') as f:
            json.dump(config, f)
    except:
        pass

def filter_songs(song_files, search_term):
    """Filter songs by search term"""
    if not search_term:
        return song_files
    search_term = search_term.lower()
    return [song for song in song_files if search_term in song.lower()]

def main():
    try:
        pygame.mixer.init()
    except pygame.error as e:
        print("Audio engine initialization failed:", e)
        return

    print("*** wavrun ***")
    
    # Load saved position
    saved_config = load_saved_position()
    default_folder = saved_config.get('last_folder', 'music')
    if not os.path.isdir(default_folder):
        default_folder = 'music'
    
    folder_input = input(f"Enter music folder path (or press Enter for '{default_folder}'): ").strip()
    
    if not folder_input:
        folder = default_folder
    else:
        folder = folder_input

    if not os.path.isdir(folder):
        print(f"Folder '{folder}' does not exist")
        return
    
    # Recursively search for audio files
    print("Scanning for audio files...")
    song_files = []
    for root, dirs, files in os.walk(folder):
        for file in files:
            if file.lower().endswith((".flac", ".mp3", ".wav")):
                # Store relative path from the base folder
                rel_path = os.path.relpath(os.path.join(root, file), folder)
                song_files.append(rel_path)
    
    song_files.sort()  # Sort alphabetically

    if not song_files:
        print("No audio files found")
        return
    
    print(f"Found {len(song_files)} audio file(s)")
    
    # Restore last position if same folder
    current_index = 0
    if saved_config.get('last_folder') == os.path.abspath(folder):
        saved_index = saved_config.get('last_index', 0)
        if 0 <= saved_index < len(song_files):
            current_index = saved_index
            print(f"Resuming from: {song_files[current_index]}")
    
    shuffle_mode = [False]  # Use list for mutable reference
    queue = []
    search_term = ""
    filtered_songs = song_files

    while True:
        print("\n" + "="*60)
        print("*** wavrun ***")
        print("="*60)
        
        # Show current mode
        mode_info = []
        if shuffle_mode[0]:
            mode_info.append("SHUFFLE")
        if queue:
            mode_info.append(f"QUEUE({len(queue)})")
        if mode_info:
            print(f"Mode: {', '.join(mode_info)}")
        
        # Filter songs if search term exists
        if search_term:
            filtered_songs = filter_songs(song_files, search_term)
            print(f"Search: '{search_term}' ({len(filtered_songs)} results)")
        else:
            filtered_songs = song_files
        
        if not filtered_songs:
            print("No songs match your search")
            search_term = ""
            continue
        
        # Show songs (limit display to 50 for performance)
        display_limit = 50
        print(f"\nSongs ({len(filtered_songs)} total):")
        for index, song in enumerate(filtered_songs[:display_limit], start=1):
            marker = " <--" if song_files.index(song) == current_index else ""
            print(f"{index}. {song}{marker}")
        
        if len(filtered_songs) > display_limit:
            print(f"... and {len(filtered_songs) - display_limit} more")
        
        print("\nCommands:")
        print("  [number] - Play song")
        print("  [A]dd [number] - Add song to queue")
        print("  [S]huffle - Toggle shuffle mode")
        print("  [/search] - Search/filter songs")
        print("  [C]lear search - Clear search filter")
        print("  [Q]uit - Exit")
        
        choice_input = input("\n> ").strip()
        
        if not choice_input:
            continue
        
        command = choice_input.upper()
        
        if command == "Q" or command == "QUIT":
            print("Quitting...")
            save_position(folder, current_index)
            break
        
        elif command == "S" or command == "SHUFFLE":
            shuffle_mode[0] = not shuffle_mode[0]
            print(f"Shuffle mode: {'ON' if shuffle_mode[0] else 'OFF'}")
            continue
        
        elif command.startswith("/"):
            search_term = choice_input[1:].strip()
            if not search_term:
                search_term = ""
                print("Search cleared")
            continue
        
        elif command == "C" or command == "CLEAR":
            search_term = ""
            print("Search cleared")
            continue
        
        elif command.startswith("A ") or command.startswith("ADD "):
            # Add to queue
            num_str = command.split()[-1] if len(command.split()) > 1 else ""
            if num_str.isdigit():
                queue_index = int(num_str) - 1
                if 0 <= queue_index < len(filtered_songs):
                    song_to_queue = filtered_songs[queue_index]
                    queue.append(song_files.index(song_to_queue))
                    print(f"Added to queue: {song_to_queue}")
                else:
                    print("Invalid song number")
            else:
                print("Usage: A [number] or ADD [number]")
            continue
        
        elif choice_input.isdigit():
            # Play song
            choice = int(choice_input) - 1
            if 0 <= choice < len(filtered_songs):
                current_index = song_files.index(filtered_songs[choice])
                save_position(folder, current_index)
                
                result = play_music(folder, song_files, current_index, shuffle_mode, "off", queue)
                
                if isinstance(result, tuple):
                    status, new_index = result
                    current_index = new_index
                    if status == "quit":
                        save_position(folder, current_index)
                        break
                    elif status == "stop":
                        # Continue in main menu
                        save_position(folder, current_index)
                        continue
                else:
                    # Backward compatibility
                    if result == "quit":
                        save_position(folder, current_index)
                        break
                    elif result == "stop":
                        save_position(folder, current_index)
                        continue
            else:
                print("Invalid choice")
        
        else:
            print("Invalid command. Enter a number to play, or type a command.")
if __name__ == '__main__':
    main()
