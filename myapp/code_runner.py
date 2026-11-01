"""Runs Java (and a few other compiled languages) for the chat's code editor.

The code never runs on this server: it is sent to a Judge0 service (the
"Judge0 CE" API on RapidAPI, whose free Basic plan is enough to start) that
compiles and runs it in its own sandbox. Python, HTML and JavaScript don't come
here at all; the page runs those in the visitor's browser.

The key and the on/off switch live on AI Models Management ("Code runner"
panel, see myapp.api_controls), like every other paid service.
"""
import base64
import re

import requests

from myapp import model_controls
from myapp.provider_keys import get_key

CONTROL_KEY = 'code-runner'
API_KEY_SETTING = 'JUDGE0_API_KEY'
HOST = 'judge0-ce.p.rapidapi.com'
SUBMIT_URL = f'https://{HOST}/submissions?base64_encoded=true&wait=true&fields=stdout,stderr,compile_output,message,status,time,memory'

# Judge0 CE language ids.
LANGUAGES = {
    'java': (62, 'Java'),
    'c': (50, 'C'),
    'cpp': (54, 'C++'),
    'csharp': (51, 'C#'),
    'go': (60, 'Go'),
    'kotlin': (78, 'Kotlin'),
    'php': (68, 'PHP'),
    'ruby': (72, 'Ruby'),
    'rust': (73, 'Rust'),
}
MAX_CODE_CHARS = 64_000
MAX_STDIN_CHARS = 16_000
MAX_OUTPUT_CHARS = 20_000
TIMEOUT_SECONDS = 30

_ACCEPTED, _COMPILE_ERROR, _TIME_LIMIT = 3, 6, 5


class RunError(Exception):
    """A plain-language reason the code could not be run."""


def is_ready():
    return bool(get_key(API_KEY_SETTING).strip()) and model_controls.is_enabled(CONTROL_KEY)


_PACKAGE_RE = re.compile(r'^[ \t]*package\s+[\w.]+\s*;[ \t]*$', re.M)
_PUBLIC_TYPE_RE = re.compile(r'^public\s+((?:final|abstract|sealed|strictfp)\s+)*(class|interface|enum|record)\b', re.M)
_CLASS_RE = re.compile(r'\b(?:class|record|enum)\s+([A-Za-z_]\w*)')
_MAIN_RE = re.compile(r'\bstatic\s+void\s+main\s*\(')
_LITERAL_RE = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')


def _blank_literals(code):
    """The code with comments and string/char literals turned into spaces
    (same length), so braces and words inside them are not counted."""
    return _LITERAL_RE.sub(lambda m: re.sub(r'[^\n]', ' ', m.group(0)), code)


def _body_end(text, open_at):
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _main_class(code):
    """The class holding main, as Outer.Inner when it is nested."""
    plain = _blank_literals(code)
    main_at = _MAIN_RE.search(plain)
    if not main_at:
        raise RunError('This Java code has no main method, so there is nothing to start. '
                       'Add  public static void main(String[] args) { … }  to a class and run it again.')
    chain = []
    for match in _CLASS_RE.finditer(plain, 0, main_at.start()):
        open_at = plain.find('{', match.end())
        if 0 <= open_at < main_at.start() < _body_end(plain, open_at):
            chain.append(match.group(1))
    if not chain:
        raise RunError("Couldn't find the class that holds the main method.")
    return '.'.join(chain)


def prepare_java(code):
    """Judge0 saves the source as Main.java and runs `java Main`. Programs
    written for a file of their own (`public class Calculator`, a `package`
    line) would not compile there, so drop the package line, make top-level
    types non-public, and add a small Main that starts the right class."""
    starter = _main_class(code)
    code = _PACKAGE_RE.sub('', code)
    # `java Main` starts a class that isn't public just as well.
    code = _PUBLIC_TYPE_RE.sub(lambda m: m.group(0)[len('public '):], code)
    if starter == 'Main':
        return code
    return code + f'\n\npublic class Main {{\n    public static void main(String[] args) throws Exception {{\n        {starter}.main(args);\n    }}\n}}\n'


def _b64(text):
    return base64.b64encode((text or '').encode('utf-8')).decode('ascii')


def _unb64(value):
    if not value:
        return ''
    try:
        return base64.b64decode(value).decode('utf-8', errors='replace')
    except (ValueError, TypeError):
        return ''


