"""File conversions added to "Convert a file": PowerPoint, web pages, JSON,
PDF pages as pictures, and more picture formats. Each test converts a real
file and opens the result, so a conversion that "succeeds" with a broken
file fails here."""
import io
import json
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from PIL import Image

from myapp import file_convert


def _picture(fmt='PNG', mode='RGBA', size=(64, 40)):
    out = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 128) if mode == 'RGBA' else (200, 30, 30)).save(out, fmt)
    return out.getvalue()


def _pptx(slides):
    from pptx import Presentation
    deck = Presentation()
    for title, bullets in slides:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text_frame.text = bullets[0]
        for bullet in bullets[1:]:
            slide.placeholders[1].text_frame.add_paragraph().text = bullet
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


class ConversionMapTests(SimpleTestCase):
    def test_every_listed_conversion_is_offered_to_the_page(self):
        mapping = file_convert.conversion_map()
        for source in ('pptx', 'html', 'json', 'gif', 'tiff', 'ico'):
            self.assertIn(source, mapping)
        self.assertIn('png', mapping['pdf'])
        self.assertNotIn('png', mapping['png'])        # never "convert to itself"
        self.assertNotIn('jpg', mapping['jpeg'])


class PictureTests(SimpleTestCase):
    def test_a_transparent_png_becomes_every_picture_format(self):
        source = _picture()
        for target in ('jpg', 'webp', 'gif', 'bmp', 'tiff', 'ico'):
            payload, name, mime = file_convert.convert(source, 'logo.png', target)
            with Image.open(io.BytesIO(payload)) as opened:
                opened.load()
            self.assertEqual(name, f'logo.{target}')
            self.assertTrue(mime.startswith('image/'), target)

    def test_gif_and_tiff_are_accepted_as_sources(self):
        for fmt, ext in (('GIF', 'gif'), ('TIFF', 'tiff')):
            payload, name, _ = file_convert.convert(_picture(fmt, 'RGB'), f'scan.{ext}', 'png')
            self.assertTrue(payload.startswith(b'\x89PNG'))


class PdfPicturesTests(SimpleTestCase):
    def test_one_page_gives_one_picture_and_several_give_a_zip(self):
        one = file_convert.text_to_pdf_bytes('# Hello\n\nOne short page.', branded=False)
        payload, name, mime = file_convert.convert(one, 'note.pdf', 'png')
        self.assertTrue(payload.startswith(b'\x89PNG'))
        self.assertEqual(name, 'note.png')

        long_text = '# Long\n\n' + '\n\n'.join(f'Paragraph {i} ' + 'words ' * 60 for i in range(60))
        many = file_convert.text_to_pdf_bytes(long_text, branded=False)
        payload, name, mime = file_convert.convert(many, 'report.pdf', 'jpg')
        self.assertEqual((name, mime), ('report-pages.zip', 'application/zip'))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertGreater(len(names), 1)
            self.assertEqual(names[0], 'page-01.jpg')
            self.assertTrue(archive.read(names[0]).startswith(b'\xff\xd8'))

    def test_pdf_to_markdown(self):
        pdf = file_convert.text_to_pdf_bytes('# Invoice\n\nTotal 4999', branded=False)
        payload, name, _ = file_convert.convert(pdf, 'invoice.pdf', 'md')
        self.assertIn('4999', payload.decode())
        self.assertEqual(name, 'invoice.md')


class PdfPreviewTests(TestCase):
    def test_the_first_page_comes_back_as_a_picture_with_the_page_count(self):
        pdf = file_convert.text_to_pdf_bytes('# Hello\n\nA page.', branded=False)
        response = self.client.post('/AI/api/convert/preview/', {'file': SimpleUploadedFile('a.pdf', pdf)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['X-Page-Count'], '1')
        with Image.open(io.BytesIO(response.content)) as page:
            self.assertEqual(page.width, 520)

    def test_only_pdfs_are_previewed(self):
        response = self.client.post('/AI/api/convert/preview/', {'file': SimpleUploadedFile('a.png', _picture())})
        self.assertEqual(response.status_code, 400)


class PowerPointTests(SimpleTestCase):
    def test_slides_become_headings_and_bullets(self):
        deck = _pptx([('Plan', ['Research', 'Build']), ('Launch', ['Go live'])])
        payload, _, _ = file_convert.convert(deck, 'deck.pptx', 'md')
        text = payload.decode()
        self.assertIn('## Plan', text)
        self.assertIn('- Build', text)
        self.assertIn('## Launch', text)
        pdf, name, mime = file_convert.convert(deck, 'deck.pptx', 'pdf')
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_text_becomes_a_presentation(self):
        payload, name, mime = file_convert.convert(b'# Plan\n\n- one\n- two\n\n# Next\n\nMore', 'notes.md', 'pptx')
        from pptx import Presentation
        self.assertGreaterEqual(len(Presentation(io.BytesIO(payload)).slides), 2)
        self.assertEqual(name, 'notes.pptx')


class WebPageTests(SimpleTestCase):
    def test_a_web_page_keeps_its_text_and_drops_scripts(self):
        page = (b'<html><head><title>x</title><style>p{}</style></head><body><h1>Prices</h1>'
                b'<script>alert(1)</script><p>Plan A costs 499.</p><ul><li>Fast</li><li>Cheap</li></ul></body></html>')
        payload, _, _ = file_convert.convert(page, 'prices.html', 'md')
        text = payload.decode()
        self.assertIn('# Prices', text)
        self.assertIn('- Cheap', text)
        self.assertNotIn('alert', text)
        self.assertTrue(file_convert.convert(page, 'prices.html', 'pdf')[0].startswith(b'%PDF'))


class JsonTests(SimpleTestCase):
    def test_json_records_round_trip_through_a_spreadsheet(self):
        records = [{'name': 'Asha', 'city': 'Pune'}, {'name': 'Ravi', 'age': 30}]
        csv_bytes, _, _ = file_convert.convert(json.dumps(records).encode(), 'people.json', 'csv')
        self.assertEqual(csv_bytes.decode().splitlines()[0], 'name,city,age')
        back, _, _ = file_convert.convert(csv_bytes, 'people.csv', 'json')
        self.assertEqual(json.loads(back)[1], {'name': 'Ravi', 'city': '', 'age': '30'})
        xlsx, _, _ = file_convert.convert(json.dumps({'items': records}).encode(), 'people.json', 'xlsx')
        self.assertTrue(xlsx.startswith(b'PK'))

    def test_bad_json_is_a_plain_message(self):
        with self.assertRaisesMessage(file_convert.ConvertError, 'valid JSON'):
            file_convert.convert(b'{not json', 'broken.json', 'csv')
