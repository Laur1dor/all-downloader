"""The remote browser must stay open through phone confirmation."""
import sys
import unittest
import tempfile
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import igremote
from bot.handlers import admin


class RemoteChallengeTests(unittest.TestCase):
    def test_phone_login_and_identity_steps_are_challenges(self):
        for path in ('/accounts/suspended/', '/accounts/login/', '/auth_platform/recaptcha/', '/challenge/', '/checkpoint/'):
            self.assertTrue(igremote._challenged('https://www.instagram.com' + path))
        self.assertFalse(igremote._challenged('https://www.instagram.com/'))


class RemoteReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_command_reuses_active_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            status_file = folder / 'igremote_status.json'
            status_file.write_text('keep existing status')
            (folder / 'igremote.active').write_text('active')
            request_file = folder / 'igremote_request'
            opened = {'state': 'open', 'at': 1000, 'password': 'test',
                      'token': 'token', 'public': 'https://public.example', 'challenged': True}
            notice = Mock(edit_text=AsyncMock())
            target = Mock(answer=AsyncMock(return_value=notice))
            router = admin.create_router(123)
            callback = next(h.callback for h in router.message.handlers
                            if h.callback.__name__ == 'handle_igcaptcha')
            with (patch.object(admin, '_IGREMOTE_STATUS', status_file),
                  patch.object(admin, '_IGREMOTE_REQUEST', request_file),
                  patch.object(admin, '_igremote_status', side_effect=[opened, {'state':'signed_in'}]),
                  patch('bot.handlers.admin.time.time', return_value=1001),
                  patch('bot.handlers.admin.asyncio.sleep', new=AsyncMock())):
                await callback(target)
            self.assertFalse(request_file.exists())
            self.assertEqual(status_file.read_text(), 'keep existing status')
            self.assertIn('public.example/token/vnc.html', notice.edit_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
