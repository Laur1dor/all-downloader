"""Admission, durable incidents and actual aiogram outbound failure contracts."""
import asyncio
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendVideo, SendMessage
from aiogram.types import Message, Chat, User, CallbackQuery

from bot import observability as audit
from bot.handlers.events import mark_outcome
from bot.telegram_outbound import OutgoingLimiter
from bot.urlcache import UrlCache


def message(ident=1, user=200, text='https://www.tiktok.com/@a/video/12345'):
    return Message(message_id=ident,date=int(time.time()),chat=Chat(id=user,type='private'),
                   from_user=User(id=user,is_bot=False,first_name='User'),text=text)


class OperationalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'audit.db'
        self.journal = audit.Journal(self.path,max_events=100,max_incidents=10)
        self.old = audit._current
        audit._current = self.journal
        self.data = {'event_from_user':message().from_user,'settings':SimpleNamespace(admin_id=100),
                     'url_cache':UrlCache()}

    def tearDown(self):
        audit._current = self.old
        self.journal.sql.close()
        self.temp.cleanup()

    async def test_hundred_same_links_start_one_job_and_controls_still_work(self):
        release = asyncio.Event()
        started = []
        async def handler(event,data):
            started.append(event.message_id)
            await release.wait()
            mark_outcome('done')
        first = asyncio.create_task(audit.run_event(handler,message(),dict(self.data)))
        await asyncio.sleep(0)
        await asyncio.gather(*(audit.run_event(handler,message(i),dict(self.data)) for i in range(2,102)))
        control = AsyncMock()
        cb = CallbackQuery(id='cancel1',from_user=message().from_user,chat_instance='x',data='cancel:x',message=message())
        await audit.run_event(control,cb,dict(self.data))
        control.assert_awaited_once()
        release.set(); await first
        self.assertEqual(started,[1])
        self.assertEqual(self.journal.sql.execute("SELECT count(*) FROM claims").fetchone()[0],1)
        self.assertLessEqual(self.journal.sql.execute('SELECT count(*) FROM events').fetchone()[0],100)

    async def test_double_click_file_same_source_tokens_starts_once(self):
        cache = self.data['url_cache']
        token = cache.store('https://www.tiktok.com/@a/video/12345')
        release = asyncio.Event()
        handler = AsyncMock(side_effect=lambda *_: None)
        async def slow(*_):
            await release.wait()
        handler.side_effect = slow
        def click(i):
            return CallbackQuery(id=str(i),from_user=message().from_user,chat_instance='x',data='ttfile:'+token,message=message())
        first = asyncio.create_task(audit.run_event(handler,click(1),dict(self.data)))
        await asyncio.sleep(0)
        await asyncio.gather(*(audit.run_event(handler,click(i),dict(self.data)) for i in range(2,102)))
        release.set();await first
        self.assertEqual(handler.await_count,1)

    def test_global_capacity_and_persisted_quota(self):
        for i in range(16):
            self.assertEqual(self.journal.admit(str(i),str(i),i),'accepted')
        self.assertEqual(self.journal.admit('17','17',17),'capacity')
        self.journal.finish('0','done')
        self.assertEqual(self.journal.admit('new','new',17),'accepted')
        for i in range(1,16):self.journal.finish(str(i),'failed')
        for i in range(2):
            self.assertEqual(self.journal.admit(f'a{i}',f'a{i}',99),'accepted')
            self.journal.finish(f'a{i}','failed')
        self.assertEqual(self.journal.admit('a2','a2',99),'accepted')
        self.journal.finish('a2','failed')
        self.assertEqual(self.journal.admit('a3','a3',99),'quota')

    def test_restart_retains_ids_and_interrupted_is_not_success(self):
        self.journal.admit('id','scope',200)
        self.journal.event(audit.Job('id','scope',200,'download','tiktok','upload'),'delivered',delivered=2)
        self.journal.block(200)
        self.journal.sql.close()
        self.journal = audit.Journal(self.path,max_events=100,max_incidents=10)
        audit._current = self.journal
        self.assertEqual(self.journal.admit('id','scope',200),'duplicate_update')
        self.assertEqual(self.journal.rows()[0]['category'],'interrupted')
        self.assertEqual(self.journal.rows()[0]['state'],'open')
        self.assertEqual(self.journal.rows()[0]['delivered'],2)
        self.assertEqual(self.journal.rows()[0]['source'],'tiktok')
        self.assertTrue(self.journal.is_blocked(200))

    async def test_lifecycle_only_matching_confirmed_success_resolves_then_reopens(self):
        async def failure(*_):mark_outcome('failed')
        async def success(*_):mark_outcome('done')
        await audit.run_event(failure,message(1),dict(self.data))
        await audit.run_event(success,message(2,text='https://www.tiktok.com/@a/video/99999'),dict(self.data))
        self.assertEqual(self.journal.rows()[0]['state'],'open')
        await audit.run_event(success,message(3),dict(self.data))
        self.assertEqual(self.journal.rows()[0]['state'],'resolved')
        # Refill quota and bypass only the brief successful double-click window.
        self.journal.sql.execute('DELETE FROM buckets')
        self.journal.sql.execute("UPDATE claims SET ts=ts-6 WHERE state='done'");self.journal.sql.commit()
        await audit.run_event(failure,message(4),dict(self.data))
        self.assertEqual(self.journal.rows()[0]['state'],'reopened')
        self.assertEqual(self.journal.rows()[0]['count'],2)

    async def test_partial_delivery_then_forbidden_stops_rest_of_work(self):
        job = audit.Job('id','scope',200,'download',stage='upload')
        token = audit._job.set(job)
        try:
            limiter = OutgoingLimiter()
            method = SendVideo(chat_id=200,video='file-id')
            send = AsyncMock(return_value=message())
            cancel = __import__('threading').Event();audit.attach_cancel(cancel)
            with patch.object(limiter,'_admit',new=AsyncMock()):
                await limiter(send,None,method)
                send.side_effect = TelegramForbiddenError(method,'bot was blocked')
                with self.assertRaises(TelegramForbiddenError):await limiter(send,None,method)
                with self.assertRaises(TelegramForbiddenError):await limiter(send,None,method)
            self.assertEqual(send.await_count,2)
            self.assertEqual(job.delivered,1)
            self.assertTrue(cancel.is_set())
            with self.assertRaises(audit.ChatBlocked):audit.ensure_available()
            self.assertTrue(self.journal.is_blocked(200))
            self.journal.incident(job,'blocked',100)
            self.journal.allow_inbound(200)
            self.assertEqual(self.journal.rows()[0]['state'],'open')
            job.blocked=False;send.side_effect=None
            with patch.object(limiter,'_admit',new=AsyncMock()):
                await limiter(send,None,SendMessage(chat_id=200,text='hello'))
            self.assertEqual(self.journal.rows()[0]['state'],'resolved')
        finally:audit._job.reset(token)

    async def test_retry_after_finite_and_does_not_raise_service_incident(self):
        limiter=OutgoingLimiter();method=SendVideo(chat_id=200,video='file-id')
        send=AsyncMock(side_effect=TelegramRetryAfter(method,'wait',retry_after=1))
        with patch.object(limiter,'_admit',new=AsyncMock()):
            with self.assertRaises(TelegramRetryAfter):await limiter(send,None,method)
        self.assertEqual(send.await_count,2)
        self.assertEqual(len(self.journal.rows()),0)

    async def test_duplicate_update_after_restart_has_no_handler_side_effect(self):
        handler=AsyncMock()
        await audit.run_event(handler,message(),dict(self.data))
        self.journal.sql.close();self.journal=audit.Journal(self.path);audit._current=self.journal
        await audit.run_event(handler,message(),dict(self.data))
        self.assertEqual(handler.await_count,1)

    def test_journal_caps_and_overflow_preserve_failure_counters(self):
        for i in range(1000):
            job=audit.Job(str(i),str(i),200,'download')
            self.journal.event(job,'delivery_unknown')
            self.journal.incident(job,'delivery_unknown',100)
        self.assertEqual(self.journal.sql.execute('SELECT count(*) FROM events').fetchone()[0],100)
        self.assertEqual(len(self.journal.rows(50)),10)
        counts=dict(self.journal.sql.execute('SELECT name,value FROM counters'))
        self.assertEqual(counts['delivery_unknown'],1000)
        self.assertEqual(counts['incident_overflow'],990)
        self.assertEqual(counts['mirror_overflow'],900)

    async def test_database_outage_recovery_idempotent_mirror(self):
        self.journal.event(audit.Job('id','scope',200,'download'),'delivery_unknown')
        pool=SimpleNamespace(execute=AsyncMock(side_effect=OSError('offline')),executemany=AsyncMock())
        with self.assertRaises(OSError):await self.journal.mirror(pool)
        self.assertEqual(self.journal.sql.execute('SELECT sum(mirrored) FROM events').fetchone()[0],0)
        pool.execute.side_effect=None
        # Simulate insert success but lost database acknowledgement.
        stored={}
        async def insert(query,rows):
            for row in rows:stored[row[0]]=row
            if len(pool.executemany.await_args_list)==1:raise ConnectionError('lost acknowledgement')
        pool.executemany.side_effect=insert
        with self.assertRaises(ConnectionError):await self.journal.mirror(pool)
        await self.journal.mirror(pool)
        self.assertEqual(len(stored),1)
        self.assertEqual(self.journal.sql.execute('SELECT sum(mirrored) FROM events').fetchone()[0],1)

    async def test_notifications_edit_group_and_rate_limit(self):
        job=audit.Job('id','scope',200,'download',stage='upload')
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),edit_message_text=AsyncMock())
        for _ in range(100):self.journal.incident(job,'failed',100)
        await self.journal.notify(bot,100)
        await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_count,1)
        self.assertIn('Повторений: 100',bot.send_message.await_args.args[1])
        self.journal.resolve('scope')
        self.journal.next_alert=0
        self.journal.sql.execute('UPDATE incidents SET next_notice=0');self.journal.sql.commit()
        await self.journal.notify(bot,100)
        bot.edit_message_text.assert_awaited_once()
        self.assertIn('Восстановлено',bot.edit_message_text.await_args.args[0])

    async def test_different_operations_share_one_alert_but_resolve_independently(self):
        self.journal.max_incidents=200
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),edit_message_text=AsyncMock())
        self.journal.incident(audit.Job('0','scope0',200,'download','tiktok','upload',reason='TimeoutError'),'failed',100)
        await self.journal.notify(bot,100)
        for i in range(1,100):
            self.journal.incident(audit.Job(str(i),f'scope{i}',200+i,'download','tiktok','upload',reason='TimeoutError'),'failed',100)
        self.journal.resolve('scope0')
        self.journal.next_alert=0
        self.journal.sql.execute('UPDATE incidents SET next_notice=0');self.journal.sql.commit()
        await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(bot.edit_message_text.await_count,1)
        self.assertIn('открыто 99, восстановлено 1',bot.edit_message_text.await_args.args[0])
        self.assertEqual(self.journal.sql.execute("SELECT count(*) FROM incidents WHERE state='resolved'").fetchone()[0],1)

    async def test_alert_budget_is_durable_across_restart(self):
        self.journal.max_incidents=100
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
        for i in range(10):
            self.journal.incident(audit.Job(str(i),str(i),200,'download',reason=f'Cause{i}'),'failed',100)
        for _ in range(10):
            self.journal.next_alert=0
            await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_count,6)
        self.journal.sql.close();self.journal=audit.Journal(self.path);audit._current=self.journal
        await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_count,6)

    def test_worker_thread_warnings_have_bounded_queue_and_overflow_counter(self):
        import threading
        def warn():
            handler=audit.AuditLog()
            for _ in range(1000):
                handler.emit(logging.LogRecord('bot.downloader',logging.WARNING,'path',3,'https://secret',(),None))
        thread=threading.Thread(target=warn);thread.start();thread.join()
        self.assertEqual(self.journal.logs.qsize(),256)
        audit.drain_logs(self.journal)
        self.assertEqual(self.journal.sql.execute("SELECT value FROM counters WHERE name='warning_overflow'").fetchone()[0],744)

    async def test_lost_notification_response_is_not_replayed_even_after_restart(self):
        self.journal.incident(audit.Job('id','scope',200,'download'),'failed',100)
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=ConnectionError('lost')))
        await self.journal.notify(bot,100)
        self.journal.sql.close();self.journal=audit.Journal(self.path);audit._current=self.journal
        self.journal.sql.execute('UPDATE incidents SET next_notice=0');self.journal.sql.commit()
        await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(self.journal.rows()[0]['message'],-1)

    def test_log_export_and_alert_never_contain_secret_or_signed_url(self):
        job=audit.Job('id','scope',200,'download')
        token=audit._job.set(job)
        try:
            handler=audit.AuditLog()
            record=logging.LogRecord('bot.downloader',logging.WARNING,'path',3,
                    'secret https://cdn.example/file?token=12345 123456:AAA-secret',(),None)
            handler.emit(record)
            self.journal.incident(job,'failed',100)
            payload=self.journal.export(self.journal.rows()[0]['id']).decode()
            events=''.join(r[0] for r in self.journal.sql.execute('SELECT details FROM events'))
            self.assertNotIn('secret',payload+events)
            self.assertNotIn('https://',payload+events)
            self.assertFalse(json.loads(payload)['automatic_repair'])
        finally:audit._job.reset(token)

    async def test_expected_input_rejection_is_audited_without_owner_incident(self):
        from bot.urlguard import BlockedAddressError
        async def handler(*_):
            try:raise BlockedAddressError('internal')
            except BlockedAddressError:mark_outcome('failed')
        await audit.run_event(handler,message(),dict(self.data))
        self.assertEqual(len(self.journal.rows()),0)
        self.assertEqual(self.journal.sql.execute("SELECT value FROM counters WHERE name='rejected'").fetchone()[0],1)

    async def test_cancel_preserves_interrupted_outcome_and_releases_capacity(self):
        async def handler(*_):await asyncio.Event().wait()
        task=asyncio.create_task(audit.run_event(handler,message(),dict(self.data)))
        await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.journal.rows()[0]['category'],'interrupted')
        self.assertEqual(self.journal.sql.execute("SELECT count(*) FROM claims WHERE state='running'").fetchone()[0],0)

    async def test_statistics_failure_cannot_reclassify_confirmed_delivery(self):
        from bot.handlers.download import _record
        db=SimpleNamespace(add_conversion=AsyncMock(side_effect=ConnectionError('offline')))
        async def handler(*_):
            audit._job.get().delivered=1
            await _record(db,200,'done','tiktok','video',time.monotonic(),123)
        await audit.run_event(handler,message(),dict(self.data))
        self.assertEqual(len(self.journal.rows()),0)
        self.assertEqual(self.journal.sql.execute("SELECT state FROM claims").fetchone()[0],'done')

    def test_rollout_drain_defers_active_work_and_atomically_pauses_new_jobs(self):
        import runpy
        drain=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/operational-drain.py'))['drain']
        self.journal.admit('active','active',200)
        self.assertFalse(drain(self.path,timeout=0))
        self.assertFalse(self.path.with_suffix('.drain').exists())
        self.journal.finish('active','done')
        self.assertTrue(drain(self.path,timeout=0))
        self.assertEqual(self.journal.admit('new','new',201),'draining')
        self.path.with_suffix('.drain').write_text('0')
        self.assertEqual(self.journal.admit('new','new',201),'accepted')

    async def test_local_journal_outage_refuses_download_but_allows_cancellation(self):
        import sqlite3
        handler=AsyncMock()
        with patch.object(self.journal,'seen',side_effect=sqlite3.OperationalError('disk unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                await audit.run_event(handler,message(),dict(self.data))
            handler.assert_not_awaited()
            cb=CallbackQuery(id='cancel2',from_user=message().from_user,chat_instance='x',data='cancel:x',message=message())
            await audit.run_event(handler,cb,dict(self.data))
        handler.assert_awaited_once()

    async def test_deadline_child_task_preserves_outcome_in_legacy_statistics(self):
        from bot.handlers.events import EventMiddleware
        db=SimpleNamespace(record_event=AsyncMock())
        data={**self.data,'db':db}
        async def handler(*_):mark_outcome('failed')
        await EventMiddleware()(handler,message(),data)
        self.assertEqual(db.record_event.await_args.args[2],'failed')

    def test_database_monitor_is_labelled_service_operation_not_owner_download(self):
        self.journal.incident(audit.Job('mirror','system:postgres',100,'mirror'),'failed',100)
        self.assertIn('Контекст: служебная операция',audit.format_incident(self.journal.rows()[0],100))

    async def test_idle_worker_and_rejection_paths_release_transaction_for_other_connection(self):
        import sqlite3
        bot=SimpleNamespace(send_message=AsyncMock(),edit_message_text=AsyncMock())
        # This exact empty-notification path held a write transaction in the
        # first live rollout, blocking both BEGIN IMMEDIATE and other writers.
        await self.journal.notify(bot,100)
        self.assertFalse(self.journal.sql.in_transaction)
        other=sqlite3.connect(self.path,timeout=.05)
        try:
            other.execute('BEGIN IMMEDIATE');other.commit()
            self.assertEqual(self.journal.admit('id','scope',200),'accepted')
            self.journal.finish('id','done')
            self.assertFalse(self.journal.seen('x'))
            self.assertTrue(self.journal.seen('x'))
            self.assertFalse(self.journal.sql.in_transaction)
            self.assertTrue(self.journal.tell_rejection(200))
            self.assertFalse(self.journal.tell_rejection(200))
            self.assertFalse(self.journal.sql.in_transaction)
            other.execute('BEGIN IMMEDIATE');other.commit()
        finally:other.close()

    async def test_notification_network_await_does_not_lock_other_writers(self):
        import sqlite3
        self.journal.incident(audit.Job('id','scope',200,'download'),'failed',100)
        async def send(*_,**__):
            other=sqlite3.connect(self.path,timeout=.05)
            try:other.execute('BEGIN IMMEDIATE');other.commit()
            finally:other.close()
            return SimpleNamespace(message_id=77)
        await self.journal.notify(SimpleNamespace(send_message=send),100)
        self.assertFalse(self.journal.sql.in_transaction)


    async def test_separate_alert_chat_persists_and_does_not_reuse_private_message_ids(self):
        self.journal.incident(audit.Job('id','scope',200,'download'),'failed',100)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),edit_message_text=AsyncMock())
        await self.journal.notify(bot,100)
        self.journal.set_alert_chat(-123,100)
        await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_args.args[0],-123)
        bot.edit_message_text.assert_not_awaited()
        self.journal.sql.close();self.journal=audit.Journal(self.path);audit._current=self.journal
        self.assertEqual(self.journal.alert_chat(100),-123)
        self.journal.resolve('scope');self.journal.next_alert=0
        await self.journal.notify(bot,100)
        self.assertEqual(bot.edit_message_text.await_args.kwargs['chat_id'],-123)
        self.assertIn('✅ Восстановлено',bot.edit_message_text.await_args.args[0])
        self.assertEqual(bot.send_message.await_count,2)

    async def test_successful_edit_lost_ack_then_not_modified_never_creates_duplicate(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        self.journal.incident(audit.Job('id','scope',200,'download'),'failed',100)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),edit_message_text=AsyncMock())
        await self.journal.notify(bot,100)
        self.journal.resolve('scope');self.journal.next_alert=0
        bot.edit_message_text.side_effect=TelegramBadRequest(EditMessageText(text='x',chat_id=100,message_id=77),'Bad Request: message is not modified')
        await self.journal.notify(bot,100)
        row=self.journal.rows()[0]
        self.assertEqual(row['revision'],row['sent_revision'])
        self.assertEqual(row['message'],77)
        self.assertEqual(bot.send_message.await_count,1)


    async def test_group_binding_is_owner_only_and_does_not_expose_dashboard(self):
        from aiogram import Bot,Dispatcher
        from aiogram.types import Update,ChatMemberOwner
        from aiogram.dispatcher.event.bases import UNHANDLED
        from bot.handlers.analytics import create_router
        bot=Bot('123456:'+'a'*35)
        async def response(_bot,method,**kwargs):
            if method.__api_method__=='getChatMember':
                return ChatMemberOwner(user=User(id=100,is_bot=False,first_name='Owner'),is_anonymous=False)
            if method.__api_method__=='getChatMemberCount':return 2
            return Message(message_id=77,date=int(time.time()),chat=Chat(id=-123,type='group'))
        bot.session.make_request=AsyncMock(side_effect=response)
        dispatcher=Dispatcher();dispatcher.include_router(create_router(100))
        def update(ident,user,text):
            return Update(update_id=ident,message=Message(message_id=ident,date=int(time.time()),chat=Chat(id=-123,type='group'),from_user=User(id=user,is_bot=False,first_name='Test'),text=text))
        try:
            self.assertIs(await dispatcher.feed_update(bot,update(1,200,'/errors_here')),UNHANDLED)
            self.assertEqual(self.journal.alert_chat(100),100)
            await dispatcher.feed_update(bot,update(2,100,'/errors_here'))
            self.assertEqual(self.journal.alert_chat(100),-123)
            self.assertIs(await dispatcher.feed_update(bot,update(3,100,'/dashboard')),UNHANDLED)
            await dispatcher.feed_update(bot,update(4,100,'/errors'))
            self.assertTrue(bot.session.make_request.await_args.args[1].text.startswith('Операционный журнал'))
        finally:await bot.session.close()


    async def test_chat_change_during_notification_cannot_attach_old_chat_message_id(self):
        self.journal.incident(audit.Job('id','scope',200,'download'),'failed',100)
        async def send(*args,**kwargs):
            if args[0]==100:self.journal.set_alert_chat(-123,100)
            return SimpleNamespace(message_id=77)
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=send),edit_message_text=AsyncMock())
        await self.journal.notify(bot,100)
        self.assertIsNone(self.journal.rows()[0]['message'])
        await self.journal.notify(bot,100)
        self.assertEqual(bot.send_message.await_args.args[0],-123)
        bot.edit_message_text.assert_not_awaited()
