"""Read TS timing and translate only transport clocks in the loopback response.

No source or cached derivative is rewritten. One constant per native archive is
applied to video/audio PTS, DTS and PCR, preserving their spacing and A/V offset.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .model import PlaybackError

CLOCK = 90000
WRAP = 1 << 33
PACKET = 188


def pts(data):
    return (((data[0] >> 1) & 7) << 30 | data[1] << 22 |
            (data[2] >> 1) << 15 | data[3] << 7 | data[4] >> 1)


def put_pts(data, offset, value):
    value %= WRAP
    data[offset:offset+5] = bytes(((data[offset] & 0xf0) | ((value >> 29) & 14) | 1,
        (value >> 22) & 255, ((value >> 14) & 254) | 1,
        (value >> 7) & 255, ((value << 1) & 254) | 1))


def payload_offset(packet):
    if len(packet) != PACKET or packet[0] != 0x47 or packet[3] & 0xc0:
        raise PlaybackError('segment-timestamps')
    return 4 + (packet[4]+1 if packet[3] & 0x20 else 0)


@lru_cache(maxsize=8192)
def first_video_pts(path, size, modified):
    # Metadata is only cached for immutable, completed HLS segments. Probe at
    # most 2 MiB; a missing video header is an explicit failure, never zero.
    with Path(path).open('rb') as source:
        for _ in range(2*1024*1024//PACKET):
            packet = source.read(PACKET)
            if not packet:
                break
            start = payload_offset(packet)
            if not packet[1] & 0x40 or not packet[3] & 0x10:
                continue
            data = packet[start:]
            if (len(data) >= 14 and data[:3] == b'\x00\x00\x01' and
                    0xe0 <= data[3] <= 0xef and data[7] & 0x80):
                return pts(data[9:14])/CLOCK
    raise PlaybackError('segment-timestamps')


def segment_pts(path):
    stat = path.stat()
    return first_video_pts(str(path), stat.st_size, stat.st_mtime_ns)


def translate(data, ticks):
    """Packet-aligned bytes, including adaptation-only PCR packets."""
    result = bytearray(data)
    for offset in range(0, len(result), PACKET):
        packet = result[offset:offset+PACKET]
        start = payload_offset(packet)
        if packet[3] & 0x20 and packet[4] >= 7:
            cursor = 6
            for flag in (0x10, 0x08):  # PCR, then optional OPCR.
                if packet[5] & flag:
                    if cursor+6 > min(start, PACKET):
                        raise PlaybackError('segment-timestamps')
                    base = (packet[cursor] << 25 | packet[cursor+1] << 17 |
                            packet[cursor+2] << 9 | packet[cursor+3] << 1 | packet[cursor+4] >> 7)
                    base = (base+ticks) % WRAP
                    packet[cursor:cursor+5] = bytes((base >> 25, (base >> 17) & 255,
                        (base >> 9) & 255, (base >> 1) & 255,
                        ((base & 1) << 7) | (packet[cursor+4] & 0x7f)))
                    cursor += 6
        if packet[1] & 0x40 and packet[3] & 0x10 and start+14 <= PACKET:
            pes = packet[start:]
            if pes[:3] == b'\x00\x00\x01' and (0xc0 <= pes[3] <= 0xef or pes[3] == 0xbd):
                flags = pes[7] & 0xc0
                if flags in (0x80, 0xc0):
                    put_pts(packet, start+9, pts(pes[9:14])+ticks)
                if flags == 0xc0:
                    if start+19 > PACKET:
                        raise PlaybackError('segment-timestamps')
                    put_pts(packet, start+14, pts(pes[14:19])+ticks)
        result[offset:offset+PACKET] = packet
    return bytes(result)


@dataclass(frozen=True)
class TransportResource:
    path: Path
    ticks: int

    def chunks(self, handle, start, end):
        # Transform whole packets even for unaligned HTTP byte ranges. Slicing
        # afterwards keeps Content-Length and repeated ranges byte-identical.
        aligned = start//PACKET*PACKET
        handle.seek(aligned)
        while aligned <= end:
            data = handle.read(min(PACKET*348, ((end-aligned)//PACKET+1)*PACKET))
            if not data:
                raise PlaybackError('segment-timestamps')
            data = translate(data, self.ticks)
            yield data[max(0, start-aligned):min(len(data), end-aligned+1)]
            aligned += len(data)
