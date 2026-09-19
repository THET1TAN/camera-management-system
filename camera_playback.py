"""Open only the archive browser; never opens live streams or PTZ windows."""
import argparse
from datetime import date


def main():
    parser = argparse.ArgumentParser(description='Lecteur des archives microSD')
    parser.add_argument('--camera', type=int, help='ID local de la caméra présélectionnée')
    parser.add_argument('--fixture-directory', help='Banc synthétique local, sans lecture des identifiants caméra')
    parser.add_argument('--date', type=date.fromisoformat, help='Journée locale YYYY-MM-DD')
    parser.add_argument('--day-only', action='store_true', help='Interroger uniquement la journée sélectionnée')
    args = parser.parse_args()
    if args.day_only and args.camera is None:
        parser.error('--day-only nécessite --camera pour limiter la recherche à une seule caméra.')
    from playback.ui import PlaybackWindow
    window = PlaybackWindow(None, args.camera,fixture_directory=args.fixture_directory,
                            selected_day=args.date,day_only=args.day_only)
    window.window.mainloop()


if __name__ == '__main__':
    main()
