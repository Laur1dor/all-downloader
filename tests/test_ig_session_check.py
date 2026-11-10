import sys,unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import iglogin_browser as login

class ScheduledSessionCheckTests(unittest.TestCase):
    def test_check_never_submits_login_or_checkpoint(self):
        for authenticated,expected in [(True,0),(False,3),(None,3)]:
            with self.subTest(authenticated=authenticated):
                page=MagicMock();page.url='https://www.instagram.com/'
                context=MagicMock();context.pages=[page]
                driver=MagicMock();driver.chromium.launch_persistent_context.return_value=context
                playwright=MagicMock();playwright.sync_playwright.return_value=nullcontext(driver)
                with patch.dict(sys.modules,{'playwright.sync_api':playwright}),patch.dict('os.environ',{'IG_USERNAME':'test','IG_PASSWORD':'test'}),patch.object(login.PROFILE_DIR.__class__,'mkdir'),patch.object(login,'clear_stale_profile_lock'),patch.object(login,'browser_proxy',return_value=None),patch.object(login,'_logged_in',return_value=authenticated),patch.object(login,'write_cookies',return_value=12) as cookies,patch.object(login,'_one_tap') as submit,patch.object(login,'_clear_checkpoint') as checkpoint:
                    self.assertEqual(login._run_browser(check_only=True),expected)
                    submit.assert_not_called();checkpoint.assert_not_called()
                    if authenticated:cookies.assert_called_once()
                    else:cookies.assert_not_called()
                context.close.assert_called_once()
    def test_scheduled_check_does_not_queue_behind_downloads(self):
        import ig_profile
        with patch.object(ig_profile,'profile_lock',return_value=nullcontext()) as lock,patch.object(login,'_run_browser',return_value=0) as browser:
            self.assertEqual(login.run(check_only=True),0)
        lock.assert_called_once_with(timeout=0);browser.assert_called_once_with(check_only=True)
    def test_schedule_is_bounded_and_does_not_catch_up_on_boot(self):
        root=Path(__file__).resolve().parents[1]/'scripts'
        text=(root/'ig-session-check.timer').read_text(encoding='utf-8-sig')
        self.assertIn('10,18:00:00 Europe/Moscow',text);self.assertIn('Persistent=false',text)
        self.assertIn('--check-only',(root/'ig-session-check.service').read_text(encoding='utf-8-sig'))
