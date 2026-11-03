"""Standalone prototype: python -m core.audio path/to/song.flac."""
import argparse
import json
import multiprocessing
import sys
import time

from .decoder import DecoderError, DecoderSession, PCMFormat


def decode_only(path, output=PCMFormat(), idle_timeout=15, *, start_seconds=0):
    """Validate the complete decoder pipeline without opening an audio device."""
    import numpy as np
    info = None
    frames = 0
    peak = 0.0
    with DecoderSession(path, output, start_seconds=start_seconds) as decoder:
        deadline = time.monotonic() + idle_timeout
        while True:
            message = decoder.receive()
            if message is None:
                if time.monotonic() >= deadline:
                    raise DecoderError("Timed out waiting for decoded audio")
                continue
            deadline = time.monotonic() + idle_timeout
            kind, value = message
            if kind == "info":
                info = value
            elif kind == "pcm":
                samples = np.frombuffer(value, dtype=np.float32).reshape(-1, output.channels)
                if not np.isfinite(samples).all():
                    raise DecoderError("Decoder produced non-finite audio samples")
                frames += len(samples)
                if samples.size:
                    peak = max(peak, float(np.abs(samples).max()))
            elif kind == "error":
                raise DecoderError(value)
            elif kind == "eof":
                if info is None or value != frames:
                    raise DecoderError("Decoder returned incomplete audio")
                return dict(info, frames=frames, decoded_seconds=frames / output.rate, peak=peak)
            else:
                raise DecoderError(f"Unknown decoder message: {kind}")


def control_command(player, line):
    """Line-oriented controls keep terminal input on the main thread on every OS."""
    command, _, value = line.strip().partition(" ")
    command = command.lower()
    value = value.strip()
    if command in ("quit", "q", "exit"):
        return False
    if command == "pause":
        player.pause()
    elif command in ("resume", "play"):
        player.play()
    elif command == "stop":
        player.stop()
    elif command == "seek":
        player.set_time(float(value) * 1000)
    elif command == "volume":
        player.set_volume(float(value))
    elif command == "load" and value:
        player.load(value.strip('"'))
        player.play()
    elif command == "status":
        print(json.dumps({"state": player.state.value, "position_ms": player.get_pos(),
                          "duration_ms": player.get_length(), "volume": player.volume,
                          "error": player.error}))
    elif command:
        raise ValueError("Commands: pause, resume, stop, seek SECONDS, volume 0..1, load PATH, status, quit")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wavrun standalone playback prototype (no VLC)")
    parser.add_argument("file", nargs="?", help="Local file or mounted network-share path")
    parser.add_argument("--decode-only", action="store_true", help="Decode without opening speakers")
    parser.add_argument("--list-devices", action="store_true", help="List PortAudio devices")
    parser.add_argument("--device", help="Output device index or name; default is the system output")
    parser.add_argument("--volume", type=float, default=0.7, help="Playback volume, 0 to 1")
    parser.add_argument("--seconds", type=float, help="Stop playback after this many seconds")
    parser.add_argument("--seek", type=float, default=0, help="Start at this position in seconds")
    parser.add_argument("--interactive", action="store_true", help="Enable line-oriented playback controls")
    args = parser.parse_args(argv)
    if args.seconds is not None and (not 0 < args.seconds < float("inf")):
        parser.error("--seconds must be a positive finite number")
    if not 0 <= args.seek < float("inf"):
        parser.error("--seek must be a nonnegative finite number")
    if args.interactive and (args.seconds is not None or args.decode_only):
        parser.error("--interactive cannot be combined with --seconds or --decode-only")
    try:
        if args.list_devices:
            import sounddevice as sd
            print(sd.query_devices())
            return 0
        if not args.file:
            parser.error("Provide a file or use --list-devices")
        if args.decode_only:
            print(json.dumps(decode_only(args.file, start_seconds=args.seek), indent=2))
            return 0
        from .player import AudioPlayer
        device = int(args.device) if args.device and args.device.isdecimal() else args.device
        with AudioPlayer(device=device, volume=args.volume) as player:
            player.load(args.file)
            player.set_time(args.seek * 1000)
            player.play()
            print(f"Playing {args.file} ({player.format.rate} Hz, {player.format.channels} channels)")
            if args.interactive:
                print("Controls: pause, resume, stop, seek SECONDS, volume 0..1, load PATH, status, quit")
                print("Interactive mode stays open after a track ends. Type quit to exit.")
                while True:
                    try:
                        if not control_command(player, input("> ")):
                            break
                    except EOFError:
                        break
                    except (ValueError, RuntimeError) as error:
                        print(f"Playback error: {error}", file=sys.stderr)
                return 0
            deadline = time.monotonic() + args.seconds if args.seconds else None
            while not player.wait(0.1):
                if deadline is not None and time.monotonic() >= deadline:
                    break
            print(f"Output: {player.frames_played} frames; buffer underruns: {player.underruns}")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Playback error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
