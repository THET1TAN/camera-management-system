"""Retired C entry point: explicitly redirect to the sequential r9 B candidate."""
from camera_viewer_direct_test import main as launch_b


def main():
    print('r9 C was withdrawn after a key-release regression. Starting r9 B instead.')
    launch_b()


if __name__ == '__main__':
    main()
