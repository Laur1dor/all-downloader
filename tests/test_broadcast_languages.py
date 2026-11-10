import unittest
from bot.broadcasts import localized_source

class BroadcastLanguageTests(unittest.TestCase):
    def test_locale_and_button(self):
        variants={lang:{"message_id":mid,"reply_markup":{"inline_keyboard":[[{"text":lang,"url":"https://t.me/klenvpnbot?start=c_dk8phj"}]]}} for lang,mid in [("ru",11),("en",22)]}
        for locale,expected in [("ru",11),("ru-RU",11),("RU_ru",11),("en",22),("de",22),(None,22),("",22)]:
            with self.subTest(locale=locale):
                chat,mid,markup=localized_source({"source_chat":101,"variants":variants,"language_code":locale})
                self.assertEqual((chat,mid),(101,expected))
                self.assertEqual(markup.inline_keyboard[0][0].url,"https://t.me/klenvpnbot?start=c_dk8phj")
    def test_json_and_legacy(self):
        self.assertEqual(localized_source({"source_chat":101,"source_message":9}),(101,9,None))
        self.assertEqual(localized_source({"source_chat":101,"variants":'{"ru":{"message_id":11},"en":{"message_id":22}}',"language_code":"en"}),(101,22,None))
