"""The chat's code editor: Java & co. go to the Judge0 runner through
/AI/api/code/run/ (never run on this server), and the chat page carries the
editor plus the list of languages the runner can take. Judge0 itself is
replaced by a fake here, so these tests make no network calls."""
import base64
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from myapp import code_runner
from myapp.models import ProviderAPICredential, StoreProfile


def _judge0(stdout='', status=3, description='Accepted', compile_output='', stderr=''):
    response = mock.Mock(ok=True, status_code=200)
    response.json.return_value = {
        'status': {'id': status, 'description': description}, 'time': '0.05', 'memory': 30000,
        'stdout': base64.b64encode(stdout.encode()).decode() if stdout else None,
        'stderr': base64.b64encode(stderr.encode()).decode() if stderr else None,
        'compile_output': base64.b64encode(compile_output.encode()).decode() if compile_output else None,
    }
    return response


class PrepareJavaTests(SimpleTestCase):
    def test_a_named_public_class_is_started_through_main(self):
        source = (
            'package com.shop;\n\nimport java.util.*;\n\npublic class Billing {\n'
            '    static class Line { int qty; }\n'
            '    // the "{" in this comment and string must not confuse anything\n'
            '    public static void main(String[] args) { System.out.println("{"); }\n}\n'
        )
        prepared = code_runner.prepare_java(source)
        self.assertNotIn('package com.shop', prepared)
        self.assertIn('\nclass Billing {', prepared)
        self.assertNotIn('public class Billing', prepared)
        self.assertIn('public class Main {', prepared)
        self.assertIn('Billing.main(args);', prepared)

    def test_a_main_class_is_left_alone(self):
        source = 'public class Main {\n    public static void main(String[] a) {}\n}\n'
        prepared = code_runner.prepare_java(source)
        self.assertEqual(prepared.count('class Main'), 1)
        self.assertNotIn('.main(args)', prepared)

    def test_main_inside_a_nested_class_is_found(self):
        source = 'public class Outer {\n    static class Inner {\n        public static void main(String[] a) {}\n    }\n}\n'
        self.assertIn('Outer.Inner.main(args);', code_runner.prepare_java(source))

    def test_code_without_main_gets_a_plain_message(self):
        with self.assertRaisesMessage(code_runner.RunError, 'no main method'):
            code_runner.prepare_java('public class Util { static int twice(int x) { return 2 * x; } }')


class CodeRunEndpointTests(TestCase):
    url = '/AI/api/code/run/'

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('coder', email='coder@example.com', password='pw')
        StoreProfile.objects.create(user=self.user, ai_onboarded=True)

    def post(self, **body):
        return self.client.post(self.url, data=json.dumps(body), content_type='application/json')

    def test_visitors_must_log_in(self):
        response = self.post(language='java', code='class Main {}')
        self.assertEqual(response.status_code, 401)

    def test_without_a_key_the_message_says_how_to_switch_it_on(self):
        self.client.force_login(self.user)
        response = self.post(language='java', code='public class Main { public static void main(String[] a) {} }')
        self.assertEqual(response.status_code, 400)
        self.assertIn('AI Models Management', response.json()['detail'])

    def test_java_runs_and_returns_its_output(self):
        ProviderAPICredential.objects.create(setting_name=code_runner.API_KEY_SETTING, value='k')
        self.client.force_login(self.user)
        with mock.patch.object(code_runner, '_post', return_value=_judge0('Hi Asha\n')) as sent:
            response = self.post(language='java', stdin='Asha\n',
                                 code='public class Greeter { public static void main(String[] a) {} }')
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['ok'])
        self.assertEqual(body['verdict'], 'Accepted')
        self.assertEqual(body['stdout'], 'Hi Asha\n')
        payload = sent.call_args.args[1]
        self.assertEqual(payload['language_id'], 62)
        self.assertEqual(base64.b64decode(payload['stdin']).decode(), 'Asha\n')
        self.assertIn('Greeter.main(args);', base64.b64decode(payload['source_code']).decode())

    def test_a_compile_error_is_reported_not_raised(self):
        ProviderAPICredential.objects.create(setting_name=code_runner.API_KEY_SETTING, value='k')
        self.client.force_login(self.user)
        reply = _judge0(status=6, description='Compilation Error', compile_output="Main.java:1: error: ';' expected")
        with mock.patch.object(code_runner, '_post', return_value=reply):
            body = self.post(language='c', code='int main() { return 0 }').json()
        self.assertEqual(body['status'], 'ok')
        self.assertFalse(body['ok'])
        self.assertTrue(body['compile_error'])
        self.assertIn("';' expected", body['compile_output'])

    def test_a_refused_key_and_unknown_languages_get_plain_messages(self):
        ProviderAPICredential.objects.create(setting_name=code_runner.API_KEY_SETTING, value='k')
        self.client.force_login(self.user)
        with mock.patch.object(code_runner, '_post', return_value=mock.Mock(ok=False, status_code=403)):
            self.assertIn('key was refused', self.post(language='go', code='package main').json()['detail'])
        self.assertIn("can't be run here", self.post(language='python', code='print(1)').json()['detail'])

    def test_runs_are_rate_limited_per_user(self):
        self.client.force_login(self.user)
        for _ in range(30):
            self.post(language='java', code='x')
        self.assertEqual(self.post(language='java', code='x').status_code, 429)


class CodeEditorPageTests(TestCase):
    def setUp(self):
        cache.clear()   # saved keys are cached briefly (myapp.provider_keys)
        self.admin = User.objects.create_superuser('boss-code', email='bosscode@example.com', password='pw')
        StoreProfile.objects.create(user=self.admin, ai_onboarded=True)

    def test_the_editor_and_backup_link_are_on_the_page(self):
        self.client.force_login(self.admin)
        page = self.client.get('/')
        self.assertContains(page, 'id="codeStudio"')
        self.assertContains(page, 'id="codeStudioBtn"')
        # The preview must never get allow-same-origin: with allow-scripts that
        # would let a previewed page reach this site's cookies and APIs.
        self.assertContains(page, '<iframe id="csFrame" title="Live preview" sandbox="allow-scripts allow-modals allow-forms allow-popups"')
        self.assertContains(page, 'href="/store/dashboard/backup/" role="menuitem"')
        self.assertContains(page, '<script id="codeRunnerLangs" type="application/json">[]</script>', html=False)

    def test_the_runner_languages_appear_once_a_key_is_saved(self):
        ProviderAPICredential.objects.create(setting_name=code_runner.API_KEY_SETTING, value='k')
        self.client.force_login(self.admin)
        page = self.client.get('/').content.decode()
        self.assertIn('"java"', page.split('id="codeRunnerLangs"')[1][:200])

    def test_backup_link_is_only_for_superusers(self):
        user = User.objects.create_user('plain-code', email='plaincode@example.com', password='pw')
        StoreProfile.objects.create(user=user, ai_onboarded=True)
        self.client.force_login(user)
        self.assertNotContains(self.client.get('/'), 'href="/store/dashboard/backup/"')

    def test_the_runner_has_a_panel_on_ai_models_management(self):
        self.client.force_login(self.admin)
        page = self.client.get('/store/dashboard/api-settings/')
        self.assertContains(page, 'Code runner (Java, C, C++ and more)')
        self.assertContains(page, 'Judge0 CE key from RapidAPI')
