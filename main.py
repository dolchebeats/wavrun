"""Application entry point, also used by the frozen terminal executable."""
import multiprocessing


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Wavrun terminal music player")
    parser.add_argument("--no-splash", action="store_true", help="Skip the startup splash")
    parser.add_argument("--diagnostics", action="store_true", help="Print dependency and resource information")
    parser.add_argument("--self-test", action="store_true", help="Check headless UI startup/shutdown without audio")
    parser.add_argument("--decode-only", metavar="FILE", help="Validate a file's decoding without audio output")
    args = parser.parse_args()
    if args.decode_only:
        from core.audio.__main__ import main as audio_main
        return audio_main([args.decode_only, "--decode-only"])
    if args.diagnostics or args.self_test:
        from core.diagnostics import report
        return report(self_test=args.self_test)
    from core.cli_playback import run_tui
    run_tui(splash=not args.no_splash)
    return 0

if __name__ == "__main__":
    # Must run before argparse/UI imports: frozen spawn workers reuse this executable.
    multiprocessing.freeze_support()
    raise SystemExit(main())

