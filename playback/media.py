"""Local media inspection and incremental HLS preparation, without video re-encoding."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

from .model import PlaybackError, check_cancel
from .processes import run


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    duration: float
    archive_key: str
    discontinuity: bool = False


def probe(path, settings, cancel):
    output, warnings = run([settings.ffprobe, '-v', 'warning', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=format_name,start_time,duration,size:stream=codec_type,codec_name,start_time,width,height,sample_rate,channels',
        '-of', 'json', str(path)], cancel, timeout=45)
    try:
        data = json.loads(output)
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video')
        audio = next((s for s in data['streams'] if s.get('codec_type') == 'audio'), None)
        duration = float(data['format']['duration'])
        start = float(data['format'].get('start_time', 0))
        video_start = float(video.get('start_time', start))
        if not math.isfinite(duration) or not 0 < duration < 7*86400 or not math.isfinite(start+video_start):
            raise ValueError
        if video.get('codec_name') not in ('h264', 'hevc'):
            raise PlaybackError('video-codec-unsupported')
        return {'duration': duration, 'container': data['format']['format_name'],
            'video': video['codec_name'], 'audio': audio.get('codec_name', '') if audio else '',
            'width': int(video.get('width', 0)), 'height': int(video.get('height', 0)),
            'source_start': start, 'video_start_offset': video_start-start,
            'warnings': bool(warnings), 'finalization': 'unconfirmed'}
    except (KeyError, ValueError, TypeError, StopIteration):
        raise PlaybackError('media-invalid') from None


def read_segments(directory, recording):
    playlist = directory/'source.m3u8'
    if not playlist.exists():
        return (), False
    try:
        lines = playlist.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return (), False  # Windows can momentarily hold a renamed playlist.
    if not lines or lines[0] != '#EXTM3U' or len(lines) > 200000:
        raise PlaybackError('playlist-invalid')
    duration, elapsed, result = None, 0., []
    for line in lines:
        if line.startswith('#EXTINF:'):
            try:
                duration = float(line.split(':', 1)[1].split(',', 1)[0])
            except ValueError:
                raise PlaybackError('playlist-invalid') from None
            if not math.isfinite(duration) or not 0 < duration < 600:
                raise PlaybackError('playlist-invalid')
        elif line and not line.startswith('#'):
            if duration is None or not re.fullmatch(r'segment-\d{5,8}\.ts', line):
                raise PlaybackError('playlist-invalid')
            path = directory/line
            if path.is_symlink() or path.resolve().parent != directory.resolve() or not path.is_file():
                raise PlaybackError('playlist-invalid')
            result.append(Segment(path, recording.start+elapsed, duration, recording.key))
            elapsed += duration
            duration = None
    return tuple(result), '#EXT-X-ENDLIST' in lines


def hls_command(source, directory, media, settings):
    audio = ['-c:a', 'copy'] if media['audio'] == 'aac' else ['-c:a', 'aac', '-b:a', '64k']
    return [settings.ffmpeg, '-nostdin', '-hide_banner', '-v', 'warning', '-y',
        '-protocol_whitelist', 'file,pipe', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'copy', *audio, '-avoid_negative_ts', 'make_zero',
        '-f', 'hls', '-hls_time', '4', '-hls_list_size', '0', '-hls_playlist_type', 'event',
        # temp_file publishes completed segments; do not claim independent_segments
        # without inspecting actual keyframes and codec parameter availability.
        '-hls_flags', 'temp_file', '-hls_segment_filename', str(directory/'segment-%05d.ts'),
        str(directory/'source.m3u8')]


def prepare(source, directory, recording, media, settings, cancel, publish, budget, input_chunks=None):
    previous = [0]
    def tick():
        budget()
        segments, complete = read_segments(directory, recording)
        if len(segments) != previous[0]:
            previous[0] = len(segments)
            publish(segments, False)
    _, warnings = run(hls_command(source, directory, media, settings), cancel, timeout=1800, tick=tick,
                      input_chunks=input_chunks)
    check_cancel(cancel)
    segments, complete = read_segments(directory, recording)
    if not segments or not complete:
        raise PlaybackError('preparation-incomplete')
    publish(segments, True)
    return dict(media, prepared_duration=sum(s.duration for s in segments),
                preparation_warnings=bool(warnings))


def growing_chunks(path, finished, failed, cancel):
    """Read the received MPEG prefix without treating temporary EOF as media EOF."""
    with path.open('rb') as source:
        while True:
            check_cancel(cancel)
            if failed.is_set():
                raise PlaybackError('incomplete-download')
            chunk = source.read(256*1024)
            if chunk:
                yield chunk
            elif finished.is_set():
                return
            else:
                cancel.wait(.05)


def packet_bounds(path, settings, cancel):
    """Measure preserved video packet timestamps; do not infer a keyframe cut."""
    output, _ = run([settings.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-select_streams', 'v:0', '-show_packets', '-show_entries',
        'packet=pts_time,duration_time:packet_side_data=', '-of', 'csv=p=0', str(path)],
        cancel, timeout=120, limit=16*1024*1024)
    first, last = math.inf, -math.inf
    for line in output.decode('utf-8').splitlines():
        if not line.strip():
            continue
        try:
            parts=line.split(',')
            pts,duration=float(parts[0]),float(parts[1])
            if not math.isfinite(pts+duration) or duration<=0:
                raise ValueError
        except (ValueError, IndexError):
            raise PlaybackError('export-timestamps') from None
        first=min(first,pts)
        last=max(last,pts+duration)
    if first>=last:
        raise PlaybackError('export-timestamps')
    return first,last
