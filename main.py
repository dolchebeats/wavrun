import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
import pygame

def play_music(folder, filename):

    file_path = os.path.join(folder, filename)

    if not os.path.exists(file_path):
        print(f"File '{file_path}' does not exist")
        return

    pygame.mixer.music.load(file_path)
    pygame.mixer.music.play()

    print(f"\nNow playing: {filename}")
    print("Commands: [P]lay/Pause, [S]top, [Q]uit")

    playing = True

    while True:

        command = input("> ").upper()

        if command == "P" and playing:
            pygame.mixer.music.pause()
            playing = False
            print("Paused")
        elif command == "P" and not playing:
            pygame.mixer.music.unpause()
            playing = True
            print("Unpaused")
        elif command == "S":
            pygame.mixer.music.stop()
            playing = False
            print("Stopping...")
            return
        elif command == "Q":
            print("Quitting...")

        else:
            print("Invalid command")
def main():
    try:
        pygame.mixer.init()
    except pygame.error as e:
        print("Audio engine initialization failed:", e)
        return

    folder = "music"

    if not os.path.isdir(folder):
        print(f"Folder '{folder}' does not exist")
        #os.mkdir(folder)
    flac_files = [file for file in os.listdir(folder) if file.endswith(".flac")]
    mp3_files = [file for file in os.listdir(folder) if file.endswith(".mp3")]
    wav_files = [file for file in os.listdir(folder) if file.endswith(".wav")]


    song_files = flac_files + mp3_files + wav_files

    if not song_files:
        print("No audio files found")

    while True:
        print("*** wavrun ***")
        print("Songs:")

        for index, song in enumerate(song_files, start=1):
            print(f"{index}. {song}")

        choice_input = input("\nEnter song number to play (or 'Q' to quit): ")
        if choice_input.upper() == "Q" or choice_input.upper() == "QUIT":
            print("Quitting...")
            break

        if not choice_input.isdigit():
            print("Enter a valid number")
            continue

        choice = int(choice_input) - 1

        if 0 <= choice < len(song_files):
            play_music(folder, song_files[choice])
        else:
            print("Invalid choice")
if __name__ == '__main__':
    main()
