"""Reproducible owner-only diagnostics without signed URLs or input-noise alerts."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from urllib.error import HTTPError

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendVideo
from aiogram.types import CallbackQuery, Chat, Message, User

from bot import observability as audit
from bot.handlers.events import EventMiddleware, mark_outcome
from bot.incident_context import clean_context, expected_input_error, public_entry_url, user_link
from bot.urlcache import UrlCache

URL='https://www.tiktok.com/@author/video/123456789'


class PublicEntryTests(unittest.TestCase):
    def test_supported_services_preserve_only_public_identifiers(self):
        cases={
            'https://vt.tiktok.com/ZSpublic/?token=PRIVATE':'https://vt.tiktok.com/ZSpublic/',
            URL+'?secret=PRIVATE#PRIVATE':URL,
            'https://youtu.be/abcdef12345?si=PRIVATE':'https://www.youtube.com/watch?v=abcdef12345',
            'https://www.youtube.com/watch?v=abcdef12345&access_token=PRIVATE':'https://www.youtube.com/watch?v=abcdef12345',
            'https://instagram.com/reel/ABC123/?igsh=PRIVATE':'https://www.instagram.com/reel/ABC123/',
            'https://twitter.com/someone/status/12345?signature=PRIVATE':'https://x.com/i/status/12345',
            'https://rule34.xxx/index.php?page=post&s=view&id=123&token=PRIVATE':'https://rule34.xxx/index.php?page=post&s=view&id=123',
            'https://rule34video.com/video/123/title/?key=PRIVATE':'https://rule34video.com/video/123/',
            'https://www.pornhub.com/view_video.php?viewkey=phABC123&token=PRIVATE':'https://www.pornhub.com/view_video.php?viewkey=phABC123',
            'https://www.pinterest.com/pin/12345/?token=PRIVATE':'https://www.pinterest.com/pin/12345/',
            'https://soundcloud.com/artist/track?token=PRIVATE':'https://soundcloud.com/artist/track',
            'https://open.spotify.com/track/ABC123?si=PRIVATE':'https://open.spotify.com/track/ABC123',
            'https://music.yandex.ru/album/123/track/456?uid=PRIVATE':'https://music.yandex.ru/album/123/track/456',
            'https://the-joi-database.com/public-post/?token=PRIVATE':'https://the-joi-database.com/public-post/',
        }
        for raw,expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(public_entry_url(raw),expected)
                self.assertEqual(public_entry_url(expected),expected)

    def test_credentials_private_hosts_cdn_signed_links_and_malformed_input_are_not_saved(self):
        values=['letters','http://[broken','https://username:PRIVATE@www.tiktok.com/@author/video/123',
                'https://127.0.0.1/video/123','file:///etc/passwd',
                'https://v16-webapp-prime.tiktok.com/video/PRIVATE.mp4?signature=PRIVATE',
                'https://tiktok.com.attacker.example/@a/video/123','https://youtube.com/login?token=PRIVATE',
                'https://youtube.com/login?v=abcdef12345','https://tiktok.com:80/@a/video/123',
                'https://www.tiktok.com/@a/video/123:99','https://www.tiktok.com/@a/video/123<script>',
                'https://www.tiktok.com:9443/@a/video/123','https://soundcloud.com/a/sets']
        for value in values:
            with self.subTest(value=value):self.assertEqual(public_entry_url(value),'')

    def test_profile_links_are_explicit_and_escaped(self):
        self.assertIn('href="tg://user?id=200"',user_link(200))
        self.assertIn('href="https://t.me/test_user"',user_link(200,'test_user'))
        self.assertIn('href="tg://user?id=200"',user_link(200,'"<script>'))
        self.assertNotIn('<script>',user_link(200,'"<script>'))
        self.assertEqual(user_link(0),'служебная операция')

    def test_metadata_is_allowlisted_and_bounded(self):
        context=clean_context({'url':URL+'?token=PRIVATE','operation':'file','time_range':(20,40),
                               'height':1080,'username':'test_user','body':'PRIVATE','headers':'PRIVATE','cookie':'PRIVATE'})
        self.assertEqual(context,{'url':URL,'operation':'file','time_range':[20,40],'height':1080,'username':'test_user'})
        self.assertEqual(clean_context({'operation':{},'telegram_method':[],'url':None,'height':True}),{})
        self.assertNotIn('time_range',clean_context({'time_range':[0,999999999]}))

    def test_only_confirmed_public_page_not_found_is_input_refusal(self):
        gone=HTTPError(URL,404,'not found',{},None)
        cdn=HTTPError('https://cdn.example/file.mp4?token=PRIVATE',404,'not found',{},None)
        self.assertTrue(expected_input_error(gone,kind='supported_link',url=URL,stage='download'))
        self.assertTrue(expected_input_error(gone,kind='supported_link',url='https://vt.tiktok.com/ZSpublic/',resolved_url=URL,stage='download'))
        self.assertFalse(expected_input_error(cdn,kind='supported_link',url=URL,stage='download'))
        self.assertFalse(expected_input_error(gone,kind='supported_link',url=URL,stage='upload'))
        self.assertFalse(expected_input_error(Exception('format unavailable'),kind='supported_link',url=URL,stage='download'))

    def test_temporary_callback_response_is_not_a_media_delivery_failure(self):
        stale=TelegramBadRequest(AnswerCallbackQuery(callback_query_id='old'),'query is too old')
        media=TelegramBadRequest(SendVideo(chat_id=200,video='file-id'),'failed to parse media')
        self.assertTrue(expected_input_error(stale,kind='callback',stage='accepted'))
        self.assertFalse(expected_input_error(media,kind='supported_link',stage='upload'))


class IncidentReproductionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'audit.db'
        self.journal=audit.Journal(self.path)
        self.old=audit._current;audit._current=self.journal
        self.user=User(id=200,first_name='User',is_bot=False,username='test_user')
        self.data={'event_from_user':self.user,'settings':SimpleNamespace(admin_id=100),'url_cache':UrlCache()}

    def tearDown(self):
        audit._current=self.old;self.journal.sql.close();self.temp.cleanup()

    def message(self,ident=1,text=URL):
        return Message(message_id=ident,date=int(time.time()),chat=Chat(id=200,type='private'),from_user=self.user,text=text)

    async def test_failure_saves_reproduction_and_notifies_only_owner_with_html_links(self):
        from bot.handlers.download import DeliveryUnknownError
        async def handler(*_):
            audit.request_context(resolved_url=URL+'?signature=PRIVATE',operation='video',time_range=(20,40),height=1080,telegram_method='sendVideo')
            audit.stage('upload')
            try:
                try:raise TimeoutError('PRIVATE transport details')
                except TimeoutError as exc:raise DeliveryUnknownError('PRIVATE') from exc
            except DeliveryUnknownError:mark_outcome('delivery_unknown')
        await audit.run_event(handler,self.message(text='https://vt.tiktok.com/ZSpublic/?secret=PRIVATE'),dict(self.data))
        row=self.journal.rows()[0]
        self.assertEqual(row['category'],'delivery_unknown')
        payload=json.loads(self.journal.export(row['id']))
        self.assertEqual(payload['context']['url'],'https://vt.tiktok.com/ZSpublic/')
        self.assertEqual(payload['context']['resolved_url'],URL)
        self.assertEqual(payload['context']['cause_class'],'TimeoutError')
        self.assertNotIn('PRIVATE',json.dumps(payload))
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
        await self.journal.notify(bot,100)
        call=bot.send_message.await_args
        self.assertEqual(call.args[0],100)
        self.assertEqual(call.kwargs['parse_mode'],'HTML')
        self.assertTrue(call.kwargs['link_preview_options'].is_disabled)
        self.assertIn('href="https://t.me/test_user"',call.args[1])
        self.assertIn('href="https://vt.tiktok.com/ZSpublic/"',call.args[1])
        self.assertIn('00:00:20–00:00:40',call.args[1]);self.assertIn('1080p',call.args[1])
        self.assertLess(len(call.args[1]),4096)

    async def test_file_callback_retains_link_but_not_callback_token(self):
        cache=self.data['url_cache'];token=cache.store(URL)
        cb=CallbackQuery(id='file1',from_user=self.user,chat_instance='x',data='ttfile:'+token,message=self.message())
        async def handler(*_):audit.stage('prepare');mark_outcome('failed')
        await audit.run_event(handler,cb,dict(self.data))
        payload=self.journal.export(self.journal.rows()[0]['id']).decode()
        context=json.loads(payload)['context']
        self.assertEqual(context['operation'],'file');self.assertEqual(context['url'],URL)
        self.assertNotIn(token,payload)

    async def test_expected_bad_input_is_rejected_without_owner_alert_or_failed_statistics(self):
        from bot.handlers.download import _record
        from bot.downloader import DownloadFailedError
        for ident,text in enumerate(['letters','https://unsupported.example/post','https://tiktok.com/not-a-post','http://[bad'],1):
            db=SimpleNamespace(add_conversion=AsyncMock(),record_event=AsyncMock())
            async def handler(*_):
                try:raise DownloadFailedError('bad input')
                except DownloadFailedError:await _record(db,200,'failed','other','video',time.monotonic())
            await EventMiddleware()(handler,self.message(ident,text),{**self.data,'db':db})
            self.assertEqual(db.add_conversion.await_args.args[1],'rejected')
            self.assertEqual(db.record_event.await_args.args[2],'rejected')
        self.assertEqual(len(self.journal.rows()),0)
        self.assertIsNone(self.journal.sql.execute("SELECT value FROM counters WHERE name='failed'").fetchone())

    async def test_real_cdn_or_media_rejection_remains_an_incident(self):
        from bot.downloader import DownloadFailedError
        async def handler(*_):
            audit.stage('download')
            try:raise DownloadFailedError('CDN unavailable')
            except DownloadFailedError:mark_outcome('failed')
        await audit.run_event(handler,self.message(),dict(self.data))
        self.assertEqual(self.journal.rows()[0]['category'],'failed')

    def test_restart_recovers_original_link_and_options(self):
        job=audit.Job('id','scope',200,'download','tiktok','upload',context={'url':URL,'operation':'file','height':2160,'username':'test_user'})
        self.journal.admit('id','scope',200);self.journal.event(job,'delivered',delivered=1)
        self.journal.sql.close();self.journal=audit.Journal(self.path);audit._current=self.journal
        row=self.journal.rows()[0]
        self.assertEqual(row['category'],'interrupted')
        self.assertEqual(json.loads(row['context']),job.context)
        self.assertEqual(row['delivered'],1)

    def test_old_schema_refreshes_profile_links_without_inventing_a_source(self):
        self.journal.incident(audit.Job('id','scope',200,'download','tiktok','upload'),'delivery_unknown',100)
        self.journal.sql.execute('UPDATE incidents SET message=77,sent_revision=revision');self.journal.sql.commit()
        self.journal.sql.close()
        c=sqlite3.connect(self.path);c.execute('ALTER TABLE incidents DROP COLUMN context');c.commit();c.close()
        self.journal=audit.Journal(self.path);audit._current=self.journal
        row=self.journal.rows()[0]
        self.assertGreater(row['revision'],row['sent_revision'])
        text=audit.format_incident(row,100)
        self.assertIn('href="tg://user?id=200"',text)
        self.assertIn('не сохранена прежней версией',text)
        self.assertEqual(row['state'],'open')

    def test_group_export_preserves_every_members_source(self):
        for i in range(3):
            self.journal.incident(audit.Job(str(i),str(i),200+i,'download','tiktok','upload',context={'url':URL+str(i),'operation':'video'}),'failed',100)
        payload=json.loads(self.journal.export(self.journal.rows()[0]['id']))
        self.assertEqual(len(payload['related']),3)
        self.assertEqual({r['context']['url']for r in payload['related']},{URL+str(i)for i in range(3)})

    def test_restart_does_not_turn_unsupported_input_into_an_incident(self):
        job=audit.Job('id','scope',200,'download','other','resolve',input_kind='unsupported_link')
        self.journal.admit('id','scope',200);self.journal.event(job,'started')
        self.journal.sql.close();self.journal=audit.Journal(self.path);audit._current=self.journal
        self.assertEqual(len(self.journal.rows()),0)
        self.assertEqual(self.journal.sql.execute('SELECT state FROM claims').fetchone()[0],'rejected')
