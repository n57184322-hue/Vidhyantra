"""Account → Themes (free themes besides Classic, for every account) and
Customize → Background animation."""
import re

from django.contrib.auth.models import User
from django.test import TestCase

from myapp.models import StoreProfile

SKINS = ('aurora', 'paper', 'sakura', 'terminal', 'ocean')
# Styled through shared rules from their own --sk-* colours.
KIT_SKINS = ('ironman', 'hulk', 'doraemon', 'shinchan', 'batman', 'superman', 'sunset', 'forest', 'royal', 'mono',
             'hindu', 'islamic', 'christian', 'sikh', 'jain', 'buddhist',
             'rose', 'midnight', 'coffee', 'lavender', 'neon', 'mint')
BG_ANIMS = ('aurora', 'stars', 'bubbles', 'grid', 'mesh', 'ripples', 'snow', 'fireflies', 'waves',
            'petals', 'rain', 'confetti', 'matrix', 'lava', 'sparkle')


class ThemesPageTests(TestCase):
    def setUp(self):
        # An ordinary free account: themes are not a premium feature.
        self.user = User.objects.create_user('plain@example.com', 'plain@example.com', 'pw')
        StoreProfile.objects.get_or_create(user=self.user, defaults={'ai_onboarded': True})
        self.client.force_login(self.user)
        self.body = self.client.get('/').content.decode()

    def test_the_menu_and_the_account_window_offer_themes(self):
        self.assertIn('data-account-panel="themes"', self.body)
        self.assertIn('data-account-tab="themes"', self.body)
        self.assertIn('data-account-content="themes"', self.body)

    def test_classic_and_five_themes_each_have_a_card_and_their_own_look(self):
        cards = re.findall(r'data-skin-choice="([a-z]+)"', self.body)
        self.assertEqual(cards, ['classic', *SKINS, *KIT_SKINS])
        for skin in SKINS:
            # Colours, fonts, a moving backdrop, bubbles and an entrance animation.
            self.assertIn(f':root[data-skin="{skin}"]{{--red:', self.body)
            self.assertIn(f':root[data-skin="{skin}"]{{--font-head:', self.body)
            self.assertIn(f':root[data-skin="{skin}"] .chat-shell::before{{', self.body)
            self.assertIn(f':root[data-skin="{skin}"] .msg-row.assistant .msg-bubble{{', self.body)
            self.assertRegex(self.body, rf':root\[data-skin="{skin}"\] \.msg-row(\.assistant|\.user)?\{{animation:skin-')
            self.assertIn(f'.skin-preview[data-p="{skin}"]{{', self.body)

    def test_hero_cartoon_and_mood_themes_each_have_their_own_look(self):
        for skin in KIT_SKINS:
            with self.subTest(skin=skin):
                self.assertIn(f':root[data-skin="{skin}"]{{--red:', self.body)
                self.assertIn(f':root[data-skin="{skin}"]{{--font-head:', self.body)
                self.assertIn(f':root[data-skin="{skin}"] .chat-shell::before{{', self.body)
                self.assertRegex(self.body, rf':root:is\([^)]*\[data-skin="{skin}"\][^)]*\) \.msg-row\.assistant \.msg-bubble\{{')
                self.assertIn(f'.skin-preview[data-p="{skin}"]{{', self.body)
                self.assertIn(f"{skin}: '", self.body.split('</script>', 1)[0])

    def test_every_background_animation_has_a_card_and_rules(self):
        cards = re.findall(r'data-bg-anim-choice="([a-z]+)"', self.body)
        self.assertEqual(cards, ['orbit', *BG_ANIMS, 'none'])
        for anim in BG_ANIMS:
            with self.subTest(anim=anim):
                self.assertIn(f':root[data-bg-anim="{anim}"] .empty-state::', self.body)
                self.assertIn(f'.bg-preview[data-anim="{anim}"]::', self.body)
        head = self.body.split('</script>', 1)[0]
        self.assertIn("localStorage.getItem('ai_bg_anim')", head)

    def test_a_saved_theme_is_applied_before_the_page_paints(self):
        head = self.body.split('</script>', 1)[0]
        self.assertIn("var AI_SKINS = { aurora: 'dark', paper: 'light', sakura: 'light', terminal: 'dark', ocean: 'dark',", head)
        self.assertIn("localStorage.getItem('ai_skin')", head)

    def test_a_font_picked_under_customize_still_wins_over_a_theme(self):
        theme_fonts = self.body.index(':root[data-skin="aurora"]{--font-head:')
        picker_fonts = self.body.index(':root[data-font="inter"]{')
        self.assertLess(theme_fonts, picker_fonts)
        # ...while the theme's colours come after the light/dark ones.
        self.assertLess(self.body.index(':root[data-theme="dark"]{'), self.body.index(':root[data-skin="aurora"]{--red:'))


