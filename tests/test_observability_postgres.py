"""Real PostgreSQL acknowledgement-loss/recovery check, isolated TEMP tables."""
import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from bot.observability import Journal, Job


@unittest.skipUnless(os.getenv('OPERATIONAL_TEST_DSN'),'needs isolated PostgreSQL session')
class OperationalPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_worker_survives_closed_database_and_admits_work_after_reconnect(self):
        import asyncpg
        from bot import observability as audit
        connection=await asyncpg.connect(os.environ['OPERATIONAL_TEST_DSN'])
        await connection.close()  # A real closed connection, not a mocked SQL result.
        class Pool:
            def __getattr__(self,name):return getattr(connection,name)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'journal.sqlite3'
            journal=Journal(path)
            async def send(*_):
                # This independent writer reproduces actual cross-process use.
                import sqlite3
                other=sqlite3.connect(path,timeout=.1)
                try:other.execute('BEGIN IMMEDIATE');other.commit()
                finally:other.close()
                return SimpleNamespace(message_id=77)
            bot=SimpleNamespace(send_message=send,edit_message_text=send)
            task=asyncio.create_task(audit.worker(journal,bot,Pool(),100))
            try:
                await asyncio.sleep(.1)
                self.assertFalse(task.done())
                self.assertEqual(journal.rows()[0]['state'],'open')
                # Admission and local outcomes continue while the real SQL
                # connection is closed. No work is replayed by this worker.
                self.assertEqual(journal.admit('a','scope-a',200),'accepted')
                journal.event(Job('a','scope-a',200,'download'),'delivery_unknown')
                journal.finish('a','delivery_unknown')
                connection=await asyncpg.connect(os.environ['OPERATIONAL_TEST_DSN'])
                await connection.execute('CREATE TEMP TABLE operational_events(id TEXT PRIMARY KEY,ts DOUBLE PRECISION,job TEXT,user_id BIGINT,action TEXT,status TEXT,stage TEXT,source TEXT,details JSONB)')
                for _ in range(100):
                    if (journal.sql.execute('SELECT count(*) FROM events WHERE mirrored=0').fetchone()[0]==0
                            and journal.rows()[0]['state']=='resolved'):
                        break
                    self.assertFalse(task.done())
                    await asyncio.sleep(.05)
                self.assertEqual(await connection.fetchval('SELECT status FROM operational_events'),'delivery_unknown')
                self.assertEqual(journal.rows()[0]['state'],'resolved')
                # Exercise admission after an actual idle notify/mirror loop.
                await asyncio.sleep(1.1)
                self.assertEqual(journal.admit('b','scope-b',201),'accepted')
                journal.finish('b','done')
                self.assertFalse(journal.sql.in_transaction)
            finally:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
                journal.sql.close()
                await connection.close()

    async def test_committed_batch_lost_ack_and_database_reconnect(self):
        import asyncpg
        conn=await asyncpg.connect(os.environ['OPERATIONAL_TEST_DSN'])
        with tempfile.TemporaryDirectory() as temp:
            journal=Journal(Path(temp)/'journal.sqlite3')
            try:
                # Always TEMP, even when operator runs this against the runtime
                # database. No test writes to real operational/conversion rows.
                await conn.execute('CREATE TEMP TABLE operational_events(id TEXT PRIMARY KEY,ts DOUBLE PRECISION,job TEXT,user_id BIGINT,action TEXT,status TEXT,stage TEXT,source TEXT,details JSONB)')
                journal.event(Job('a','scope',200,'download'),'delivery_unknown')
                async def lost_ack(query,rows):
                    await conn.executemany(query,rows)
                    raise ConnectionError('simulated acknowledgement loss after commit')
                with self.assertRaises(ConnectionError):
                    await journal.mirror(SimpleNamespace(execute=conn.execute,executemany=lost_ack))
                self.assertEqual(await conn.fetchval('SELECT count(*) FROM operational_events'),1)
                await journal.mirror(conn)
                self.assertEqual(await conn.fetchval('SELECT count(*) FROM operational_events'),1)
                await conn.close()
                journal.event(Job('b','scope2',200,'download'),'failed')
                with self.assertRaises(Exception):await journal.mirror(conn)
                journal.pg_ready=False
                conn=await asyncpg.connect(os.environ['OPERATIONAL_TEST_DSN'])
                await conn.execute('CREATE TEMP TABLE operational_events(id TEXT PRIMARY KEY,ts DOUBLE PRECISION,job TEXT,user_id BIGINT,action TEXT,status TEXT,stage TEXT,source TEXT,details JSONB)')
                await journal.mirror(conn)
                self.assertEqual(await conn.fetchval('SELECT status FROM operational_events'),'failed')
                self.assertEqual(journal.sql.execute('SELECT count(*) FROM events WHERE mirrored=0').fetchone()[0],0)
                await conn.execute('UPDATE operational_events SET ts=0')
                await journal.mirror(conn)
                self.assertEqual(await conn.fetchval('SELECT count(*) FROM operational_events'),0)
            finally:
                journal.sql.close()
                await conn.close()
