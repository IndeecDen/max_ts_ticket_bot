import unittest
from app.services.delivery import render_work_card


class CardLinkTests(unittest.TestCase):
    def test_links_escape_user_content(self):
        text = 'Заявка #1\nАвтор: A <B>\nЧат: -20\nОжидает\n\n<b>client</b>'
        result = render_work_card(text, 10, {'title': 'Help & support', 'link': 'https://max.ru/join/test'})
        self.assertIn('href="max://user/10">A &lt;B&gt;', result)
        self.assertIn('href="https://max.ru/join/test">Help &amp; support', result)
        self.assertIn('&lt;b&gt;client&lt;/b&gt;', result)

    def test_missing_or_unsafe_chat_link_keeps_title(self):
        for link in (None, 'javascript:alert(1)', 'https://max.ru.evil/test'):
            result = render_work_card('Заявка #1\nАвтор: A\nЧат: -20\nТекст', 10, {'title': 'Private', 'link': link})
            self.assertIn('Чат: Private\n', result)
            self.assertNotIn('href="https:', result)

    def test_large_body_is_bounded_without_broken_entities(self):
        result = render_work_card('Заявка #1\nАвтор: A\nЧат: -20\n' + '&😀' * 4000, 10, {})
        self.assertLessEqual(len(result.encode('utf-16-le')) // 2, 3900)
        self.assertTrue(result.endswith('…'))
