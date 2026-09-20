"""Local media inspection and incremental HLS preparation, without video re-encoding."""
from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import re

from .model import PlaybackError, check_cancel
from .processes import run
from .timestamps import segment_pts, WRAP, CLOCK

PREFIX_STEPS = (256*1024, 1024*1024, 4*1024*1024, 16*1024*1024, 64*1024*1024)


@dataclass(frozen=True)
class PrefixResult:
    media: dict = None
    reason: str = ''
    retry: bool = False


def mp4_prefix(data):
    """Inspect bounded top-level boxes, never search compressed bytes for 'moov'."""
    offset, moov = 0, False
    while offset+8 <= len(data):
        size = int.from_bytes(data[offset:offset+4], 'big')
        kind = data[offset+4:offset+8]
        header = 8
        if size == 1:
            if offset+16 > len(data):
                return 'prefix-insufficient'
            size = int.from_bytes(data[offset+8:offset+16], 'big')
            header = 16
        if kind in (b'mdat', b'moof'):
            return '' if moov else 'mp4-metadata-after-media'
        if size < header:
            return 'mp4-layout-unsupported'
        if offset+size > len(data):
            return 'prefix-insufficient'
        if kind == b'moov':
            moov = True
        offset += size
    return 'prefix-insufficient'


def media_info(data, warnings=0, complete=True):
    try:
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video')
        audio = next((s for s in data['streams'] if s.get('codec_type') == 'audio'), None)
        duration = float(data['format']['duration']) if complete else None
        start = float(data['format'].get('start_time', video.get('start_time', 0)))
        video_start = float(video.get('start_time', start))
        width, height = int(video.get('width', 0)), int(video.get('height', 0))
        sar = Fraction(video.get('sample_aspect_ratio', '1:1').replace(':','/')) if video.get('sample_aspect_ratio') not in (None,'N/A','0:1') else Fraction(1)
        if (not math.isfinite(start+video_start) or not 0 < width <= 16384 or not 0 < height <= 16384 or not 0 < width*sar <= 16384 or
                (complete and (not math.isfinite(duration) or not 0 < duration < 7*86400))):
            raise ValueError
        if video.get('codec_name') not in ('h264', 'hevc'):
            raise PlaybackError('video-codec-unsupported')
        return {'duration': duration, 'container': data['format']['format_name'],
            'video': video['codec_name'], 'audio': audio.get('codec_name', '') if audio else '',
            'width': width, 'height': height, 'source_start': start,
            'frame_rate': video.get('avg_frame_rate', '0/0'),
            'display_width': round(width*sar),
            'video_start_offset': video_start-start, 'warnings': bool(warnings),
            'finalization': 'unconfirmed'}
    except (KeyError, ValueError, TypeError, StopIteration, ZeroDivisionError):
        raise PlaybackError('media-invalid' if complete else 'prefix-insufficient') from None


def probe_prefix(path, settings, cancel, size):
    """Finite snapshot, finite work; no global duration requirement or backend guess."""
    with path.open('rb') as source:
        data = source.read(min(size, PREFIX_STEPS[-1]))
    if data.lstrip(b'\xef\xbb\xbf \r\n\t').startswith((b'<', b'{', b'[')):
        raise PlaybackError('invalid-media-response')
    if len(data) < PREFIX_STEPS[0]:
        return PrefixResult(reason='prefix-insufficient', retry=True)
    is_mp4 = data[4:8] in (b'ftyp', b'moov', b'free', b'wide')
    if is_mp4:
        reason = mp4_prefix(data)
        if reason:
            return PrefixResult(reason=reason, retry=reason == 'prefix-insufficient')
    try:
        output, warnings = run([settings.ffprobe, '-v', 'warning', '-protocol_whitelist', 'file,pipe',
            '-probesize', str(len(data)), '-analyzeduration', '2000000',
            '-show_entries', 'format=format_name,start_time:stream=codec_type,codec_name,start_time,width,height',
            '-of', 'json', 'pipe:0'], cancel, timeout=8, input_chunks=(data,), allow_early_input_close=True)
        info = media_info(json.loads(output), warnings, complete=False)
    except PlaybackError as exc:
        if exc.code in ('media-invalid', 'media-timeout', 'prefix-insufficient'):
            return PrefixResult(reason='prefix-insufficient', retry=True)
        raise
    except (ValueError, TypeError):
        return PrefixResult(reason='prefix-insufficient', retry=True)
    formats = set(info['container'].split(','))
    if not formats.intersection(('mpeg', 'mpegts', 'mov', 'mp4')):
        return PrefixResult(reason='container-requires-complete-file')
    if formats.intersection(('mov', 'mp4')) and not is_mp4:
        return PrefixResult(reason='mp4-layout-unsupported')
    return PrefixResult(media=info)


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    duration: float
    archive_key: str
    discontinuity: bool = False
    first_pts: float = None


