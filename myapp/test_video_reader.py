"""A video attached in the chat is read as one picture of its frames, in
order with their times, and a text of what is said and shown in it
(myapp.video_reader); that pair goes to a model that reads pictures, never
to picture editing."""
import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from PIL import Image

from myapp import audio_transcribe, image_sheet, video_reader, views
from myapp.models import AIMessage, StoreProfile

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = None


def make_clip(colours=(), seconds_each=1, sound_seconds=0, extension='mp4'):
    """A small clip of solid colours, one after another, with a tone under it."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, f'clip.{extension}')
        subprocess.run(_clip_args(colours, seconds_each, sound_seconds) + [path], capture_output=True, check=True)
        with open(path, 'rb') as clip:
            return clip.read()


def _clip_args(colours, seconds_each, sound_seconds):
    args = [FFMPEG, '-v', 'error', '-y']
    for colour in colours:
        args += ['-f', 'lavfi', '-i', f'color=c={colour}:s=160x120:r=10:d={seconds_each}']
    if sound_seconds:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={sound_seconds}']
    if colours:
        streams = ''.join(f'[{i}:v]' for i in range(len(colours)))
        args += ['-filter_complex', f'{streams}concat=n={len(colours)}:v=1:a=0[v]', '-map', '[v]',
                 '-c:v', 'libx264', '-pix_fmt', 'yuv420p']
    if sound_seconds:
        args += ['-map', f'{len(colours)}:a', '-c:a', 'aac']
    return args


def jpeg_uri(colour='red'):
    out = io.BytesIO()
    Image.new('RGB', (64, 48), colour).save(out, 'JPEG')
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()


class TextTests(SimpleTestCase):
    def test_frames_are_spread_over_the_video(self):
        self.assertEqual(video_reader.frame_times(2), [0.5, 1.5])
        self.assertEqual(video_reader.frame_times(0.3), [0.15])
        self.assertEqual(len(video_reader.frame_times(9)), 9)
        long_one = video_reader.frame_times(300)
        self.assertEqual(len(long_one), video_reader.MAX_FRAMES)
        self.assertLess(long_one[0], 20)
        self.assertGreater(long_one[-1], 280)

    def test_clock(self):
        self.assertEqual([video_reader.clock(s) for s in (0, 5.9, 75, 3725)], ['0:00', '0:05', '1:15', '1:02:05'])

    def test_the_text_says_what_was_said_and_shown(self):
        text = video_reader.describe(
            'trip.mp4', 9.2, 9, True, [(0.0, 2.1, 'Hello everyone.'), (65.4, 70, 'Bye!')],
            [(0.5, 'CAT'), (1.5, 'CAT'), (3.5, 'DOG'), (4.5, ''), (6.5, 'CAT')],
        )
        self.assertTrue(text.startswith('[Video attached: trip.mp4 · 0:09 long]'))
        self.assertIn('9 still frames', text)
        self.assertIn('never mention frames', text)     # the person never hears how the video was read
        self.assertIn('What is said in the video:\n[0:00] Hello everyone.\n[1:05] Bye!', text)
        # The same words on frames in a row are listed once.
        self.assertIn('Words shown on screen:\n[0:00] CAT\n[0:03] DOG\n[0:06] CAT', text)
        self.assertTrue(video_reader.is_video_text(text))
        self.assertTrue(video_reader.is_video_text(f'=== File 1 of 2: trip.mp4 ===\n{text}'))
        self.assertFalse(video_reader.is_video_text('An ordinary [Video attached: x] mention mid-line'))

    def test_the_text_for_each_kind_of_sound(self):
        cases = {
            (False, None): 'The video has no sound.',
            (True, None): "The sound in the video couldn't be written down.",
            (True, ()): 'No speech was heard in the video',
        }
        for (has_audio, spoken), expected in cases.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, video_reader.describe('a.mp4', 4, 4, has_audio, spoken, []))
        sound_only = video_reader.describe('voice.m4a', 4, 0, True, [(0, 1, 'Hi')], [])
        self.assertIn('This file has sound only, no pictures.', sound_only)
        self.assertNotIn('still frame', sound_only)
        long_talk = video_reader.describe('talk.mp4', 3600, 9, True, [(0, 1, 'Hi')], [])
        self.assertIn('Only the first 10 minutes of speech were written down.', long_talk)

    def test_the_name_cannot_break_the_heading(self):
        text = video_reader.describe('a]b\n[c.mp4', 3, 3, False, None, [])
        self.assertTrue(text.startswith('[Video attached: a b c.mp4 · 0:03 long]'), text.splitlines()[0])

    def test_the_storyboard_keeps_order_and_shape(self):
        frames = [Image.new('RGB', (320, 180), colour) for colour in ('red', 'lime', 'blue', 'white')]
        sheet = image_sheet.storyboard(frames, ['0:00', '0:01', '0:02', '0:03'])
        picture = Image.open(io.BytesIO(base64.b64decode(sheet.split(',', 1)[1])))
        cell_w, cell_h, gap = image_sheet.FRAME_EDGE, image_sheet.FRAME_EDGE * 180 // 320, image_sheet.GAP
        self.assertEqual(picture.size, (3 * cell_w + 4 * gap, 2 * cell_h + 3 * gap))
        centre = lambda column, row: picture.getpixel((gap + column * (cell_w + gap) + cell_w // 2,
                                                       gap + row * (cell_h + gap) + cell_h * 3 // 4))
        self.assertGreater(centre(0, 0)[0], 200)        # red, then lime, then blue, then white below
        self.assertGreater(centre(1, 0)[1], 200)
        self.assertGreater(centre(2, 0)[2], 200)
        self.assertGreater(min(centre(0, 1)), 200)
        self.assertEqual(image_sheet.storyboard([], []), '')


@unittest.skipUnless(FFMPEG, 'needs imageio-ffmpeg')
class ReadTests(SimpleTestCase):
    def setUp(self):
        self.heard = []

        def fake_speech(path, max_seconds):
            self.heard.append((os.path.getsize(path), max_seconds))
            return [(0.0, 1.4, 'Hello there.')]

        speech = patch.object(video_reader.audio_transcribe, 'timed_segments', side_effect=fake_speech)
        words = patch.object(video_reader.image_ocr, 'extract_data_uri', side_effect=self.screen_words)
        speech.start()
        words.start()
        self.addCleanup(speech.stop)
        self.addCleanup(words.stop)

    @staticmethod
    def screen_words(data_uri):
        frame = Image.open(io.BytesIO(base64.b64decode(data_uri.split(',', 1)[1]))).convert('RGB')
        r, g, b = frame.getpixel((frame.width // 2, frame.height // 2))
        return 'RED' if r > max(g, b) else 'GREEN' if g > max(r, b) else 'BLUE'

    def read(self, data, name):
        return video_reader.read(SimpleUploadedFile(name, data))

    def test_a_video_with_sound_is_read_in_order(self):
        clip = make_clip(colours=('red', 'green', 'blue'), sound_seconds=3)
        result = self.read(clip, 'clip.mp4')
        self.assertEqual(result['frames'], 3)
        self.assertTrue(result['image'].startswith('data:image/jpeg;base64,'))
        self.assertEqual(self.heard, [(len(clip), video_reader.MAX_SPEECH_SECONDS)])
        text = result['text']
        self.assertTrue(text.startswith('[Video attached: clip.mp4 · 0:03 long]'), text)
        self.assertIn('[0:00] Hello there.', text)
        self.assertIn('Words shown on screen:\n[0:00] RED\n[0:01] GREEN\n[0:02] BLUE', text)
        picture = Image.open(io.BytesIO(base64.b64decode(result['image'].split(',', 1)[1]))).convert('RGB')
        cell, gap = image_sheet.FRAME_EDGE, image_sheet.GAP
        cell_h = cell * 120 // 160
        colours = [picture.getpixel((gap + i * (cell + gap) + cell // 2, gap + cell_h * 3 // 4)) for i in range(3)]
        self.assertEqual([max(range(3), key=lambda c: rgb[c]) for rgb in colours], [0, 1, 2])
        # What the person sees is one plain frame from the middle (green), not the frames.
        poster = Image.open(io.BytesIO(base64.b64decode(result['poster'].split(',', 1)[1]))).convert('RGB')
        self.assertLessEqual(max(poster.size), video_reader.POSTER_EDGE)
        self.assertEqual(poster.width * 120, poster.height * 160)
        rgb = poster.getpixel((poster.width // 2, poster.height // 2))
        self.assertEqual(max(range(3), key=lambda c: rgb[c]), 1)

    def test_a_silent_video(self):
        result = self.read(make_clip(colours=('red', 'blue')), 'silent.mp4')
        self.assertEqual(result['frames'], 2)
        self.assertIn('The video has no sound.', result['text'])
        self.assertEqual(self.heard, [])

    def test_a_sound_only_file(self):
        result = self.read(make_clip(sound_seconds=2, extension='m4a'), 'voice.m4a')
        self.assertEqual((result['frames'], result['image'], result['poster']), (0, '', ''))
        self.assertIn('This file has sound only, no pictures.', result['text'])
        self.assertIn('[0:00] Hello there.', result['text'])

    def test_a_file_that_is_not_a_video(self):
        with self.assertRaisesMessage(video_reader.VideoError, "couldn't be read as a video"):
            self.read(b'just some text, not a video', 'notes.mp4')


@unittest.skipUnless(FFMPEG and importlib.util.find_spec('numpy'), 'needs imageio-ffmpeg and numpy')
class SoundTests(SimpleTestCase):
    """The speech model gets its sound decoded by ffmpeg, never by PyAV."""

    def clip_path(self, data):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, 'clip.mp4')
        with open(path, 'wb') as clip:
            clip.write(data)
        return path

    def test_the_sound_of_a_video_as_samples(self):
        path = self.clip_path(make_clip(colours=('red',), seconds_each=3, sound_seconds=3))
        audio = audio_transcribe.samples(path)
        self.assertEqual(str(audio.dtype), 'float32')
        self.assertAlmostEqual(audio.size / audio_transcribe.SAMPLE_RATE, 3, delta=0.2)
        self.assertGreater(float(abs(audio).max()), 0.1)        # the tone, not silence
        self.assertAlmostEqual(audio_transcribe.samples(path, 1).size / audio_transcribe.SAMPLE_RATE, 1, delta=0.1)

    def test_the_model_is_given_samples(self):
        path = self.clip_path(make_clip(colours=('red',), seconds_each=2, sound_seconds=2))
        segment = type('Segment', (), {'start': 0.0, 'end': 1.0, 'text': ' Hi. '})

        class Model:
            def transcribe(self, audio, **kwargs):
                self.audio = audio
                return iter([segment]), None

        model = Model()
        with patch.object(audio_transcribe, '_get_model', return_value=model):
            self.assertEqual(audio_transcribe.timed_segments(path), [(0.0, 1.0, 'Hi.')])
            with open(path, 'rb') as clip:
                self.assertEqual(audio_transcribe.transcribe('clip.mp4', clip.read()), 'Hi.')
        self.assertEqual(str(model.audio.dtype), 'float32')

    def test_a_file_without_sound(self):
        path = self.clip_path(make_clip(colours=('red',)))
        with self.assertRaises(RuntimeError):
            audio_transcribe.samples(path)


class EndpointTests(TestCase):
    url = '/AI/api/read-video/'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post(self, data=b'video bytes', name='clip.mp4'):
        return self.client.post(self.url, {'file': SimpleUploadedFile(name, data)})

    def test_it_returns_the_picture_and_text(self):
        result = {'text': '[Video attached: clip.mp4 · 0:03 long]', 'image': 'data:image/jpeg;base64,AAAA',
                  'poster': 'data:image/jpeg;base64,BBBB', 'seconds': 3, 'frames': 3}
        with patch.object(video_reader, 'read', return_value=result) as read:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok', 'filename': 'clip.mp4', 'text': result['text'],
                                           'image': result['image'], 'poster': result['poster']})
        self.assertEqual(read.call_args.args[0].name, 'clip.mp4')

    def test_problems_are_told_plainly(self):
        with patch.object(video_reader, 'read', side_effect=video_reader.VideoError('That video couldn\'t be played.')):
            response = self.post()
        self.assertEqual((response.status_code, response.json()['detail']), (400, "That video couldn't be played."))
        with patch.object(video_reader, 'read', side_effect=RuntimeError('boom')):
            response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('boom', response.json()['detail'])
        with patch.object(video_reader, 'MAX_BYTES', 4):
            response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertIn('too large', response.json()['detail'])
        self.assertEqual(self.client.post(self.url).json()['detail'], 'No video provided.')
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_too_many_videos_are_slowed_down(self):
        with patch.object(views, 'AI_VIDEO_RATE_LIMIT', 1), \
                patch.object(video_reader, 'read', return_value={'text': 't', 'image': '', 'poster': '', 'seconds': 1, 'frames': 0}):
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post().status_code, 429)


class SendTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        user = User.objects.create_user('video@example.com', 'video@example.com', 'pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=user, defaults={'ai_onboarded': True})
        self.client.force_login(user)

    def send(self, calls, **payload):
        def fake_stream(history, **kwargs):
            calls.append((history, kwargs))
            return iter(['A cat waves and says hello.'])

        with patch('myapp.views.ai_chat.stream_chat', side_effect=fake_stream), \
                patch('myapp.views.image_ocr.extract_data_uri', return_value='SHOULD NOT BE READ') as ocr:
            response = self.client.post('/AI/api/send/', data=json.dumps(payload), content_type='application/json')
            reply = b''.join(response.streaming_content).decode()
        ocr.assert_not_called()
        return reply

    def test_a_video_goes_to_a_model_that_reads_pictures(self):
        frames, poster = jpeg_uri('red'), jpeg_uri('blue')
        video_text = video_reader.describe('clip.mp4', 3, 3, True, [(0, 1, 'Hello there.')], [(0.5, 'CAT')])
        calls = []
        reply = self.send(
            # An edit-shaped message must not send the frames to picture editing.
            calls, message='remove the background music', model='quick', image=frames, video_poster=poster,
            document_name='clip.mp4', document_text=video_text, document_mode='video',
        )
        self.assertIn('A cat waves', reply)
        history, kwargs = calls[0]
        self.assertEqual(kwargs['model_key'], 'vision')
        last = history[-1]['content']
        self.assertEqual(last[1], {'type': 'image_url', 'image_url': {'url': frames}})
        self.assertIn('[0:00] Hello there.', last[0]['text'])
        self.assertIn('remove the background music', last[0]['text'])
        # Kept as the one frame people see, with the frames beside it for the model only.
        saved = AIMessage.objects.get(role=AIMessage.ROLE_USER)
        self.assertEqual((saved.image_data, saved.video_frames, saved.document_name, saved.document_text),
                         (poster, frames, 'clip.mp4', video_text))

        # A reloaded chat shows the frame as the video; the frames never leave the server.
        page = self.client.get(f'/AI/api/conversations/{saved.conversation_id}/')
        sent = page.json()['messages'][0]
        self.assertEqual((sent['image_data'], sent.get('sent_video')), (poster, True))
        self.assertNotIn(frames, page.content.decode())

        # Retry sends the frames to the model again and keeps the two apart.
        calls.clear()
        self.send(calls, message='what does the person say?', model='quick', conversation_id=saved.conversation_id,
                  replace_message_id=saved.pk)
        self.assertEqual(calls[0][0][-1]['content'][1]['image_url']['url'], frames)
        again = AIMessage.objects.filter(role=AIMessage.ROLE_USER, superseded=False).get()
        self.assertEqual((again.image_data, again.video_frames), (poster, frames))

    def test_an_ordinary_picture_is_kept_as_it_is(self):
        picture = jpeg_uri('red')
        with patch('myapp.views.ai_chat.stream_chat', return_value=iter(['A red square.'])), \
                patch('myapp.views.image_ocr.extract_data_uri', return_value=''):
            response = self.client.post('/AI/api/send/', data=json.dumps({
                'message': 'what colour is this?', 'model': 'quick', 'image': picture, 'video_poster': jpeg_uri('blue'),
            }), content_type='application/json')
            b''.join(response.streaming_content)
        saved = AIMessage.objects.get(role=AIMessage.ROLE_USER)
        self.assertEqual((saved.image_data, saved.video_frames), (picture, ''))
