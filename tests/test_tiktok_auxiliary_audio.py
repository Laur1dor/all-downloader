"""Real MP4s with corrupt auxiliary AAC must keep all video and primary sound."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from bot import downloader
from bot.media_parts import MediaPreparationError


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
class TikTokAuxiliaryAudioTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)

    def run_media(self,args):
        return subprocess.run(args,capture_output=True,check=True,timeout=40).stdout

    def fixture(self,audio=2,hdr=False):
        path=self.root/'fixture.mp4'
        args=['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=96x192:rate=60:duration=0.5']
        for i in range(audio):args+=['-f','lavfi','-i',f'sine=frequency={440+i*220}:sample_rate=44100:duration=0.5']
        args+=['-map','0:v:0']
        for i in range(audio):args+=['-map',f'{i+1}:a:0']
        args+=['-c:v','libx264','-threads','1','-g','1','-pix_fmt','yuv420p','-c:a','aac','-b:a','64k']
        if hdr:args+=['-x264-params','colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc',
                     '-color_trc','arib-std-b67','-color_primaries','bt2020','-colorspace','bt2020nc']
        args+=['-movflags','+faststart',str(path)]
        self.run_media(args)
        return path

    def corrupt(self,path,audio_index):
        packets=json.loads(self.run_media(['ffprobe','-v','error','-select_streams',f'a:{audio_index}',
            '-show_packets','-show_entries','packet=pos,size','-of','json',str(path)]))['packets']
        data=bytearray(path.read_bytes())
        for packet in packets:
            start,size=int(packet['pos']),int(packet['size']);data[start:start+size]=b'\xff'*size
        path.write_bytes(data)

    def packet_hash(self,path,mapping):
        return self.run_media(['ffmpeg','-v','error','-i',str(path),'-map',mapping,'-c','copy','-f','hash','-hash','sha256','-'])

    def test_real_bad_secondary_audio_is_removed_without_encoding(self):
        source=self.fixture();video=self.packet_hash(source,'0:v:0');audio=self.packet_hash(source,'0:a:0')
        self.corrupt(source,1);before=source.read_bytes()
        self.assertFalse(downloader._tiktok_playable(source))
        with patch.object(downloader,'_transcode_tiktok_compatible') as encode:
            repaired=downloader._repair_tiktok_auxiliary_audio(source,5_000_000,None)
        self.assertIsNotNone(repaired);encode.assert_not_called()
        self.assertTrue(downloader._tiktok_playable(repaired))
        self.assertEqual(self.packet_hash(repaired,'0:v:0'),video)
        self.assertEqual(self.packet_hash(repaired,'0:a:0'),audio)
        self.assertEqual(source.read_bytes(),before)
        probe=json.loads(self.run_media(['ffprobe','-v','error','-count_frames','-show_streams','-of','json',str(repaired)]))
        streams=probe['streams'];v=streams[0]
        self.assertEqual((v['width'],v['height'],v['r_frame_rate'],v['nb_read_frames']),(96,192,'60/1','30'))
        self.assertEqual(sum(s['codec_type']=='audio'for s in streams),1)

    def test_only_bad_extra_track_is_removed_and_good_secondary_is_kept(self):
        source=self.fixture(audio=3);good=self.packet_hash(source,'0:a:1');self.corrupt(source,2)
        result=downloader._repair_tiktok_auxiliary_audio(source,5_000_000,None)
        self.assertTrue(downloader._tiktok_playable(result))
        self.assertEqual(self.packet_hash(result,'0:a:1'),good)
        probe=json.loads(self.run_media(['ffprobe','-v','error','-show_streams','-of','json',str(result)]))
        self.assertEqual(sum(s['codec_type']=='audio'for s in probe['streams']),2)

    def test_healthy_multiple_tracks_are_untouched(self):
        source=self.fixture();before=source.read_bytes()
        self.assertIsNone(downloader._repair_tiktok_auxiliary_audio(source,5_000_000,None))
        self.assertEqual(source.read_bytes(),before)

    def test_bad_primary_sound_is_never_dropped(self):
        source=self.fixture();self.corrupt(source,0);before=source.read_bytes()
        self.assertIsNone(downloader._repair_tiktok_auxiliary_audio(source,5_000_000,None))
        self.assertEqual(source.read_bytes(),before)
        self.assertFalse(source.with_name('fixture-audio-compatible.mp4').exists())

    def test_size_limit_and_cancel_remove_intermediates(self):
        source=self.fixture();self.corrupt(source,1);before=source.read_bytes()
        self.assertIsNone(downloader._repair_tiktok_auxiliary_audio(source,1,None))
        self.assertFalse(source.with_name('fixture-audio-compatible.mp4').exists())
        cancel=Event()
        actual_run=downloader._run_ffmpeg
        def interrupted(command,*args):
            if command[-1]=='-':
                return actual_run(command,*args)
            Path(command[-1]).write_bytes(b'partial');cancel.set();raise MediaPreparationError('cancel')
        with patch.object(downloader,'_run_ffmpeg',side_effect=interrupted):
            with self.assertRaises(downloader.DownloadCancelledError):
                downloader._repair_tiktok_auxiliary_audio(source,5_000_000,cancel)
        self.assertFalse(source.with_name('fixture-audio-compatible.mp4').exists())
        self.assertEqual(source.read_bytes(),before)

    def test_video_packet_mismatch_is_rejected(self):
        source=self.fixture();self.corrupt(source,1)
        with patch.object(downloader,'_source_video_packet_hash',side_effect=['original','different']):
            self.assertIsNone(downloader._repair_tiktok_auxiliary_audio(source,5_000_000,None))
        self.assertFalse(source.with_name('fixture-audio-compatible.mp4').exists())

    def test_full_fetch_flow_repairs_audio_before_sei_or_transcoding(self):
        source=self.fixture();self.corrupt(source,1);before=source.read_bytes()
        item={'id':'123','video':{'playAddr':'https://v19.tiktokcdn-us.com/video'}}
        directory=self.root/'fetch';directory.mkdir()
        def fetch(_session,_url,destination,*args,**kwargs):
            destination.write_bytes(before);return True
        with patch.object(downloader,'_stream_to_file',side_effect=fetch), \
             patch('bot.tiktok_hdr.probe_tiktok_item_hdr',return_value=False), \
             patch.object(downloader,'_repair_tiktok_sei') as sei, \
             patch.object(downloader,'_transcode_tiktok_compatible') as encode:
            result,info=downloader._fetch_tiktok_media(item,MagicMock(proxies={}),str(directory),
                None,5_000_000,None,None,{},resolver=True)
        sei.assert_not_called();encode.assert_not_called()
        self.assertTrue(downloader._tiktok_playable(result))
        self.assertEqual(info['original_path'].read_bytes(),before)

    def test_hdr_packets_stay_hdr_in_repaired_document(self):
        source=self.fixture(hdr=True);video=self.packet_hash(source,'0:v:0');self.corrupt(source,1)
        self.assertTrue(downloader._source_hdr_state(source))
        result=downloader._repair_tiktok_auxiliary_audio(source,5_000_000,None)
        self.assertEqual(self.packet_hash(result,'0:v:0'),video)
        self.assertTrue(downloader._source_hdr_state(result))
        ready=downloader._prepare_source_document(source,5_000_000,None)
        self.assertTrue(ready.source_hdr)
        self.assertEqual(self.packet_hash(ready.path,'0:v:0'),video)
