"""Local speech-to-text powered by faster-whisper.

The sound is decoded here, with ffmpeg from imageio-ffmpeg, into the 16 kHz
mono samples the model works on, instead of by faster-whisper's own reader
(PyAV): that reader breaks when a new PyAV release changes what it accepts,
and the latest faster-whisper (1.2.1) fails on the latest PyAV (19).
"""
import logging
import os
import subprocess
import tempfile
import threading

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DECODE_TIMEOUT = 300
_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel('tiny', device='cpu', compute_type='int8')
    return _model


def samples(path, max_seconds=None):
    """The sound in an audio or video file as 16 kHz mono float32 samples (a
    numpy array), from the start up to max_seconds when given."""
    import imageio_ffmpeg
    import numpy as np
    args = [imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-i', path, '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE)]
    if max_seconds:
        args += ['-t', str(max_seconds)]
    run = subprocess.run(args + ['-f', 's16le', 'pipe:1'], capture_output=True, timeout=DECODE_TIMEOUT)
    if run.returncode != 0 and not run.stdout:
        logger.info('ffmpeg could not decode sound: %s', run.stderr.decode('utf-8', 'replace')[-300:])
        raise RuntimeError('no sound could be read from the file')
    return np.frombuffer(run.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(filename, file_bytes):
    try:
        model = _get_model()
        suffix = os.path.splitext(filename or '')[1] or '.webm'
        path = None
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            path = handle.name
            handle.write(file_bytes)
        try:
            segments, _ = model.transcribe(samples(path), beam_size=1, vad_filter=True)
            return ' '.join(segment.text.strip() for segment in segments).strip()
        finally:
            if path and os.path.exists(path):
                os.unlink(path)
    except Exception as exc:
        raise RuntimeError(f"Could not transcribe that audio: {exc}")


def timed_segments(path, max_seconds=None):
    """(start, end, text) for each stretch of speech in an audio or video
    file, times in seconds. Errors are left to the caller."""
    audio = samples(path, max_seconds)
    if not audio.size:
        return []
    segments, _ = _get_model().transcribe(audio, beam_size=1, vad_filter=True)
    return [(segment.start, segment.end, segment.text.strip()) for segment in segments if segment.text.strip()]
