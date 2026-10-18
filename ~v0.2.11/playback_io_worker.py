"""Private, killable archive network worker. Never launches a window or player."""
from playback.remote import worker_main

if __name__ == '__main__':
    worker_main()
