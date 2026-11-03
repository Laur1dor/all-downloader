"""Source repair must handle independent MP4, NAL and audio faults together."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from bot import downloader as d
from bot.media_parts import MediaPreparationError


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
class SourceDocumentCombinationTests(unittest.TestCase):
    def run_media(self,args,check=True):
        return subprocess.run(args,capture_output=True,check=check,timeout=40)

    def fixture(self,root,transfer='arib-std-b67',tail_moov=False,bad_duration=False,bad_audio=False,bad_picture=False):
        path=root/'source.mp4';hdr=transfer!='bt709'
        primaries='bt2020' if hdr else 'bt709';matrix='bt2020nc' if hdr else 'bt709'
        args=['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=96x192:rate=60:duration=0.5',
              '-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=0.5']
        if bad_audio:args+=['-f','lavfi','-i','sine=frequency=880:sample_rate=44100:duration=0.5']
        args+=['-map','0:v:0','-map','1:a:0']
        if bad_audio:args+=['-map','2:a:0']
        args+=['-c:v','libx264','-threads','1','-g','1','-pix_fmt','yuv420p','-x264-params',
               f'colorprim={primaries}:transfer={transfer}:colormatrix={matrix}',
               '-color_trc',transfer,'-color_primaries',primaries,'-colorspace',matrix,'-c:a','aac']
        if not tail_moov:args+=['-movflags','+faststart']
        args+=[str(path)];self.run_media(args)
        self.assertEqual(d._source_hdr_state(path),hdr)
        def packets(spec):
            return json.loads(self.run_media(['ffprobe','-v','error','-select_streams',spec,
                '-show_packets','-show_entries','packet=pos,size','-of','json',str(path)]).stdout)['packets']
        video=packets('v:0');audio=packets('a:1') if bad_audio else []
        raw=bytearray(path.read_bytes());packet=video[len(video)//2]
        start,size=int(packet['pos']),int(packet['size'])
        payload=(b'\x65'+b'\xff'*(size-5)) if bad_picture else bytes(size-4)
        raw[start:start+size]=(size-4).to_bytes(4,'big')+payload
        for packet in audio:
            start,size=int(packet['pos']),int(packet['size']);raw[start:start+size]=b'\xff'*size
        if bad_duration:
            at=raw.index(b'mvhd');self.assertEqual(raw[at+4],0)
            raw[at+20:at+24]=b'\xff'*4 if bad_duration=='unknown' else bytes(4)
        path.write_bytes(raw);return path

    def frames(self,path):
        out=self.run_media(['ffmpeg','-v','quiet','-i',str(path),'-map','0:v:0',
            '-fps_mode','passthrough','-f','framemd5','-']).stdout.decode()
        return [line.split(',')[-1].strip()for line in out.splitlines()if line and not line.startswith('#')]

    def audio_hash(self,path):
        return self.run_media(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-c','copy',
            '-f','hash','-hash','sha256','-']).stdout

    def check_repair(self,source):
        before=hashlib.sha256(source.read_bytes()).hexdigest();frames=self.frames(source)
        self.assertEqual(len(frames),29)  # One fixture sample is empty, not an available picture.
        video_hash=d._source_video_packet_hash(source,remove_unspecified=True);audio=self.audio_hash(source)
        self.assertFalse(d._source_document_faststart(source))
        result=d._prepare_source_document(source,5_000_000,None)
        self.assertTrue(result.structure_modified);self.assertTrue(d._source_document_faststart(result.path))
        self.assertEqual(d._source_video_packet_hash(result.path,remove_unspecified=True),video_hash)
        self.assertEqual(self.audio_hash(result.path),audio)
        self.assertEqual(self.frames(result.path),frames)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        d._source_document_decode(result.path,None)
        meta,video,_=d._source_document_probe(result.path)
        self.assertEqual((video['width'],video['height'],video['r_frame_rate']),(96,192,'60/1'))
        return result,meta

    def test_empty_nal_with_moov_at_end_or_invalid_duration_preserves_hdr(self):
        for transfer,tail,header in [('arib-std-b67',True,False),
                ('arib-std-b67',False,True),('smpte2084',False,'unknown')]:
            with self.subTest(transfer=transfer,tail=tail,header=header), tempfile.TemporaryDirectory()as folder:
                source=self.fixture(Path(folder),transfer,tail,header)
                result,meta=self.check_repair(source)
                self.assertTrue(result.source_hdr)
                self.assertEqual(meta['streams'][0]['color_transfer'],transfer)

    def test_three_combined_faults_preserve_primary_audio_and_valid_frames(self):
        for transfer,tail,header in [('arib-std-b67',False,'unknown'),('bt709',True,False)]:
            with self.subTest(transfer=transfer), tempfile.TemporaryDirectory()as folder:
                source=self.fixture(Path(folder),transfer,tail,header,bad_audio=True)
                result,meta=self.check_repair(source)
                self.assertEqual(result.source_hdr,transfer!='bt709')
                self.assertEqual(sum(x['codec_type']=='audio'for x in meta['streams']),1)

    def test_real_picture_corruption_is_rejected_and_partial_file_is_removed(self):
        with tempfile.TemporaryDirectory()as folder:
            source=self.fixture(Path(folder),bad_duration=True,bad_picture=True)
            before=source.read_bytes()
            with self.assertRaises(d.DownloadFailedError):d._prepare_source_document(source,5_000_000,None)
            self.assertEqual(source.read_bytes(),before)
            self.assertFalse(source.with_name('source-source.mp4').exists())

    def test_cancel_during_validation_cannot_fall_through_to_repair(self):
        with tempfile.TemporaryDirectory()as folder:
            source=self.fixture(Path(folder));cancel=Event()
            def interrupted(*args,**kwargs):cancel.set();raise MediaPreparationError('cancel')
            actual=d._run_ffmpeg
            with patch.object(d,'_source_document_decode',side_effect=interrupted), \
                 patch.object(d,'_run_ffmpeg',wraps=actual)as run:
                with self.assertRaises(d.DownloadCancelledError):d._prepare_source_document(source,5_000_000,cancel)
            self.assertFalse(any('-c'in c.args[0] for c in run.call_args_list))
            self.assertFalse(source.with_name('source-source.mp4').exists())