class DefaultAndDailyThemeTests(TestCase):
    """Doraemon until a superuser picks another default; "Change theme every
    day" picks one from the date. Both are applied before the page paints."""

    def setUp(self):
        self.user = User.objects.create_user('daily@example.com', 'daily@example.com', 'pw')
        StoreProfile.objects.get_or_create(user=self.user, defaults={'ai_onboarded': True})
        self.admin = User.objects.create_superuser('theme-boss', 'boss@example.com', 'pw')
        StoreProfile.objects.get_or_create(user=self.admin, defaults={'ai_onboarded': True})

    def head(self, user=None):
        if user:
            self.client.force_login(user)
        return self.client.get('/').content.decode().split('</script>', 1)[0]

    def test_doraemon_is_the_default_before_first_paint(self):
        head = self.head()
        self.assertIn("var AI_DEFAULT_SKIN = 'doraemon';", head)
        self.assertIn("if (localStorage.getItem('ai_skin_daily') === '1') skin = aiDailySkin();", head)
        self.assertIn('else if (!skin) skin = AI_DEFAULT_SKIN;', head)

    def test_every_theme_on_the_page_can_be_made_the_default(self):
        from myapp.views import AI_THEME_NAMES
        head = self.head()
        listed = set(re.findall(r"(\w+): '(?:dark|light)'", head.split('var AI_SKINS =', 1)[1].split('};', 1)[0]))
        self.assertEqual(listed, set(AI_THEME_NAMES))

    def test_the_daily_switch_is_for_everyone_and_the_default_box_for_superusers(self):
        self.client.force_login(self.user)
        page = self.client.get('/').content.decode()
        self.assertIn('id="skinDailyToggle"', page)
        self.assertNotIn('id="skinDefaultSelect"', page)
        self.client.force_login(self.admin)
        self.assertIn('id="skinDefaultSelect"', self.client.get('/').content.decode())

    def test_a_superuser_sets_the_default_for_everyone(self):
        from myapp.models import SiteCustomization
        self.client.force_login(self.admin)
        response = self.client.post('/AI/api/default-theme/', '{"skin": "islamic"}', content_type='application/json')
        self.assertEqual(response.json(), {'status': 'ok', 'skin': 'islamic'})
        self.assertEqual(SiteCustomization.get_solo().default_skin, 'islamic')
        self.client.logout()
        self.assertIn("var AI_DEFAULT_SKIN = 'islamic';", self.head())
        self.client.force_login(self.admin)
        self.client.post('/AI/api/default-theme/', '{"skin": "classic"}', content_type='application/json')
        self.assertEqual(SiteCustomization.get_solo().default_skin, '')

    def test_others_cannot_change_it_and_unknown_themes_are_refused(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/AI/api/default-theme/', '{"skin": "mono"}', content_type='application/json').status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post('/AI/api/default-theme/', '{"skin": "<script>"}', content_type='application/json').status_code, 400)
