"""Bounded preparation ownership, separate from playback coordination."""
import threading


class PreparationJob:
    def __init__(self, entry, work, predecessor=None):
        self.entry, self.predecessor = entry, predecessor
        self.done = threading.Event()
        self.error = None
        self.reported = False
        def run():
            try:
                work()
            except Exception as exc:
                self.error = exc
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, name='Archive preparation', daemon=True)

    def start(self):
        self.thread.start()

    def join(self):
        self.thread.join()
