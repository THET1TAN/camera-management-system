"""Loopback-only H.264/PCMU RTSP fixture. No camera, firewall or admin rights.

Small test server, intentionally limited to RTP interleaved over RTSP/TCP. FFmpeg
generates a moving synthetic source; this is not a production RTSP implementation.
"""
import base64
import math
import re
import socket
import struct
import subprocess
import threading
import time


def synthetic_frames(ffmpeg='ffmpeg', fps=10):
    result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
        '-i', f'testsrc2=size=320x180:rate={fps}', '-t', '10', '-an', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', str(max(1, fps)),
        '-x264-params', 'aud=1:repeat-headers=1', '-f', 'h264', 'pipe:1'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
    if result.returncode:
        raise RuntimeError('FFmpeg could not generate the synthetic source')
    units = [unit for unit in re.split(b'\x00\x00\x00?\x01', result.stdout) if unit]
    sps = next(unit for unit in units if unit[0] & 31 == 7)
    pps = next(unit for unit in units if unit[0] & 31 == 8)
    frames, frame = [], []
    for unit in units:
        if unit[0] & 31 == 9 and frame:
            frames.append(frame)
            frame = []
        frame.append(unit)
    if frame:
        frames.append(frame)
    return frames, sps, pps


def ulaw(value):
    sign = 0x80 if value < 0 else 0
    value = min(abs(value), 32635) + 132
    exponent = max(0, min(7, value.bit_length()-8))
    return (~(sign | exponent << 4 | ((value >> (exponent+3)) & 15))) & 255


class RTSPFixture:
    def __init__(self, frames, sps, pps, fps=10):
        self.frames, self.sps, self.pps, self.fps = frames, sps, pps, fps
        self.stop = threading.Event()
        self.mode = 'live'
        self.clients = set()
        self.lock = threading.Lock()
        self.threads = []
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.server.settimeout(.2)
        self.url = f'rtsp://127.0.0.1:{self.server.getsockname()[1]}/synthetic'
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def set_mode(self, mode):
        assert mode in ('live', 'offline', 'silence')
        self.mode = mode
        if mode == 'offline':
            with self.lock:
                clients = list(self.clients)
            for client in clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.mode == 'offline':
                client.close()
                continue
            with self.lock:
                self.clients.add(client)
            thread = threading.Thread(target=self._client, args=(client,), daemon=True)
            self.threads = [t for t in self.threads if t.is_alive()]
            self.threads.append(thread)
            thread.start()

    def _sdp(self):
        return ('v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Synthetic test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n'
            'a=control:*\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n'
            f'a=fmtp:96 packetization-mode=1;sprop-parameter-sets={base64.b64encode(self.sps).decode()},'
            f'{base64.b64encode(self.pps).decode()}\r\na=control:track0\r\n'
            'm=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000/1\r\na=control:track1\r\n').encode()

    @staticmethod
    def _rtp(client, channel, payload, sequence, timestamp, marker, payload_type):
        packet = struct.pack('!BBHII', 0x80, payload_type | (0x80 if marker else 0),
                             sequence & 65535, timestamp & 0xffffffff, 1234+channel) + payload
        client.sendall(b'$' + bytes([channel]) + struct.pack('!H', len(packet)) + packet)

    def _client(self, client):
        client.settimeout(.02)
        buffer, playing, channels = b'', False, {}
        sequences = [0, 0]
        next_video = next_audio = time.monotonic()
        # Start every connection at an IDR. After that the image evolves normally.
        frame_index, video_tick, audio_tick = 0, 0, 0
        try:
            while not self.stop.is_set() and self.mode != 'offline':
                try:
                    data = client.recv(65536)
                    if not data:
                        break
                    buffer += data
                except socket.timeout:
                    pass
                while buffer:
                    if buffer[:1] == b'$':
                        if len(buffer) < 4:
                            break
                        size = 4 + struct.unpack('!H', buffer[2:4])[0]
                        if len(buffer) < size:
                            break
                        buffer = buffer[size:]
                        continue
                    if b'\r\n\r\n' not in buffer:
                        break
                    header, buffer = buffer.split(b'\r\n\r\n', 1)
                    lines = header.decode().split('\r\n')
                    method, url, _ = lines[0].split(' ', 2)
                    headers = {key.lower(): value.strip() for key, value in
                               (line.split(':', 1) for line in lines[1:] if ':' in line)}
                    body = b''
                    extra = 'Session: 12345678\r\n'
                    if method == 'OPTIONS':
                        extra += 'Public: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN\r\n'
                    elif method == 'DESCRIBE':
                        body = self._sdp()
                        extra += f'Content-Type: application/sdp\r\nContent-Base: {self.url}/\r\n'
                    elif method == 'SETUP':
                        track = 1 if 'track1' in url else 0
                        match = re.search(r'interleaved=(\d+)-(\d+)', headers.get('transport', ''))
                        if not match:
                            break
                        channels[track] = int(match[1])
                        extra += f'Transport: RTP/AVP/TCP;unicast;interleaved={match[1]}-{match[2]}\r\n'
                    elif method == 'PLAY':
                        playing = True
                        next_video = next_audio = time.monotonic()
                        extra += 'Range: npt=0.000-\r\n'
                    elif method == 'TEARDOWN':
                        playing = False
                    reply = (f'RTSP/1.0 200 OK\r\nCSeq: {headers.get("cseq", "1")}\r\n'
                             f'{extra}Content-Length: {len(body)}\r\n\r\n').encode() + body
                    client.sendall(reply)
                if not playing or self.mode != 'live':
                    next_video = next_audio = time.monotonic()
                    continue
                now = time.monotonic()
                if 0 in channels and now >= next_video:
                    frame = self.frames[frame_index % len(self.frames)]
                    frame_index += 1
                    for index, unit in enumerate(frame):
                        if len(unit) <= 1200:
                            parts = [unit]
                        else:
                            parts = []
                            for offset in range(1, len(unit), 1198):
                                chunk = unit[offset:offset+1198]
                                flag = (0x80 if offset == 1 else 0) | (0x40 if offset+1198 >= len(unit) else 0)
                                parts.append(bytes([(unit[0] & 0xe0) | 28, (unit[0] & 31) | flag])+chunk)
                        for part_index, part in enumerate(parts):
                            last = index == len(frame)-1 and part_index == len(parts)-1
                            self._rtp(client, channels[0], part, sequences[0], video_tick, last, 96)
                            sequences[0] += 1
                    video_tick += int(90000/self.fps)
                    next_video += 1/self.fps
                if 1 in channels and now >= next_audio:
                    sound = bytes(ulaw(int(3000*math.sin(2*math.pi*440*(audio_tick+i)/8000))) for i in range(160))
                    self._rtp(client, channels[1], sound, sequences[1], audio_tick, False, 0)
                    sequences[1] += 1
                    audio_tick += 160
                    next_audio += .02
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.clients.discard(client)
            client.close()

    def close(self):
        self.stop.set()
        self.set_mode('offline')
        self.server.close()
        self.thread.join(1)
        for thread in self.threads:
            thread.join(1)
