"""Real disposable subprocess, simulating hung native calls and event bursts."""
import json
import sys
import threading
import time
from pathlib import Path

config = json.loads(sys.stdin.buffer.readline())
generation = config['generation']
if config.get('audio_probe'):
    with Path(config['audio_probe']).open('a') as output:
        output.write(json.dumps({key: config[key] for key in ('generation', 'muted', 'volume')})+'\n')


def emit(kind, **values):
    print(json.dumps(dict(kind=kind, generation=generation, **values)), flush=True)


def commands():
    for line in sys.stdin.buffer:
        message = json.loads(line)
        if config.get('audio_probe') and 'muted' in message:
            with Path(config['audio_probe']).open('a') as output:
                output.write(json.dumps(dict(generation=generation, **message))+'\n')
        emit('audio-observed', **message)
    if config.get('block_stop'):
        emit('operation', name='stop', phase='enter')
        while True:
            time.sleep(1)
    stopped.set()


stopped = threading.Event()
threading.Thread(target=commands, daemon=True).start()
if config.get('block_discover'):
    emit('operation', name='discover', phase='enter')
    stopped.wait(60)
elif config.get('burst'):
    for _ in range(20):
        emit('failure', reason='vlc-error')
    stopped.wait(60)
else:
    # Stale failure must not retire this session.
    print(json.dumps({'kind': 'failure', 'generation': generation-1, 'reason': 'ended'}), flush=True)
    frame = 0
    while not stopped.wait(.05):
        frame += 1
        emit('sample', received=frame*1024, decoded=frame, displayed=frame, audio=frame,
             width=320, height=180, bitrate=.2)
        if config.get('fail_first') and generation == 1 and frame == 8:
            emit('failure', reason='ended')
            stopped.wait(60)
            break
