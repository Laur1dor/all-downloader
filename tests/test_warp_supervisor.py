"""Exercise supervisor exit on loss of the WARP daemon, even with SOCKS alive."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'requires Linux container shell')
class WarpSupervisorTests(unittest.TestCase):
    def test_exits_when_daemon_dies_while_vendor_entrypoint_is_running(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/warp-supervise.sh'
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            vendor=folder/'vendor';vendor.write_text('#!/bin/sh\nexec sleep 20\n');vendor.chmod(0o700)
            ps=folder/'ps'
            ps.write_text('#!/bin/sh\nif [ ! -e "$WARP_TEST_SEEN" ]; then\n touch "$WARP_TEST_SEEN"\n echo "S warp-svc"\nelse\n echo "S gost"\nfi\n');ps.chmod(0o700)
            env={**os.environ,'PATH':f'{folder}:{os.environ["PATH"]}','WARP_TEST_SEEN':str(folder/'seen'),
                 'WARP_WATCH_GRACE':'0.05','WARP_WATCH_INTERVAL':'0.05'}
            result=subprocess.run(['bash',str(script),str(vendor)],env=env,capture_output=True,text=True,timeout=4)
            self.assertTrue((folder/'seen').exists())
            self.assertEqual(result.returncode,1)
            self.assertIn('WARP daemon exited',result.stderr)
