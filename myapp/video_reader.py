"""A video attached in the chat, read as one picture and a text.

The chat models read pictures and text but not video, so a video is turned
into what they do read: up to MAX_FRAMES still frames spread evenly over it,
laid out in order on one picture with each frame's time on it
(image_sheet.storyboard), and a text with what is said in it, with times
(faster-whisper, via myapp.audio_transcribe), and the words shown on screen
in each frame (myapp.image_ocr). The page sends both with the next message,
the way a picture sent with a file travels. ffmpeg comes from imageio-ffmpeg.
Nothing is kept: the upload is read from a temporary file and dropped.
"""
import base64
import contextlib
import io
import logging
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from myapp import audio_transcribe, image_ocr, image_sheet

logger = logging.getLogger(__name__)

MAX_BYTES = 100 * 1024 * 1024    # keep in step with VIDEO_MAX_BYTES in ai.html
MAX_FRAMES = 9
FRAME_READ_EDGE = 1280           # frames are read this big so small words on screen still read
MAX_SPEECH_SECONDS = 10 * 60
MAX_SCREEN_WORDS_CHARS = 500     # per frame
POSTER_EDGE = 640                # the one frame people see of a video they sent
MAX_TEXT_CHARS = 60_000
FFMPEG_TIMEOUT = 120
# Each video keeps a processor busy for a while, so only two are read at once.
_slots = threading.BoundedSemaphore(2)
SLOT_WAIT_SECONDS = 60

# The text made here opens with this line, which is also how views tells a
# message with a video from one with an ordinary picture.
_HEADING_RE = re.compile(r'^\[Video attached: ', re.M)
_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)')
_STREAM_RE = re.compile(r'^\s*Stream #\S+.*?: (Video|Audio): (.*)$', re.M)
_DECODED_TIME_RE = re.compile(r'time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)')


class VideoError(Exception):
    """Why a video can't be read, worded for the person who sent it."""


def is_video_text(text):
    return bool(text) and bool(_HEADING_RE.search(text))


def read(uploaded):
    """{'text', 'image', 'poster', 'seconds', 'frames'} for an uploaded video
    file. 'image' is the picture of its frames, for the model only; 'poster'
    is one frame from the middle, which is what the person is shown. Both are
    '' for a file with sound only. Raises VideoError."""
    ffmpeg = _ffmpeg()
    if not _slots.acquire(timeout=SLOT_WAIT_SECONDS):
        raise VideoError('Other videos are being read right now — please try again in a minute.')
    try:
        with _local_copy(uploaded) as path:
            return _read(ffmpeg, path, uploaded.name)
    finally:
        _slots.release()


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.exception('ffmpeg is not available for reading videos')
        raise VideoError("Videos can't be read right now — please try again later.")


@contextlib.contextmanager
def _local_copy(uploaded):
    """A path to the upload: Django's own temporary file for a big one, else
    a copy written for the length of the read."""
    if hasattr(uploaded, 'temporary_file_path'):
        yield uploaded.temporary_file_path()
        return
    suffix = re.sub(r'[^\w.]', '', os.path.splitext(uploaded.name or '')[1])[:10] or '.mp4'
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        with handle:
            for chunk in uploaded.chunks():
                handle.write(chunk)
        yield handle.name
    finally:
        os.unlink(handle.name)


def _run(args, timeout=FFMPEG_TIMEOUT):
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning('ffmpeg took longer than %s seconds reading a video', timeout)
        return None


def _seconds(match):
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _probe(ffmpeg, path):
    """(length in seconds or None, has pictures, has sound)."""
    run = _run([ffmpeg, '-hide_banner', '-i', path], timeout=30)
    info = run.stderr.decode('utf-8', 'replace') if run else ''
    streams = _STREAM_RE.findall(info)
    # A music file's cover art shows up as a one-picture "video" stream.
    has_video = any(kind == 'Video' and 'attached pic' not in rest for kind, rest in streams)
    has_audio = any(kind == 'Audio' for kind, _ in streams)
    match = _DURATION_RE.search(info)
    return (_seconds(match) if match else None), has_video, has_audio


