"""Open only the archive browser; never opens live streams or PTZ windows."""
import argparse
from playback.ui import PlaybackWindow


def main():
    parser = argparse.ArgumentParser(description='Lecteur des archives microSD')
    parser.add_argument('--camera', type=int, help='ID local de la caméra présélectionnée')
    parser.add_argument('--fixture-directory', help='Banc synthétique local, sans lecture des identifiants caméra')
    args = parser.parse_args()
    window = PlaybackWindow(None, args.camera,fixture_directory=args.fixture_directory)
    window.window.mainloop()


if __name__ == '__main__':
    main()