def _clip(text):
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + '\n… (output cut short)'


def _post(key, payload):
    return requests.post(
        SUBMIT_URL, json=payload, timeout=TIMEOUT_SECONDS,
        headers={'X-RapidAPI-Key': key, 'X-RapidAPI-Host': HOST, 'Content-Type': 'application/json'},
    )


def _explain_http(status):
    if status in (401, 403):
        return 'The code runner key was refused. An admin needs to check it under AI Models Management → Code runner.'
    if status == 429:
        return "Today's code runs are used up. Please try again later."
    if status >= 500:
        return 'The code runner is having trouble right now. Please try again in a minute.'
    return f'The code runner could not take this code (error {status}).'


def run(language, code, stdin=''):
    """Compile and run `code`. Returns a dict with ok, verdict, stdout, stderr,
    compile_output, time and memory; raises RunError with a message
    that can be shown to the user as it is."""
    if language not in LANGUAGES:
        raise RunError("This language can't be run here.")
    if not model_controls.is_enabled(CONTROL_KEY):
        raise RunError('Running code is switched off right now.')
    key = get_key(API_KEY_SETTING).strip()
    if not key:
        raise RunError(f"Running {LANGUAGES[language][1]} isn't set up yet. An admin can switch it on under "
                       'AI Models Management → Code runner.')
    code = code or ''
    if not code.strip():
        raise RunError('There is no code to run.')
    if len(code) > MAX_CODE_CHARS:
        raise RunError('This code is too long to run here (64,000 characters at most).')
    if len(stdin or '') > MAX_STDIN_CHARS:
        raise RunError('The input is too long (16,000 characters at most).')
    if language == 'java':
        code = prepare_java(code)

    model_controls.record_request(CONTROL_KEY)
    try:
        response = _post(key, {'language_id': LANGUAGES[language][0], 'source_code': _b64(code), 'stdin': _b64(stdin)})
    except requests.Timeout:
        model_controls.record_error(CONTROL_KEY, 'Timed out')
        raise RunError('The code runner took too long to answer. Please try again.')
    except requests.RequestException as exc:
        model_controls.record_error(CONTROL_KEY, f'{exc.__class__.__name__}: {exc}')
        raise RunError("Couldn't reach the code runner. Please try again in a minute.")
    if not response.ok:
        model_controls.record_error(CONTROL_KEY, f'HTTP {response.status_code}')
        raise RunError(_explain_http(response.status_code))
    try:
        body = response.json()
    except ValueError:
        model_controls.record_error(CONTROL_KEY, 'Not JSON')
        raise RunError('The code runner sent back something unexpected. Please try again.')

    status = body.get('status') or {}
    status_id = status.get('id')
    model_controls.record_success(CONTROL_KEY)
    return {
        'ok': status_id == _ACCEPTED,
        'verdict': status.get('description') or '',
        'compile_error': status_id == _COMPILE_ERROR,
        'timed_out': status_id == _TIME_LIMIT,
        'stdout': _clip(_unb64(body.get('stdout'))),
        'stderr': _clip(_unb64(body.get('stderr')) or _unb64(body.get('message'))),
        'compile_output': _clip(_unb64(body.get('compile_output'))),
        'time': body.get('time'),
        'memory': body.get('memory'),
    }


def test_connection():
    """One tiny Java program through the runner, for the dashboard's Test
    button. Returns (ok, message)."""
    key = get_key(API_KEY_SETTING).strip()
    if not key:
        return False, 'No Judge0 key is set.'
    program = 'public class Main { public static void main(String[] a) { System.out.println("ok"); } }'
    try:
        response = _post(key, {'language_id': LANGUAGES['java'][0], 'source_code': _b64(program), 'stdin': ''})
    except requests.RequestException as exc:
        return False, f'{exc.__class__.__name__}: {str(exc)[:200]}'
    if not response.ok:
        return False, _explain_http(response.status_code)
    try:
        body = response.json()
    except ValueError:
        return False, 'The runner answered with something that is not JSON.'
    if _unb64(body.get('stdout')).strip() == 'ok':
        return True, 'Working — a test Java program ran and printed its output.'
    return False, f"The runner answered but the test program didn't finish ({(body.get('status') or {}).get('description', 'unknown')})."
