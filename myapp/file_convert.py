"""Convert an uploaded file between document, spreadsheet and image formats.

Everything here runs locally with libraries already in requirements.txt — no
conversion service, no API key, no network call — so a conversion either works
offline or fails cleanly with a message the user can act on.

Scope is deliberately honest: these are **content** conversions, not
pixel-perfect layout ones. Turning a DOCX into a PDF without LibreOffice means
carrying the text, headings and lists across, not reproducing the original's
exact typography. The UI says so, because a conversion that silently drops a
letterhead is worse than one that told you it would.

``convert()`` is the single entry point; ``targets_for()`` says what a given
file can become, so the UI never offers a conversion that would then fail.
"""

import csv
import html.parser
import io
import json
import os
import re
import zipfile

MAX_PDF_PAGES = 100
# Matches doc_extract.MAX_CSV_ROWS in spirit — a spreadsheet big enough to
# exceed this is a database export, not something to hand back through a chat
# attachment, and materialising it would blow the request's memory budget.
MAX_SHEET_ROWS = 20_000
# A PDF turned into pictures: one image per page, zipped when there are
# several. Past this many pages the zip gets too big to hand back in one go.
MAX_PDF_IMAGE_PAGES = 30
PDF_IMAGE_DPI = 150


class ConvertError(Exception):
    """A conversion that cannot be completed, with a user-facing reason."""


# Source extension -> what it can be turned into. Kept explicit rather than
# computed so the UI's dropdown and the server's validation can never disagree.
_IMAGE_TARGETS = ('pdf', 'png', 'jpg', 'webp', 'gif', 'bmp', 'tiff', 'ico')


def _image_targets(source):
    same = {'jpeg': 'jpg', 'tif': 'tiff'}.get(source, source)
    return tuple(target for target in _IMAGE_TARGETS if target != same)


_CONVERSIONS = {
    'pdf': ('docx', 'txt', 'md', 'png', 'jpg'),
    'docx': ('pdf', 'txt', 'md', 'pptx'),
    'pptx': ('pdf', 'docx', 'txt', 'md'),
    'txt': ('pdf', 'docx', 'md', 'pptx'),
    'md': ('pdf', 'docx', 'txt', 'pptx'),
    'html': ('pdf', 'docx', 'txt', 'md'),
    'htm': ('pdf', 'docx', 'txt', 'md'),
    'csv': ('xlsx', 'pdf', 'json'),
    'xlsx': ('csv', 'pdf', 'json'),
    'json': ('csv', 'xlsx'),
    **{source: _image_targets(source) for source in ('png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif', 'tiff', 'tif', 'ico')},
}
_IMAGE_SOURCES = ('png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif', 'tiff', 'tif', 'ico')
_TEXT_TARGETS = ('txt', 'md')

_MIME_TYPES = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'csv': 'text/csv; charset=utf-8',
    'txt': 'text/plain; charset=utf-8',
    'md': 'text/markdown; charset=utf-8',
    'json': 'application/json',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'webp': 'image/webp',
    'gif': 'image/gif',
    'bmp': 'image/bmp',
    'tiff': 'image/tiff',
    'ico': 'image/x-icon',
    'zip': 'application/zip',
}


def _extension(filename):
    return (os.path.splitext(filename or '')[1] or '').lstrip('.').lower()


def targets_for(filename):
    """Formats ``filename`` can be converted into (possibly empty)."""
    return list(_CONVERSIONS.get(_extension(filename), ()))


def conversion_map():
    """Every source extension and what it can become, for the page's buttons."""
    return {source: list(targets) for source, targets in _CONVERSIONS.items()}


def mime_for(extension):
    return _MIME_TYPES.get((extension or '').lower(), 'application/octet-stream')


# ── source readers: every supported input becomes either text or table rows ──

def _pdf_to_text(data):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ConvertError("That PDF couldn't be read — it may be corrupt.") from exc
    if getattr(reader, 'is_encrypted', False):
        raise ConvertError('That PDF is password-protected — remove the password and try again.')

    pages = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or '')
        except Exception:
            continue
    text = '\n\n'.join(part for part in pages if part.strip())
    if not text.strip():
        # A scanned PDF is images with no text layer. Say that plainly rather
        # than handing back an empty document that looks like a silent success.
        raise ConvertError(
            "That PDF has no selectable text — it looks like a scan. "
            'Convert it to an image, or upload it in chat so it can be read with OCR.'
        )
    return text


