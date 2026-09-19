"""Open only the archive browser; never opens live streams or PTZ windows."""
import argparse
from datetime import date
from playback.presentation import TEXT


def main():
    parser = argparse.ArgumentParser(description=TEXT['cli_description'])
    parser.add_argument('--camera', type=int, help=TEXT['cli_camera'])
    parser.add_argument('--fixture-directory', help=TEXT['cli_fixture'])
    parser.add_argument('--date', type=date.fromisoformat, help=TEXT['cli_date'])
    parser.add_argument('--day-only', action='store_true', help=TEXT['cli_day_only'])
    args = parser.parse_args()
    if args.day_only and args.camera is None:
        parser.error(TEXT['cli_day_requires_camera'])
    from playback.ui import PlaybackWindow
    window = PlaybackWindow(None, args.camera,fixture_directory=args.fixture_directory,
                            selected_day=args.date,day_only=args.day_only)
    window.window.mainloop()


if __name__ == '__main__':
    main()
