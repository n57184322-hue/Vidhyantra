"""An added chat model shown under its own name: a GPT name wears the
built-in GPT mark (Sol's) in the model picker, and the model and company it
really runs on never show through as itself. The added Mistral Large 4 is
renamed GPT-5.6 Sol, with an identity that keeps that private."""
import importlib
import json
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from myapp import ai_chat, custom_models, model_controls, views
from myapp.models import AIModelControl, CustomAIModel, StoreProfile

rename_migration = importlib.import_module('myapp.migrations.0111_rename_mistral_large_to_gpt_56_sol')
MISTRAL = 'mistralai/mistral-large-4-1t-a52b'


def added(name, model_id=MISTRAL, description=''):
    return CustomAIModel.objects.create(kind=CustomAIModel.KIND_CHAT, model_id=model_id,
                                        display_name=name, description=description)


class AddedModelTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def tearDown(self):
        CustomAIModel.objects.all().delete()
        custom_models.mark_changed()

    def sol(self):
        row = added('GPT-5.6 Sol', description='The flagship GPT-5.6 model.')
        custom_models.mark_changed()
        return row


class GptLookTests(AddedModelTestCase):
    def test_a_gpt_name_gets_the_gpt_mark(self):
        for name, look in (('GPT-5.6 Sol', 'sol'), ('gpt 5.6 sol', 'sol'), ('ChatGPT 7', 'sol'),
                           ('Mistral Large 4 1t A52B', ''), ('Cryptographer', '')):
            with self.subTest(name=name):
                self.assertEqual(custom_models._config(added(name))['look'], look)

    def test_the_picker_shows_the_gpt_mark_for_it(self):
        row = self.sol()
        user = User.objects.create_user('look@example.com', 'look@example.com', 'pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=user, defaults={'ai_onboarded': True})
        self.client.force_login(user)
        page = self.client.get('/').content.decode()
        self.assertIn(f'data-model="{row.model_key}" data-look="{ai_chat.SOL_MODEL_KEY}"', page)
        self.assertIn('>GPT-5.6 Sol<', page)
        self.assertIn('The flagship GPT-5.6 model.', page)
        option = page[page.index(f'data-model="{row.model_key}"'):]
        self.assertIn(f'class="model-option-icon" data-m="{ai_chat.SOL_MODEL_KEY}"', option[:600])


class BackendNameTests(SimpleTestCase):
    def test_the_maker_and_family_come_from_the_model_name(self):
        names = ai_chat.backend_names(MISTRAL, 'GPT-5.6 Sol')
        for word in ('Mistral AI', 'Mistral', 'Mixtral', 'Le Chat', 'NVIDIA', 'Nemotron'):
            self.assertIn(word, names)
        self.assertEqual(names[0], 'Mistral AI')     # longest first, so "Mistral AI" is matched whole
        # An unknown publisher: its own name, but never a word the shown name uses.
        self.assertIn('acme', ai_chat.backend_names('acme-ai/zorbo-7b-chat', 'Zorbo Pro'))
        self.assertNotIn('zorbo', ai_chat.backend_names('acme-ai/zorbo-7b-chat', 'Zorbo Pro'))

    def test_questions_about_the_assistant_itself(self):
        for text in ('who are you', 'Who made you?', 'which model are you', 'are you mistral?', 'what model is this',
                     'what is your base model', 'introduce yourself', 'tum kaun ho', 'aapko kisne banaya', 'who r u'):
            self.assertTrue(ai_chat.asks_about_yourself(text), text)
        for text in ('tell me about mistral ai', 'what are you able to do', 'what are your thoughts on llama',
                     'write a poem about the sun', 'what is 2 + 2'):
            self.assertFalse(ai_chat.asks_about_yourself(text), text)


class ReplyFilterTests(AddedModelTestCase):
    def setUp(self):
        super().setUp()
        self.key = self.sol().model_key
        brand = patch.object(ai_chat, 'get_ai_brand_name', return_value='Vidhyantra')
        brand.start()
        self.addCleanup(brand.stop)

    def hide(self, text, identity=None):
        return views._hide_added_model_backend(text, self.key, identity)

    def test_talking_about_itself_never_names_mistral(self):
        cases = {
            "I'm Mistral Large, a model developed by Mistral AI. How can I help?":
                "I'm GPT-5.6 Sol, a model developed by the Vidhyantra team. How can I help?",
            'Hello! I am GPT-5.6 Sol, created by Mistral AI.': 'Hello! I am GPT-5.6 Sol, created by the Vidhyantra team.',
            'Mistral AI created me.': 'The Vidhyantra team created me.',
            'Under the hood, the model is Mistral Large 4.': 'Under the hood, the model is GPT-5.6 Sol.',
            'My name is Le Chat.': 'My name is GPT-5.6 Sol.',
            f'I run on {MISTRAL}.': 'I run on GPT-5.6 Sol.',
            "I'm based on the Mixtral architecture, built by the Mistral team.":
                "I'm based on GPT-5.6 Sol, built by the Vidhyantra team.",
            'Main Mistral AI ka model hoon.': 'Main the Vidhyantra team ka model hoon.',
        }
        for leaked, shown in cases.items():
            with self.subTest(leaked=leaked):
                self.assertEqual(self.hide(leaked), shown)

    def test_an_answer_about_mistral_keeps_its_facts(self):
        text = 'Mistral AI is a French company founded in 2023. Its Le Chat assistant is popular.'
        self.assertEqual(self.hide(text), text)

    def test_an_owner_identity_that_names_the_maker_is_kept(self):
        text = 'I was made by Mistral AI.'
        self.assertEqual(self.hide(text, {'creator': 'Mistral AI'}), text)

    def test_built_in_models_are_left_to_their_own_filters(self):
        text = 'I was made by Mistral AI.'
        self.assertEqual(views._hide_added_model_backend(text, 'quick'), text)

    def test_the_chat_shows_the_filtered_reply(self):
        user = User.objects.create_user('sol@example.com', 'sol@example.com', 'pw', is_staff=True)
        StoreProfile.objects.get_or_create(user=user, defaults={'ai_onboarded': True})
        self.client.force_login(user)
        with patch('myapp.views.ai_chat.stream_chat', return_value=iter(["I'm Mistral Large, made by Mistral AI. Ask away!"])):
            response = self.client.post('/AI/api/send/', data=json.dumps({'message': 'hi there', 'model': self.key}),
                                        content_type='application/json')
            text = b''.join(response.streaming_content).decode()
        self.assertIn('GPT-5.6 Sol', text)
        self.assertNotIn('Mistral', text)


class StreamTests(AddedModelTestCase):
    def setUp(self):
        super().setUp()
        self.key = self.sol().model_key
        self.sent = []

    def ask(self, question, *replies):
        replies = list(replies)

        def fake_stream(client, kwargs):
            self.sent.append(kwargs)
            yield from replies.pop(0) if len(replies) > 1 else replies[0]

        with patch.object(ai_chat, '_stream_content', side_effect=fake_stream), \
                patch.object(ai_chat, '_client_for_cfg', return_value=object()), \
                patch.object(ai_chat, 'STREAM_RETRY_BACKOFF_SECONDS', 0):
            return ''.join(ai_chat._stream_chat_impl([{'role': 'user', 'content': question}], self.key))

    def test_the_prompt_keeps_the_maker_private(self):
        self.ask('hello', ['Hi!'])
        system, reminder = self.sent[0]['messages'][0]['content'], self.sent[0]['messages'][-2]['content']
        self.assertIn('YOUR IDENTITY: you are GPT-5.6 Sol', system)
        self.assertIn('Mistral AI', system)
        self.assertIn('Identity lock: you are GPT-5.6 Sol', reminder)

    def test_who_are_you_naming_mistral_is_answered_again(self):
        reply = self.ask('who are you?', ["I'm Mistral Large, ", 'made by Mistral AI.'], ["I'm GPT-5.6 Sol. How can I help?"])
        self.assertEqual(reply, "I'm GPT-5.6 Sol. How can I help?")
        self.assertEqual(len(self.sent), 2)

    def test_a_model_that_keeps_naming_it_gets_the_set_answer(self):
        reply = self.ask('which model are you really', ['Honestly, I am Mistral Large 4.'])
        self.assertIn("I'm GPT-5.6 Sol", reply)
        self.assertNotIn('Mistral', reply)
        self.assertEqual(len(self.sent), ai_chat.STREAM_RETRY_ATTEMPTS + 1)

    def test_an_ordinary_question_about_mistral_is_answered_once(self):
        reply = self.ask('tell me about Mistral AI', ['Mistral AI is a French AI company.'])
        self.assertEqual(reply, 'Mistral AI is a French AI company.')
        self.assertEqual(len(self.sent), 1)

    def test_an_owner_identity_naming_the_maker_turns_the_lock_off(self):
        model_controls.set_identity(self.key, creator='Mistral AI')
        reply = self.ask('who made you?', ['I was made by Mistral AI.'])
        self.assertEqual(reply, 'I was made by Mistral AI.')
        self.assertNotIn('YOUR IDENTITY', self.sent[0]['messages'][0]['content'])


class RenameMigrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_added_mistral_model_becomes_gpt_56_sol_with_a_private_identity(self):
        mistral = added('Mistral Large 4 1t A52B')
        edited = added('My own name', description='Kept.')
        rename_migration.rename(apps, None)
        mistral.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(mistral.display_name, 'GPT-5.6 Sol')
        self.assertIn('flagship GPT-5.6', mistral.description)
        self.assertLessEqual(len(mistral.description), 300)
        self.assertEqual((edited.display_name, edited.description), ('My own name', 'Kept.'))
        identity = model_controls.get_identity(mistral.model_key)
        self.assertEqual(identity['name'], 'GPT-5.6 Sol')
        self.assertIn('private', identity['notes'])
        self.assertFalse(AIModelControl.objects.filter(model_key=edited.model_key).exists())
        names = ai_chat.backend_name_re(ai_chat.backend_names(MISTRAL, 'GPT-5.6 Sol'))
        self.assertFalse(names.search(' '.join(identity.values())))    # names no maker, so the lock stays on
        self.assertFalse(ai_chat._IDENTITY_LEAK_RE.search(' '.join(identity.values())))

    def test_an_identity_already_set_is_kept(self):
        mistral = added('Mistral Large 4 1t A52B')
        model_controls.set_identity(mistral.model_key, name='Sol Pro', tone='formal')
        rename_migration.rename(apps, None)
        cache.clear()
        self.assertEqual(model_controls.get_identity(mistral.model_key), {'name': 'Sol Pro', 'tone': 'formal'})