def _docx_to_text(data):
    from docx import Document as DocxDocument

    try:
        document = DocxDocument(io.BytesIO(data))
    except Exception as exc:
        raise ConvertError("That Word file couldn't be read — it may be corrupt.") from exc

    lines = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or '').strip()
        if not text:
            lines.append('')
            continue
        # Carry Word's heading levels across as Markdown so the PDF/text
        # writers below reproduce the document's structure instead of
        # flattening everything into identical paragraphs.
        style = (getattr(paragraph.style, 'name', '') or '').lower()
        heading = re.match(r'heading (\d)', style)
        if heading:
            lines.append('#' * min(int(heading.group(1)), 6) + ' ' + text)
        elif style.startswith('list'):
            lines.append('- ' + text)
        else:
            lines.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [(cell.text or '').strip() for cell in row.cells]
            if any(cells):
                lines.append(' | '.join(cells))
    return '\n'.join(lines).strip()


def _pptx_to_text(data):
    """Each slide's title as a heading, then the text on it."""
    from pptx import Presentation

    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise ConvertError("That PowerPoint file couldn't be read — it may be corrupt.") from exc
    lines = []
    for number, slide in enumerate(deck.slides, start=1):
        title_shape = slide.shapes.title
        title = (title_shape.text or '').strip() if title_shape is not None else ''
        lines.append(f'## {title or f"Slide {number}"}')
        for shape in slide.shapes:
            if shape is title_shape or not getattr(shape, 'has_text_frame', False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = ''.join(run.text for run in paragraph.runs).strip()
                if text:
                    lines.append(('  ' * paragraph.level) + '- ' + text)
        lines.append('')
    text = '\n'.join(lines).strip()
    if not re.search(r'[^\s#\-]', re.sub(r'(?m)^## Slide \d+$', '', text)):
        raise ConvertError('That presentation has no text to convert — its slides may be pictures only.')
    return text


class _HtmlToMarkdown(html.parser.HTMLParser):
    """A web page's readable text, headings and list items kept as Markdown."""
    _SKIP = {'script', 'style', 'noscript', 'svg', 'head', 'template'}
    _BLOCKS = {'p', 'div', 'section', 'article', 'br', 'tr', 'table', 'ul', 'ol', 'blockquote', 'pre', 'header', 'footer'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skipping += 1
        elif re.fullmatch(r'h[1-6]', tag):
            self.parts.append('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag == 'li':
            self.parts.append('\n- ')
        elif tag in self._BLOCKS:
            self.parts.append('\n')
        elif tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif re.fullmatch(r'h[1-6]', tag) or tag in self._BLOCKS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(re.sub(r'\s+', ' ', data))


def _html_to_text(data):
    parser = _HtmlToMarkdown()
    parser.feed(_decode_text(data))
    lines = [line.strip() for line in ''.join(parser.parts).splitlines()]
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    if not text:
        raise ConvertError('That web page has no readable text to convert.')
    return text


def _json_to_rows(data):
    """A list of objects (or of lists) as table rows, first row = headers."""
    try:
        value = json.loads(_decode_text(data))
    except ValueError as exc:
        raise ConvertError("That JSON file couldn't be read — check it is valid JSON.") from exc
    if isinstance(value, dict):
        # {"items": [...]} - use the first list inside.
        value = next((item for item in value.values() if isinstance(item, list)), [value])
    if not isinstance(value, list) or not value:
        raise ConvertError('That JSON has no list of records to turn into a table.')
    if all(isinstance(item, dict) for item in value):
        headers = []
        for item in value:
            headers.extend(key for key in item if key not in headers)
        rows = [headers] + [[_cell(item.get(key)) for key in headers] for item in value]
    elif all(isinstance(item, list) for item in value):
        rows = [[_cell(cell) for cell in item] for item in value]
    else:
        rows = [['value']] + [[_cell(item)] for item in value]
    return rows[:MAX_SHEET_ROWS]


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def rows_to_json_bytes(rows):
    """Table rows (first row = headers) as a list of JSON objects."""
    rows = [list(row) for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        return b'[]'
    headers = [str(cell).strip() or f'column_{index + 1}' for index, cell in enumerate(rows[0])]
    records = [
        {header: (row[index] if index < len(row) else '') for index, header in enumerate(headers)}
        for row in rows[1:]
    ]
    return json.dumps(records, ensure_ascii=False, indent=2).encode('utf-8')


def _xlsx_to_rows(data):
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise ConvertError("That spreadsheet couldn't be read — it may be corrupt.") from exc
    sheet = workbook.active
    rows = []
    for row in sheet.iter_rows(values_only=True):
        rows.append(['' if value is None else str(value) for value in row])
        if len(rows) >= MAX_SHEET_ROWS:
            break
    workbook.close()
    return rows


def _csv_to_rows(data):
    text = _decode_text(data)
    return [row for row in csv.reader(io.StringIO(text))][:MAX_SHEET_ROWS]


def _decode_text(data):
    for encoding in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


# ── writers ──
# The document writers live in doc_pdf / doc_word / doc_sheet, on top of the
# shared Markdown parser in doc_blocks. They are imported on first use, the way
# doc_extract.py imports fitz — a missing optional library must degrade to a
# clear message, never an import-time crash that takes the whole site down.
# Conversions pass branded=False: it is the user's own file, not ours, so it
# gets typography and page numbers but no title band, date or brand name.

# A table PDF stops here: past it a spreadsheet is a data export, not a page to read.
MAX_PDF_TABLE_ROWS = 1000


def text_to_pdf_bytes(content, **options):
    """Render Markdown-ish text (headings, lists, tables, code, charts) as a designed PDF."""
    from myapp import doc_pdf
    return doc_pdf.render_pdf(content, **options)


def text_to_docx_bytes(content, **options):
    """Render Markdown-ish text as a real DOCX package."""
    try:
        from myapp import doc_word
    except ImportError as exc:
        raise ConvertError('Word generation is temporarily unavailable.') from exc
    return doc_word.render_docx(content, **options)


def rows_to_xlsx_bytes(rows, **options):
    """Write table rows as a real XLSX workbook, numbers stored as numbers."""
    try:
        from myapp import doc_sheet
    except ImportError as exc:
        raise ConvertError('Excel generation is temporarily unavailable.') from exc
    return doc_sheet.render_xlsx(rows, **options)


def rows_to_pdf_bytes(rows, **options):
    """Table rows (first row = headers) as a PDF table, wide ones in landscape."""
    from myapp import doc_pdf
    from myapp.doc_blocks import Block, escape_markdown

    rows = [list(row) for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        return doc_pdf.render_pdf('', **options)
    width = max(len(row) for row in rows)

    def cells(row):
        return [escape_markdown(str(cell)) for cell in row + [''] * (width - len(row))]

    body = [cells(row) for row in rows[1:]]
    blocks = [Block('table', header=cells(rows[0]), rows=body[:MAX_PDF_TABLE_ROWS], aligns=[''] * width)]
    if len(body) > MAX_PDF_TABLE_ROWS:
        blocks.append(Block(
            'para', text=f'Showing the first {MAX_PDF_TABLE_ROWS:,} of {len(body):,} rows.',
        ))
    return doc_pdf.render_pdf('', blocks=blocks, landscape=width > 6, **options)


def rows_to_csv_bytes(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def _image_to_pdf_bytes(data):
    """One image, scaled to fit a single page with the aspect ratio intact."""
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ConvertError("That image couldn't be read — it may be corrupt.") from exc
    # PDF has no alpha channel; flatten onto white so a transparent PNG
    # doesn't come out with a black background.
    if image.mode in ('RGBA', 'LA', 'P'):
        image = image.convert('RGBA')
        canvas = Image.new('RGB', image.size, (255, 255, 255))
        canvas.paste(image, mask=image.split()[-1])
        image = canvas
    else:
        image = image.convert('RGB')

    buffer = io.BytesIO()
    image.save(buffer, format='PDF', resolution=100.0)
    return buffer.getvalue()


def _image_to_image_bytes(data, target):
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ConvertError("That image couldn't be read — it may be corrupt.") from exc

    pillow_format = {
        'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP', 'gif': 'GIF', 'bmp': 'BMP', 'tiff': 'TIFF', 'ico': 'ICO',
    }[target]
    if pillow_format in ('JPEG', 'BMP') and image.mode in ('RGBA', 'LA', 'P'):
        # No transparency in these: flatten onto white rather than black.
        image = image.convert('RGBA')
        canvas = Image.new('RGB', image.size, (255, 255, 255))
        canvas.paste(image, mask=image.split()[-1])
        image = canvas
    elif pillow_format in ('JPEG', 'BMP'):
        image = image.convert('RGB')
    elif image.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P'):
        image = image.convert('RGBA')

    buffer = io.BytesIO()
    if pillow_format == 'ICO':
        # An icon holds small sizes only; the largest fits the picture's shorter side.
        largest = max(16, min(256, *image.size))
        sizes = [(size, size) for size in (16, 24, 32, 48, 64, 128, 256) if size <= largest]
        image.save(buffer, format='ICO', sizes=sizes)
    else:
        image.save(buffer, format=pillow_format)
    return buffer.getvalue()


def pdf_preview_png(data, width=520):
    """A small picture of a PDF's first page, for "Convert a file":
    (png bytes, page count). Raises ConvertError if it can't be read."""
    import fitz

    try:
        document = fitz.open(stream=data, filetype='pdf')
    except Exception as exc:
        raise ConvertError("That PDF couldn't be read — it may be corrupt.") from exc
    if document.needs_pass:
        raise ConvertError('That PDF is password-protected.')
    if document.page_count == 0:
        raise ConvertError('That PDF has no pages.')
    page = document[0]
    zoom = width / max(page.rect.width, 1)
    picture = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes('png')
    pages = document.page_count
    document.close()
    return picture, pages


def _pdf_to_images(data, target):
    """Each page as a picture: (bytes, is_zip). One page gives the image
    itself; several give a .zip of page-01.png, page-02.png, ..."""
    import fitz

    try:
        document = fitz.open(stream=data, filetype='pdf')
    except Exception as exc:
        raise ConvertError("That PDF couldn't be read — it may be corrupt.") from exc
    if document.needs_pass:
        raise ConvertError('That PDF is password-protected — remove the password and try again.')
    if document.page_count == 0:
        raise ConvertError('That PDF has no pages.')
    if document.page_count > MAX_PDF_IMAGE_PAGES:
        raise ConvertError(
            f'That PDF has {document.page_count} pages; pictures can be made for up to {MAX_PDF_IMAGE_PAGES}. '
            'Split it into smaller PDFs first.'
        )
    pictures = []
    for page in document:
        pixmap = page.get_pixmap(dpi=PDF_IMAGE_DPI, alpha=False)
        pictures.append(pixmap.tobytes('png' if target == 'png' else 'jpeg'))
    document.close()
    if len(pictures) == 1:
        return pictures[0], False
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for number, picture in enumerate(pictures, start=1):
            archive.writestr(f'page-{number:02d}.{target}', picture)
    return buffer.getvalue(), True


def convert(data, source_name, target):
    """Convert ``data`` (bytes of ``source_name``) into ``target``.

    Returns ``(bytes, filename, mimetype)``. Raises ConvertError with a
    user-facing message for anything unsupported or unreadable.
    """
    source = _extension(source_name)
    target = (target or '').lower().lstrip('.')
    target = {'jpeg': 'jpg', 'tif': 'tiff'}.get(target, target)
    if not data:
        raise ConvertError('That file is empty.')
    if source not in _CONVERSIONS:
        raise ConvertError(f'{source.upper() or "That file type"} files cannot be converted here.')
    if target not in _CONVERSIONS[source]:
        allowed = ', '.join(_CONVERSIONS[source]).upper()
        raise ConvertError(f'A {source.upper()} can only be converted to: {allowed}.')

    stem = os.path.splitext(os.path.basename(source_name or 'converted'))[0] or 'converted'
    stem = re.sub(r'[^A-Za-z0-9_.-]+', '-', stem).strip('.-')[:80] or 'converted'

    if source in _IMAGE_SOURCES:
        payload = _image_to_pdf_bytes(data) if target == 'pdf' else _image_to_image_bytes(data, target)
    elif source == 'pdf' and target in ('png', 'jpg'):
        payload, zipped = _pdf_to_images(data, target)
        if zipped:
            return payload, f'{stem}-pages.zip', mime_for('zip')
    elif source in ('csv', 'xlsx', 'json'):
        rows = {'csv': _csv_to_rows, 'xlsx': _xlsx_to_rows, 'json': _json_to_rows}[source](data)
        if target == 'xlsx':
            payload = rows_to_xlsx_bytes(rows, branded=False)
        elif target == 'csv':
            payload = rows_to_csv_bytes(rows)
        elif target == 'json':
            payload = rows_to_json_bytes(rows)
        else:
            payload = rows_to_pdf_bytes(rows, branded=False)
    else:
        if source == 'pdf':
            text = _pdf_to_text(data)
        elif source == 'docx':
            text = _docx_to_text(data)
        elif source == 'pptx':
            text = _pptx_to_text(data)
        elif source in ('html', 'htm'):
            text = _html_to_text(data)
        else:
            text = _decode_text(data)
        if target == 'pdf':
            payload = text_to_pdf_bytes(text, branded=False)
        elif target == 'docx':
            payload = text_to_docx_bytes(text, branded=False)
        elif target == 'pptx':
            from myapp import doc_slides
            payload = doc_slides.render_pptx(text)
        else:
            payload = text.encode('utf-8')

    return payload, f'{stem}.{target}', mime_for(target)
