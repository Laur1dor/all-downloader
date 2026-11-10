import sys,time,unittest,email.utils
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import iglogin_browser as login

class MailRecoveryTests(unittest.TestCase):
    def message(self,**changes):
        m=EmailMessage();m['From']=changes.get('sender','Instagram <security@mail.instagram.com>');m['To']=changes.get('recipient','owner@example.com')
        m['Date']=changes.get('date',email.utils.formatdate(time.time(),usegmt=True));m.set_content(changes.get('body','Hi account.media, your code is 023896'),cte='base64');return m.as_bytes()
    def code(self,**changes):return login._mail_code(self.message(**changes),time.time()-60,'owner@example.com','account.media')
    def test_current_profile_code_preserves_leading_zero(self):self.assertEqual(self.code(),'023896')
    def test_wrong_sender_recipient_profile_and_stale_mail_are_rejected(self):
        for changes in [{'sender':'Instagram <bad@evil.example>'},{'sender':'bad@instagram.com.evil.example'},{'recipient':'other@example.com'},{'body':'Hi oldaccount.media, your code is 123456'},{'date':email.utils.formatdate(time.time()-3600,usegmt=True)},{'date':'invalid'}]:
            with self.subTest(changes=changes):self.assertIsNone(self.code(**changes))
    def test_meta_sender_is_accepted(self):self.assertEqual(self.code(sender='noreply@account.meta.com'),'023896')
    def test_new_code_form_is_not_confused_with_login_form(self):
        self.assertIn('input[name="email"]',login._checkpoint_code_fields(SimpleNamespace(url='https://www.instagram.com/auth_platform/codeentry/?x=1')))
        self.assertNotIn('input[name="email"]',login._checkpoint_code_fields(SimpleNamespace(url='https://www.instagram.com/accounts/login/')))
    def test_mail_fetch_is_read_only_and_does_not_mark_seen(self):
        box=MagicMock();box.search.return_value=('OK',[b'7']);box.fetch.return_value=('OK',[(b'header',self.message())])
        with patch.dict('os.environ',{'IG_IMAP_HOST':'imap.example.com','IG_IMAP_USER':'owner@example.com','IG_IMAP_PASSWORD':'secret','IG_HANDLE':'account.media'}),patch.object(login.imaplib,'IMAP4_SSL') as imap:
            imap.return_value.__enter__.return_value=box
            self.assertEqual(login.code_from_mail(time.time()-60),'023896')
        box.select.assert_called_once_with('INBOX',readonly=True);box.fetch.assert_called_once_with(b'7','(BODY.PEEK[])')

class AuthenticatedProfileTests(unittest.TestCase):
    def test_save_login_prompt_is_dismissed_only_on_instagram(self):
        page=MagicMock(url='https://www.instagram.com/accounts/onetap/')
        with patch.object(login,'_click_named',return_value=True) as click:
            login._dismiss_save_login(page)
            click.assert_called_once_with(page,('Not now','Не сейчас'))
            page.wait_for_timeout.assert_called_once_with(3000)
        for url in ['https://evil.example/accounts/onetap/','https://www.instagram.com/accounts/login/']:
            page.url=url
            with patch.object(login,'_click_named') as click:
                login._dismiss_save_login(page);click.assert_not_called()

    def test_visible_current_profile_and_inbox_confirm_unknown_probe(self):
        page=MagicMock(url='https://www.instagram.com/')
        page.context.cookies.return_value=[{'name':'sessionid','value':'secret'}]
        page.evaluate.return_value={'status':404}
        def locator(selector):
            obj=MagicMock();obj.first=obj;obj.count.return_value=1 if selector in ('a[href="/account.media/"] img','a[href="/direct/inbox/"]') else 0;return obj
        page.locator.side_effect=locator
        with patch.dict('os.environ',{'IG_HANDLE':'account.media'}):
            self.assertTrue(login._authentication_state(page))
            page.url='https://www.instagram.com/accounts/login/'
            self.assertIsNone(login._authentication_state(page))
            page.url='https://www.instagram.com/'
            page.context.cookies.return_value=[]
            self.assertFalse(login._authentication_state(page))
    def test_remembered_account_without_inbox_is_inconclusive(self):
        page=MagicMock(url='https://www.instagram.com/')
        page.context.cookies.return_value=[{'name':'sessionid','value':'remembered'}]
        page.evaluate.return_value={'status':200,'unknown':True}
        def locator(selector):
            obj=MagicMock();obj.first=obj;obj.count.return_value=1 if selector=='a[href="/account.media/"] img' else 0;return obj
        page.locator.side_effect=locator
        with patch.dict('os.environ',{'IG_HANDLE':'account.media'}):self.assertIsNone(login._authentication_state(page))

if __name__=='__main__':unittest.main()