def probe(path, settings, cancel):
    output, warnings = run([settings.ffprobe, '-v', 'warning', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=format_name,start_time,duration,size:stream=codec_type,codec_name,start_time,width,height,sample_rate,channels,avg_frame_rate,sample_aspect_ratio',
        '-of', 'json', str(path)], cancel, timeout=45)
    try:
        return media_info(json.loads(output), warnings)
    except (ValueError, TypeError):
        raise PlaybackError('media-invalid') from None


def read_segments(directory, recording, timing=False, video_offset=0.):
    playlist = directory/'source.m3u8'
    if not playlist.exists():
        return (), False
    try:
        lines = playlist.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return (), False  # Windows can momentarily hold a renamed playlist.
    if not lines or lines[0] != '#EXTM3U' or len(lines) > 200000:
        raise PlaybackError('playlist-invalid')
    duration, elapsed, result, origin = None, 0., [], None
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
            first = segment_pts(path) if timing else None
            if first is not None:
                if origin is None:
                    origin = first
                delta = first-origin
                delta += round((elapsed-delta)/(WRAP/CLOCK))*(WRAP/CLOCK)
                start = recording.start+video_offset+delta
            else:
                start = recording.start+elapsed
            result.append(Segment(path, start, duration, recording.key, first_pts=first))
            elapsed += duration
            duration = None
    return tuple(result), '#EXT-X-ENDLIST' in lines


def hls_command(source, directory, media, settings):
    audio = ['-c:a', 'copy'] if media['audio'] == 'aac' else ['-c:a', 'aac', '-b:a', '64k']
    analysis = ['-probesize', '1048576', '-analyzeduration', '2000000'] if source == 'pipe:0' else []
    return [settings.ffmpeg, '-nostdin', '-hide_banner', '-v', 'warning', '-y',
        '-protocol_whitelist', 'file,pipe', *analysis, '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'copy', *audio, '-avoid_negative_ts', 'make_zero',
        '-f', 'hls', '-hls_time', '4', '-hls_list_size', '0', '-hls_playlist_type', 'event',
        # temp_file publishes completed segments; do not claim independent_segments
        # without inspecting actual keyframes and codec parameter availability.
        '-hls_flags', 'temp_file', '-hls_segment_filename', str(directory/'segment-%05d.ts'),
        str(directory/'source.m3u8')]


def prepare(source, directory, recording, media, settings, cancel, publish, budget, input_chunks=None):
    # This producer exclusively owns this derivative; never consume a stale
    # playlist left by an interrupted preparation. Originals are untouched.
    (directory/'source.m3u8').unlink(missing_ok=True)
    previous = [0]
    def tick():
        budget()
        segments, complete = read_segments(directory, recording, timing=True,
                                            video_offset=media.get('video_start_offset', 0.))
        if len(segments) != previous[0]:
            previous[0] = len(segments)
            publish(segments, False)
    _, warnings = run(hls_command(source, directory, media, settings), cancel, timeout=1800, tick=tick,
                      input_chunks=input_chunks)
    check_cancel(cancel)
    segments, complete = read_segments(directory, recording, timing=True,
                                        video_offset=media.get('video_start_offset', 0.))
    if not segments or not complete:
        raise PlaybackError('preparation-incomplete')
    publish(segments, True)
    return dict(media, prepared_duration=sum(s.duration for s in segments),
                preparation_warnings=bool(warnings))


def growing_chunks(path, finished, failed, cancel):
    """Read received bytes without treating temporary EOF as media EOF."""
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


def packet_bounds(path, settings, cancel, stream='v:0'):
    """Measure preserved video packet timestamps; do not infer a keyframe cut."""
    output, _ = run([settings.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-select_streams', stream, '-show_packets', '-show_entries',
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