def _decoded_seconds(ffmpeg, path):
    """The length found by playing the file through, for one whose header
    doesn't say (a video recorded in a browser often doesn't)."""
    run = _run([ffmpeg, '-hide_banner', '-i', path, '-f', 'null', '-'])
    times = _DECODED_TIME_RE.findall(run.stderr.decode('utf-8', 'replace')) if run else []
    if not times:
        return None
    hours, minutes, seconds = times[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _frame(ffmpeg, path, at):
    scale = (
        f"scale='min({FRAME_READ_EDGE},iw)':'min({FRAME_READ_EDGE},ih)'"
        ':force_original_aspect_ratio=decrease'
    )
    run = _run([
        ffmpeg, '-v', 'error', '-ss', f'{at:.3f}', '-i', path, '-frames:v', '1', '-an',
        '-vf', scale, '-f', 'image2pipe', '-c:v', 'png', 'pipe:1',
    ])
    if not run or not run.stdout:
        return None
    try:
        with Image.open(io.BytesIO(run.stdout)) as opened:
            return opened.convert('RGB')
    except Exception:
        return None


def frame_times(seconds):
    """When to take frames: one a second for a short clip, up to MAX_FRAMES,
    each in the middle of its stretch of the video."""
    count = min(MAX_FRAMES, max(1, round(seconds)))
    return [seconds * (index + 0.5) / count for index in range(count)]


def _frames(ffmpeg, path, seconds):
    taken = [(at, _frame(ffmpeg, path, at)) for at in frame_times(seconds)]
    taken = [(at, frame) for at, frame in taken if frame is not None]
    if not taken:
        first = _frame(ffmpeg, path, 0)
        taken = [(0, first)] if first is not None else []
    return taken


def _speech(path):
    """[(start, end, text)] said in the first MAX_SPEECH_SECONDS, or None
    when the sound couldn't be written down."""
    try:
        return audio_transcribe.timed_segments(path, MAX_SPEECH_SECONDS)
    except Exception:
        logger.warning('The speech in a video could not be written down', exc_info=True)
        return None


def _jpeg_uri(frame, quality):
    out = io.BytesIO()
    frame.save(out, 'JPEG', quality=quality)
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()


def _poster(frame):
    small = frame.copy()
    small.thumbnail((POSTER_EDGE, POSTER_EDGE))
    return _jpeg_uri(small, 82)


def _screen_words(frame):
    words = image_ocr.extract_data_uri(_jpeg_uri(frame, 90))
    return ' / '.join(line.strip() for line in words.splitlines() if line.strip())[:MAX_SCREEN_WORDS_CHARS]


def clock(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes}:{seconds:02d}'


def _read(ffmpeg, path, name):
    seconds, has_video, has_audio = _probe(ffmpeg, path)
    if not has_video and not has_audio:
        raise VideoError("That file couldn't be read as a video — please try another one.")
    if seconds is None:
        seconds = _decoded_seconds(ffmpeg, path)
    if not seconds:
        raise VideoError("That video couldn't be played — please try another one.")
    # The speech is written down while the frames are taken and read.
    with ThreadPoolExecutor(max_workers=1) as pool:
        speech = pool.submit(_speech, path) if has_audio else None
        frames = _frames(ffmpeg, path, seconds) if has_video else []
        words = [(at, _screen_words(frame)) for at, frame in frames]
        spoken = speech.result() if speech else None
    if not frames and not has_audio:
        raise VideoError("No pictures could be taken from that video — please try another one.")
    labels = [clock(at) for at, _ in frames]
    image = image_sheet.storyboard([frame for _, frame in frames], labels)
    poster = _poster(frames[len(frames) // 2][1]) if image else ''
    text = describe(name, seconds, len(frames) if image else 0, has_audio, spoken, words)
    return {'text': text, 'image': image, 'poster': poster, 'seconds': seconds, 'frames': len(frames) if image else 0}


def describe(name, seconds, frame_count, has_audio, spoken, words):
    """The text sent with the frames. ``spoken`` is None when the sound
    couldn't be written down; ``words`` is [(time, words on screen)]."""
    name = re.sub(r'[\[\]\r\n]+', ' ', os.path.basename(name or '')).strip()[:120] or 'video'
    parts = [f'[Video attached: {name} · {clock(seconds)} long]']
    if frame_count:
        parts.append(
            f'The picture sent with this message shows {frame_count} still '
            f'frame{"s" if frame_count != 1 else ""} taken evenly from this video, in time order '
            '(left to right, then top to bottom), each with its time in its top-left corner. Together with '
            'the text below, they are the video: answer as someone who watched it, and if something '
            "can't be told from them, say so rather than guess. Talk about it only as a video: never "
            'mention frames, stills, a grid or collage of pictures, a transcript, screen text being read, '
            'or how the video was given to you.'
        )
    else:
        parts.append('This file has sound only, no pictures.')
    if not has_audio:
        parts.append('The video has no sound.')
    elif spoken is None:
        parts.append("The sound in the video couldn't be written down.")
    elif not spoken:
        parts.append('No speech was heard in the video (there may be music or other sounds).')
    else:
        lines = '\n'.join(f'[{clock(start)}] {text}' for start, _, text in spoken)
        if seconds > MAX_SPEECH_SECONDS:
            lines += f'\n(Only the first {MAX_SPEECH_SECONDS // 60} minutes of speech were written down.)'
        parts.append(f'What is said in the video:\n{lines}')
    shown, previous = [], ''
    for at, text in words:
        if text and text != previous:
            shown.append(f'[{clock(at)}] {text}')
        previous = text
    if shown:
        parts.append('Words shown on screen:\n' + '\n'.join(shown))
    return '\n\n'.join(parts)[:MAX_TEXT_CHARS]
