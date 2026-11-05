"""Bounded journal; only sanitized public entry links, never bodies/credentials.

SQLite is authoritative for admission and incidents. PostgreSQL is an idempotent
mirror, never a prerequisite for telemetry. No model or automatic code repair.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import queue
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from bot.incident_context import clean_context, expected_input_error, public_entry_url, reproduction, user_link

_current = None
_job: ContextVar[Job | None] = ContextVar("operational_job", default=None)
_silent = ContextVar("operational_silent", default=False)
STAGES = {"accepted": "приём задания", "resolve": "поиск источника",
          "download": "скачивание", "prepare": "подготовка файла",
          "upload": "отправка", "queue": "ожидание очереди", "paused": "выбор пользователя"}
REASONS = {"failed": "Операция завершилась ошибкой", "delivery_unknown": "Telegram не подтвердил доставку",
           "interrupted": "Процесс перезапущен до подтверждения результата",
           "blocked": "Пользователь запретил боту отправку сообщений",
           "warning": "Зафиксировано предупреждение; окончательный результат указан ниже"}
CAUSES = {
    'MediaPreparationError':'Полученный файл не прошёл подготовку или проверку целостности.',
    'DownloadFailedError':'Источник не вернул пригодный результат; подробная причина не подтверждена.',
    'TelegramNetworkError':'Сетевая связь с Telegram прервалась.',
    'DeliveryUnknownError':'Ответ Telegram потерян; нельзя утверждать, что сообщение не было доставлено.',
    'TelegramForbiddenError':'Telegram запретил отправку в этот чат; оставшаяся работа отменена.',
    'TimeoutError':'Истёк установленный срок операции.',
    'ConnectionError':'Соединение оборвалось или не было установлено.',
    'ConnectionDoesNotExistError':'Соединение с базой данных потеряно.',
    'CannotConnectNowError':'База данных временно не принимает подключения.',
}


@dataclass
class Job:
    id: str
    scope: str
    user: int
    action: str
    source: str = "other"
    stage: str = "accepted"
    delivered: int = 0
    result: str | None = None
    reason: str = ""
    blocked: bool = False
    cancels: list = field(default_factory=list)
    context: dict = field(default_factory=dict)
    input_kind: str = 'unknown'


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def safe_code(value: str) -> str:
    # Codes only. Never render exception text, paths or signed source addresses.
    return "".join(c for c in str(value) if c.isascii() and (c.isalnum() or c in "_.:-"))[:80]


class Journal:
    def __init__(self, path: Path, *, max_events=20000, max_incidents=1000):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sql = sqlite3.connect(path, timeout=.05)
        path.chmod(0o600)
        self.sql.row_factory = sqlite3.Row
        self.sql.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA journal_size_limit=2097152;
        PRAGMA wal_autocheckpoint=128;
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, ts REAL, job TEXT, user INTEGER,
          action TEXT, status TEXT, stage TEXT, source TEXT, details TEXT, mirrored INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS event_mirror ON events(mirrored,ts);
        CREATE INDEX IF NOT EXISTS event_job ON events(job,ts);
        CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, scope TEXT, category TEXT,
          stage TEXT, source TEXT, user INTEGER, own INTEGER, state TEXT, count INTEGER,
          first REAL, last REAL, restored REAL, delivered INTEGER, reason TEXT,
          revision INTEGER DEFAULT 1, sent_revision INTEGER DEFAULT 0,
          message INTEGER, next_notice REAL DEFAULT 0, attempts INTEGER DEFAULT 0,
          context TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS alert_destination(id INTEGER PRIMARY KEY CHECK(id=1), chat INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY, work TEXT, user INTEGER,
          state TEXT, ts REAL);
        CREATE INDEX IF NOT EXISTS claim_work ON claims(work,state);
        CREATE TABLE IF NOT EXISTS blocked(user INTEGER PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS buckets(user INTEGER PRIMARY KEY, tokens REAL, ts REAL);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY,ts REAL);
        CREATE TABLE IF NOT EXISTS notice_new(ts REAL);
        CREATE TABLE IF NOT EXISTS rejection_notices(user INTEGER PRIMARY KEY,ts REAL);
        CREATE INDEX IF NOT EXISTS incident_cohort ON incidents(category,stage,source,reason);
        CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
        """)
        self.max_events, self.max_incidents = max_events, max_incidents
        self.next_alert = 0.0
        self.pg_ready = False
        self.logs = queue.Queue(maxsize=256)
        self.log_overflow = 0
        self._writes = 0
        if 'context' not in {r['name'] for r in self.sql.execute('PRAGMA table_info(incidents)')}:
            self.sql.execute("ALTER TABLE incidents ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
            # Refresh existing owner messages with real profile anchors. Missing
            # historic URLs stay explicitly unknown, never guessed or fabricated.
            self.sql.execute('UPDATE incidents SET revision=revision+1 WHERE message>0')
            self.sql.commit()
        # Never replay uncertain work after restart. Stable claims still suppress
        # the same Telegram update; a new user request is a new operation.
        for row in self.sql.execute("SELECT * FROM claims WHERE state='running'").fetchall():
            job = Job(row['id'], row['work'], row['user'], 'restart')
            last = self.sql.execute('SELECT action,source,stage,details FROM events WHERE job=? ORDER BY ts DESC LIMIT 1',(row['id'],)).fetchone()
            if last:
                job.action,job.source,job.stage = last['action'],last['source'],last['stage']
                details=json.loads(last['details'])
                job.context=clean_context(details.get('context',{}))
                job.input_kind=details.get('input_kind','unknown')
                job.delivered = self.sql.execute("SELECT coalesce(max(CAST(json_extract(details,'$.delivered') AS INTEGER)),0) FROM events WHERE job=?",(row['id'],)).fetchone()[0]
            job.reason = 'ProcessRestart'
            if not job.delivered and expected_input_error(None,kind=job.input_kind,stage=job.stage):
                self.event(job,'rejected',result='interrupted')
                self.sql.execute("UPDATE claims SET state='rejected' WHERE id=?",(row['id'],))
            else:self.incident(job, 'interrupted', admin_id=0)
        self.sql.execute("UPDATE claims SET state='interrupted' WHERE state='running'")
        self.sql.execute("UPDATE incidents SET message=-1 WHERE message=-2")
        self.sql.commit()

    def count(self, name: str, amount=1):
        self.sql.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                         (safe_code(name), amount))

    def event(self, job: Job, status: str, **details):
        allowed = {k: v for k, v in details.items() if k in {'delivered', 'duration_ms', 'error_class', 'location', 'parts', 'bytes', 'result'}
                   and isinstance(v, (int, float, str, bool))}
        allowed = {k: safe_code(v) if isinstance(v, str) else v for k, v in allowed.items()}
        context=clean_context(job.context)
        if context:allowed['context']=context
        if job.input_kind in {'supported_link','unsupported_link','invalid_link','text','command','callback'}:
            allowed['input_kind']=job.input_kind
        self.sql.execute("INSERT INTO events(id,ts,job,user,action,status,stage,source,details) VALUES(?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, time.time(), job.id, job.user, safe_code(job.action),
                          safe_code(status), safe_code(job.stage), safe_code(job.source), json.dumps(allowed)))
        self.count(status)
        self._writes += 1
        # Count/trim per insert rather than occasional unbounded overshoot.
        excess = self.sql.execute("SELECT count(*) FROM events").fetchone()[0] - self.max_events
        if excess > 0:
            lost = self.sql.execute("SELECT count(*) FROM (SELECT mirrored FROM events ORDER BY ts LIMIT ?) WHERE mirrored=0", (excess,)).fetchone()[0]
            self.count('mirror_overflow', lost)
            self.sql.execute("DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY ts LIMIT ?)", (excess,))
        self.sql.commit()

    def admit(self, ident: str, work: str, user: int, *, admin=False) -> str:
        # Serialize drain activation with the complete admission decision.
        self.sql.execute('BEGIN IMMEDIATE')
        try:
            marker=self.path.with_suffix('.drain')
            if marker.exists():
                try:
                    if float(marker.read_text()) > time.time():
                        return 'draining'
                except (OSError,ValueError):
                    # An interrupted marker write must not pause work forever.
                    if time.time()-marker.stat().st_mtime < 90:
                        return 'draining'
            return self._admit(ident,work,user,admin=admin)
        finally:
            self.sql.commit()

    def _admit(self, ident: str, work: str, user: int, *, admin=False) -> str:
        now = time.time()
        self.sql.execute("DELETE FROM claims WHERE state!='running' AND ts<?", (now-86400,))
        if self.sql.execute("SELECT 1 FROM claims WHERE id=?", (ident,)).fetchone():
            return 'duplicate_update'
        if self.sql.execute("SELECT 1 FROM claims WHERE work=? AND state='running'", (work,)).fetchone():
            return 'duplicate_job'
        if self.sql.execute("SELECT 1 FROM claims WHERE work=? AND state='done' AND ts>?",(work,now-5)).fetchone():
            return 'duplicate_job'
        total = self.sql.execute("SELECT count(*) FROM claims WHERE state='running'").fetchone()[0]
        own = self.sql.execute("SELECT count(*) FROM claims WHERE state='running' AND user=?", (user,)).fetchone()[0]
        if total >= 16 or own >= (2 if admin else 1):
            return 'capacity'
        # Persisted work rate budget, including owner; cancellation/control
        # callbacks do not pass through this work gate.
        row = self.sql.execute("SELECT tokens,ts FROM buckets WHERE user=?", (user,)).fetchone()
        tokens = min(3., row[0]+max(0, now-row[1])/20) if row else 3.
        if tokens < 1:
            return 'quota'
        self.sql.execute("INSERT OR REPLACE INTO buckets VALUES(?,?,?)", (user,tokens-1,now))
        self.sql.execute("DELETE FROM buckets WHERE user IN (SELECT user FROM buckets ORDER BY ts DESC LIMIT -1 OFFSET 5000)")
        if self.sql.execute("SELECT count(*) FROM claims").fetchone()[0] >= 10000:
            # A rolling replay window must not turn one day's traffic into a
            # global outage. Active jobs are never evicted.
            self.sql.execute("DELETE FROM claims WHERE id IN (SELECT id FROM claims WHERE state!='running' ORDER BY ts LIMIT 1)")
            self.count('job_dedup_evicted')
        self.sql.execute("INSERT INTO claims VALUES(?,?,?,'running',?)", (ident,work,user,now))
        self.sql.commit()
        return 'accepted'

    def tell_rejection(self,user):
        now=time.time()
        self.sql.execute('DELETE FROM rejection_notices WHERE ts<?',(now-60,))
        self.sql.commit()
        if self.sql.execute('SELECT count(*) FROM rejection_notices').fetchone()[0]>=10:
            return False
        if self.sql.execute('SELECT 1 FROM rejection_notices WHERE user=?',(user,)).fetchone():
            return False
        self.sql.execute('INSERT OR REPLACE INTO rejection_notices VALUES(?,?)',(user,now))
        self.sql.commit()
        return True

    def finish(self, ident: str, state: str):
        self.sql.execute("UPDATE claims SET state=?,ts=? WHERE id=?", (safe_code(state),time.time(),ident))
        self.sql.commit()

    def seen(self, ident: str) -> bool:
        now = time.time()
        self.sql.execute("DELETE FROM seen WHERE ts<?", (now-86400,))
        if self.sql.execute("SELECT 1 FROM seen WHERE id=?",(ident,)).fetchone():
            self.sql.commit()
            return True
        if self.sql.execute("SELECT count(*) FROM seen").fetchone()[0] >= 10000:
            self.sql.execute("DELETE FROM seen WHERE id IN (SELECT id FROM seen ORDER BY ts LIMIT 1)")
            self.count('update_dedup_evicted')
        self.sql.execute("INSERT INTO seen VALUES(?,?)",(ident,now))
        self.sql.commit()
        return False

    def block(self, user: int):
        self.sql.execute("INSERT OR REPLACE INTO blocked VALUES(?,?)", (user,time.time()))
        self.sql.execute("DELETE FROM blocked WHERE user IN (SELECT user FROM blocked ORDER BY ts DESC LIMIT -1 OFFSET 5000)")
        self.sql.commit()

    def is_blocked(self, user: int) -> bool:
        return bool(self.sql.execute("SELECT 1 FROM blocked WHERE user=?", (user,)).fetchone())

    def allow_inbound(self, user: int):
        # A new incoming update allows one new attempt, but does NOT resolve
        # a blocked-delivery incident. Only an actual successful send does.
        self.sql.execute("DELETE FROM blocked WHERE user=?", (user,))
        self.sql.commit()

    def incident(self, job: Job, category: str, admin_id: int):
        category = category if category in REASONS else 'failed'
        scope = f'chat:{job.user}' if category == 'blocked' else job.scope
        ident = digest(scope + ':' + category + ':' + job.stage)
        existing = self.sql.execute("SELECT 1 FROM incidents WHERE id=?", (ident,)).fetchone()
        if not existing and self.sql.execute("SELECT count(*) FROM incidents").fetchone()[0] >= self.max_incidents:
            self.sql.execute("DELETE FROM incidents WHERE id IN (SELECT id FROM incidents WHERE state='resolved' "
                             "AND revision=sent_revision ORDER BY last LIMIT 1)")
        if not existing and self.sql.execute("SELECT count(*) FROM incidents").fetchone()[0] >= self.max_incidents:
            # Keep active incidents and aggregate excess failures; never claim
            # that this unattributed aggregate has been repaired.
            self.count('incident_overflow')
            self.sql.commit()
            return
        now = time.time()
        self.sql.execute("""INSERT INTO incidents(id,scope,category,stage,source,user,own,state,count,first,last,delivered,reason,context)
          VALUES(?,?,?,?,?,?,?,'open',1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          state=CASE WHEN incidents.state='resolved' THEN 'reopened' ELSE incidents.state END,
          count=count+1,last=excluded.last,user=excluded.user,delivered=excluded.delivered,
          reason=excluded.reason,context=excluded.context,revision=revision+1""",
          (ident,scope,category,job.stage,safe_code(job.source),job.user,int(job.user==admin_id),
           now,now,job.delivered,safe_code(job.reason),json.dumps(clean_context(job.context))))
        peer = self.sql.execute("SELECT message,next_notice FROM incidents WHERE id!=? AND category=? "
                                "AND stage=? AND source=? AND reason=? AND message IS NOT NULL ORDER BY last DESC LIMIT 1",
                                (ident,category,job.stage,safe_code(job.source),safe_code(job.reason))).fetchone()
        if peer:
            self.sql.execute("UPDATE incidents SET message=?,next_notice=? WHERE id=? AND message IS NULL",
                             (peer['message'],peer['next_notice'],ident))
        self.sql.commit()

    def resolve(self, scope: str):
        self.sql.execute("UPDATE incidents SET state='resolved',restored=?,revision=revision+1,next_notice=0 "
                         "WHERE scope=? AND state!='resolved'", (time.time(),scope))
        self.sql.commit()

    def alert_chat(self, admin_id: int) -> int:
        row = self.sql.execute('SELECT chat FROM alert_destination WHERE id=1').fetchone()
        return row['chat'] if row else admin_id

    def set_alert_chat(self, chat_id: int, admin_id: int):
        if self.alert_chat(admin_id) == chat_id:
            return
        # Message IDs belong to their chat. Never edit an old ID in a new chat.
        self.sql.execute('INSERT INTO alert_destination VALUES(1,?) ON CONFLICT(id) DO UPDATE SET chat=excluded.chat',(chat_id,))
        self.sql.execute('UPDATE incidents SET message=NULL,revision=revision+1,next_notice=0,attempts=0')
        self.sql.commit()
        self.next_alert = 0

    def rows(self, limit=10):
        return self.sql.execute("SELECT * FROM incidents ORDER BY last DESC LIMIT ?", (min(limit,50),)).fetchall()

    def export(self, ident: str) -> bytes:
        row = self.sql.execute("SELECT * FROM incidents WHERE id=?", (ident,)).fetchone()
        if not row:
            return b'{}'
        result = dict(row)
        result['context']=clean_context(json.loads(result['context']))
        cohort=(row['category'],row['stage'],row['source'],row['reason'])
        result['related']=[{**dict(r),'context':clean_context(json.loads(r['context']))}
                           for r in self.sql.execute('SELECT id,user,state,count,context FROM incidents WHERE category=? AND stage=? AND source=? AND reason=? ORDER BY last DESC',cohort)]
        # Safe future handoff only: no code execution/model network calls.
        result['automatic_repair'] = False
        result['required_gates'] = ['isolated_patch', 'regressions', 'CI', 'owner_release', 'rollback']
        return json.dumps(result,ensure_ascii=False,indent=2).encode()

    async def mirror(self, pool):
        if not self.pg_ready:
            await pool.execute("""CREATE TABLE IF NOT EXISTS operational_events(
              id TEXT PRIMARY KEY,ts DOUBLE PRECISION NOT NULL,job TEXT,user_id BIGINT,
              action TEXT,status TEXT,stage TEXT,source TEXT,details JSONB);
              CREATE INDEX IF NOT EXISTS operational_events_ts ON operational_events(ts);""")
            self.pg_ready = True
        rows = self.sql.execute("SELECT * FROM events WHERE mirrored=0 ORDER BY ts LIMIT 100").fetchall()
        if rows:
            await pool.executemany("INSERT INTO operational_events VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb) ON CONFLICT(id) DO NOTHING",
                                  [(r['id'],r['ts'],r['job'],r['user'],r['action'],r['status'],r['stage'],r['source'],r['details']) for r in rows])
            self.sql.executemany("UPDATE events SET mirrored=1 WHERE id=?", [(r['id'],) for r in rows])
            self.sql.commit()
        # Fixed retention and a hard row bound. Mirror is diagnostic, not the
        # existing conversion/statistics database.
        await pool.execute("DELETE FROM operational_events WHERE id IN (SELECT id FROM operational_events "
                           "WHERE ts < extract(epoch from now())-2592000 ORDER BY ts LIMIT 1000)")
        await pool.execute("DELETE FROM operational_events WHERE id IN (SELECT id FROM operational_events "
                           "ORDER BY ts DESC OFFSET 100000 LIMIT 1000)")

    async def notify(self, bot, admin_id: int):
        from aiogram.types import LinkPreviewOptions
        destination = self.alert_chat(admin_id)
        if time.time() < self.next_alert:
            return
        now = time.time()
        self.sql.execute("DELETE FROM notice_new WHERE ts<?",(now-600,))
        # Even a DELETE matching zero rows starts a SQLite write transaction.
        # Never retain it across an empty-queue return or a network await.
        self.sql.commit()
        new_allowed = self.sql.execute("SELECT count(*) FROM notice_new").fetchone()[0] < 6
        row = self.sql.execute("SELECT * FROM incidents WHERE revision>sent_revision AND next_notice<=? "
                               "AND (message>0 OR (message IS NULL AND ?)) ORDER BY last LIMIT 1", (now,new_allowed)).fetchone()
        if not row:
            return
        self.next_alert = time.time()+60
        revision = row['revision']
        cohort = (row['category'],row['stage'],row['source'],row['reason'])
        try:
            pending = self.sql.execute("SELECT count(*) FROM (SELECT 1 FROM incidents WHERE revision>sent_revision GROUP BY category,stage,source,reason)").fetchone()[0]
            members = self.sql.execute("SELECT * FROM incidents WHERE category=? AND stage=? AND source=? AND reason=?",cohort).fetchall()
            opened = sum(m['state']!='resolved' for m in members)
            examples=[m for m in members if m['state']!='resolved'] or members
            grouped = dict(max(examples,key=lambda m:m['last']))
            grouped.update(count=sum(m['count'] for m in members),first=min(m['first'] for m in members),
                           last=max(m['last'] for m in members),delivered=sum(m['delivered'] for m in members),
                           state='open' if opened else 'resolved')
            text = format_incident(grouped,admin_id)+f'\nОпераций с этой причиной: {len(members)}; открыто {opened}, восстановлено {len(members)-opened}.'
            text += f'\nГрупп в очереди сводки: {pending}. /errors — журнал и счётчики.'
            if len(members)>1:
                text+=f"\nВсе ссылки и участники: /errors {row['id']}"
            overflow = self.sql.execute("SELECT value FROM counters WHERE name='incident_overflow'").fetchone()
            if overflow:
                text += f'\nОтказов сверх лимита отдельных групп: {overflow[0]} (сохранены в счётчиках).'
            if row['message']:
                await bot.edit_message_text(text,chat_id=destination,message_id=row['message'],
                                           parse_mode='HTML',link_preview_options=LinkPreviewOptions(is_disabled=True))
                message = row['message']
            else:
                # Reserve the durable budget BEFORE the ambiguous network call.
                self.sql.execute("INSERT INTO notice_new VALUES(?)",(time.time(),))
                self.sql.execute("UPDATE incidents SET message=-2 WHERE category=? AND stage=? AND source=? AND reason=?",cohort)
                self.sql.commit()
                sent = await bot.send_message(destination,text,parse_mode='HTML',
                                              link_preview_options=LinkPreviewOptions(is_disabled=True))
                message = sent.message_id
        except asyncio.CancelledError:
            if not row['message']:
                self.sql.execute("UPDATE incidents SET message=-1 WHERE category=? AND stage=? AND source=? AND reason=?",cohort)
                self.count('notification_delivery_unknown')
                self.sql.commit()
            raise
        except Exception as exc:
            if destination != self.alert_chat(admin_id):
                return
            # Update a deleted original on next pass. No immediate send fallback,
            # and no automatic retry of an ambiguous network send.
            from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
            if isinstance(exc,TelegramBadRequest) and row['message'] and 'message is not modified' in str(exc).lower():
                self.sql.executemany('UPDATE incidents SET sent_revision=?,attempts=0,next_notice=? WHERE id=?',
                                     [(m['revision'],time.time()+300,m['id']) for m in members])
                self.sql.commit()
                return
            if isinstance(exc,TelegramBadRequest):
                self.sql.execute("UPDATE incidents SET message=NULL WHERE category=? AND stage=? AND source=? AND reason=?",cohort)
            delay = min(3600,30*2**min(row['attempts'],7))
            if isinstance(exc,TelegramRetryAfter):
                delay = max(delay,float(exc.retry_after))
            if isinstance(exc,TelegramForbiddenError):
                delay = 3600
            if isinstance(exc,(TelegramForbiddenError,TelegramRetryAfter)) and not row['message']:
                self.sql.execute("UPDATE incidents SET message=NULL WHERE category=? AND stage=? AND source=? AND reason=?",cohort)
            if not isinstance(exc,(TelegramBadRequest,TelegramForbiddenError,TelegramRetryAfter)) and not row['message']:
                # An upload may have succeeded before its response was lost.
                # Suppress automatic replay; /errors still exposes the incident.
                self.sql.execute("UPDATE incidents SET sent_revision=revision,message=-1 WHERE category=? AND stage=? AND source=? AND reason=?",cohort)
                self.count('notification_delivery_unknown')
            self.sql.execute("UPDATE incidents SET next_notice=?,attempts=attempts+1 WHERE category=? AND stage=? AND source=? AND reason=?", (time.time()+delay,*cohort))
            self.count('notification_failed')
        else:
            if destination != self.alert_chat(admin_id):
                return
            # Only acknowledge the cohort snapshot rendered above; a new error
            # during the network await must still result in a subsequent edit.
            self.sql.executemany("UPDATE incidents SET sent_revision=?,message=?,attempts=0,next_notice=? WHERE id=?",
                                 [(m['revision'],message,time.time()+300,m['id']) for m in members])
            self.sql.execute("UPDATE incidents SET message=?,next_notice=? WHERE category=? AND stage=? AND source=? AND reason=? AND message=-2",
                             (message,time.time()+300,*cohort))
        self.sql.commit()


def format_incident(row, admin_id=0):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    stamp = lambda t: datetime.fromtimestamp(t,ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M:%S')
    state = {'open':'🔴 Открыт','resolved':'✅ Восстановлено','reopened':'🔴 Повторился'}[row['state']]
    context = ('служебная операция' if row['user']==0 or row['scope'].startswith('system:')
               else 'ваша задача' if row['user']==admin_id else 'задача другого пользователя')
    restored = ('PostgreSQL снова отвечает на запросы журнала.' if row['scope']=='system:postgres'
                else 'Telegram подтвердил отправку сообщения в этот чат.' if row['category']=='blocked'
                else 'Доставка подтверждена повторной успешной операцией.')
    outcome = (restored if row['state']=='resolved'
               else f"Подтверждено отправок медиа: {row['delivered']}. " +
               ('Итоговая доставка неизвестна; повтор автоматически не запускается.' if row['category'] in {'delivery_unknown','interrupted'}
                else 'Полный успешный результат не подтверждён.'))
    data=dict(row)
    request=clean_context(json.loads(data.get('context') or '{}'))
    service=row['user']==0 or row['scope'].startswith('system:')
    person='' if service else '; '+user_link(row['user'],request.get('username',''))
    replay='' if service or (row['source'] in {'other','telegram'} and not request.get('operation')) else '\n'+reproduction(request)
    return (f"{state} · {REASONS[row['category']]}\n"
            f"Этап: {STAGES.get(row['stage'],'обработка действия')}. Источник: {safe_code(row['source'])}.\n"
            f"Контекст: {context}{person}.\n{outcome}{replay}\n"
            f"Повторений: {row['count']}. Первое: {stamp(row['first'])}; последнее: {stamp(row['last'])} МСК.\n"
            f"Причина: {CAUSES.get(row['reason'],'Обработанный отказ; точная причина не подтверждена.')} "
            f"Класс: {safe_code(row['reason']) or 'не указан'}.\n"
            f"Диагностика: {row['id']}\n"
            "Проверить /errors. Исправление подтверждается только успешным повторением этой операции.")


def current():
    return _current


def record(status: str, **details):
    job = _job.get()
    if _current and job and not _silent.get():
        try:
            _current.event(job,status,**details)
        except sqlite3.Error:
            # Fail closed for admission, fail open for logging an existing job.
            pass


def stage(name: str):
    job = _job.get()
    if job and name in STAGES and job.stage != name:
        job.stage = name
        record('stage')


def outcome(value: str):
    job = _job.get()
    if job:
        job.result = value
        record(value,delivered=job.delivered)


def ensure_available():
    job = _job.get()
    if job and job.blocked:
        raise ChatBlocked("Chat delivery unavailable")


class ChatBlocked(Exception):
    pass


def attach_cancel(event):
    job = _job.get()
    if job:
        job.cancels.append(event)
        if job.blocked:
            event.set()


def set_source(source):
    job = _job.get()
    if job:
        job.source = safe_code(source or 'other')


def request_context(**values):
    job=_job.get()
    if job:
        job.context=clean_context({**job.context,**values})
        if job.context.get('url'):job.input_kind='supported_link'
        record('request_context')


def refused_input(error) -> bool:
    job=_job.get()
    return bool(job and expected_input_error(error,kind=job.input_kind,
                url=job.context.get('url',''),resolved_url=job.context.get('resolved_url',''),stage=job.stage))


async def run_event(handler, event, data):
    """Called before router handlers. Control callbacks bypass the work quota."""
    from aiogram.types import CallbackQuery, Message
    from aiogram.exceptions import TelegramForbiddenError
    from bot.urlkey import canonical_key, _platform
    from urllib.parse import urlparse
    journal = _current
    user = data.get('event_from_user')
    if not journal or not user:
        return await handler(event,data)
    callback = isinstance(event,CallbackQuery)
    from bot.handlers.events import action_name
    action = action_name(event)
    payload = (event.data or '') if callback else (getattr(event,'text',None) or '')
    work = (payload.startswith(('http://','https://')) if not callback
            else payload.startswith(('audio:','ttfile:')))
    ident = digest(f'callback:{event.id}' if callback else f'message:{event.chat.id}:{event.message_id}')
    try:
        duplicate = journal.seen(ident)
    except sqlite3.Error:
        if work:
            data['_audit_refused'] = True
            raise
        # Cancellation and read-only owner commands must remain usable if the
        # local journal itself is unavailable. Heavy work still fails closed.
        return await handler(event,data)
    if duplicate:
        journal.count('duplicate_update'); journal.sql.commit()
        data['_audit_refused'] = True
        return None
    journal.allow_inbound(user.id)
    url = payload.split(maxsplit=1)[0] if not callback and payload else ''
    if callback:
        cache = data.get('url_cache')
        url = cache.get(payload.split(':',1)[-1]) if cache else ''
    try:
        source = _platform(urlparse(url or '').hostname or '')
        material = canonical_key(url) if url else payload
    except ValueError:
        source='other';material=payload
    # Timing/quality suffix must not collide with the whole video operation.
    if not callback and ' ' in payload:
        material += ':'+payload.split(maxsplit=1)[1]
    scope = digest(f'{user.id}:{action}:{material}')
    settings = data.get('settings')
    admin = getattr(settings,'admin_id',0)
    job = Job(ident,scope,user.id,action,source or 'other')
    link=public_entry_url(url or '')
    job.input_kind=('supported_link' if link else 'invalid_link' if source and source!='other'
                    else 'unsupported_link' if work else 'callback' if callback
                    else 'command' if payload.startswith('/') else 'text')
    job.context=clean_context({'url':link,'username':getattr(user,'username',None),
                              'operation':{'ttfile':'file','audio':'audio','desc':'description'}.get(payload.split(':',1)[0],'video') if work or link else None})
    token = _job.set(job)
    admitted = False
    started = time.monotonic()
    try:
        if work:
            decision = journal.admit(ident,scope,user.id,admin=user.id==admin)
            record(decision)
            if decision != 'accepted':
                # No reply to every refused update: flood middleware is the
                # only bounded user-facing notice. Counts remain queryable.
                data['_audit_refused'] = True
                job.result = decision
                if journal.tell_rejection(user.id):
                    text = ('Бот обновляется. Повторите запрос через минуту.' if decision=='draining'
                            else 'Задание уже выполняется или достигнут лимит. Дождитесь завершения и повторите позже.')
                    try:
                        if callback:
                            await event.answer(text)
                        else:
                            await event.answer(text)
                    except Exception:
                        record('rejection_notice_failed')
                return None
            admitted = True
        record('started')
        try:
            if work:
                return await asyncio.wait_for(handler(event,data),2400)
            return await handler(event,data)
        except asyncio.CancelledError:
            job.result = 'interrupted'
            record('interrupted',delivered=job.delivered)
            raise
        except Exception as exc:
            job.reason = type(exc).__name__
            if refused_input(exc):
                job.result='rejected'
                record('rejected',error_class=job.reason)
                return None
            job.result = 'blocked' if isinstance(exc,(ChatBlocked,TelegramForbiddenError)) else 'failed'
            if exc.__cause__:request_context(cause_class=type(exc.__cause__).__name__)
            record(job.result,delivered=job.delivered,error_class=job.reason)
            raise
    finally:
        try:
            result = 'blocked' if job.blocked else job.result or 'ok'
            data['_operational_outcome'] = result
            record('finished',duration_ms=int((time.monotonic()-started)*1000),delivered=job.delivered,result=result)
            if result in {'failed','delivery_unknown','interrupted','blocked'}:
                journal.incident(job,result,admin)
            elif result == 'done':
                journal.resolve(job.scope)
            if admitted:
                journal.finish(ident,result)
            if result in {'interrupted','blocked'}:
                for cancel in job.cancels:
                    cancel.set()
        finally:
            _job.reset(token)


def install(path: Path):
    global _current
    _current = Journal(path)
    handler = AuditLog(level=logging.WARNING)
    logging.getLogger().addHandler(handler)
    return _current, handler


class AuditLog(logging.Handler):
    def emit(self, entry):
        if _silent.get() or not entry.name.startswith(('bot.', 'aiogram.')):
            return
        if entry.levelno >= logging.WARNING:
            job = _job.get()
            if job and entry.exc_info and entry.exc_info[0]:
                job.reason = entry.exc_info[0].__name__
            if _current:
                # The downloader also logs from worker threads. Queue only
                # sanitized metadata, never LogRecord/traceback/URL references.
                reason = entry.exc_info[0].__name__ if entry.exc_info and entry.exc_info[0] else 'Warning'
                location = safe_code(f'{entry.name}:{entry.funcName}:{entry.lineno}')
                if not job:
                    job = Job('system',digest(f'{entry.name}:{entry.funcName}'),0,'system',reason=reason)
                else:
                    # Keep the stage at warning time, not the later stage when
                    # the worker drains its bounded logging queue.
                    job = Job(job.id,job.scope,job.user,job.action,job.source,
                              job.stage,job.delivered,reason=reason,context=clean_context(job.context))
                try:
                    _current.logs.put_nowait((job,reason,location,entry.levelno))
                except queue.Full:
                    _current.log_overflow += 1


def drain_logs(journal):
    for _ in range(256):
        try:
            job,reason,location,level = journal.logs.get_nowait()
        except queue.Empty:
            break
        journal.event(job,'warning',error_class=reason,location=location)
        if job.id == 'system' and level >= logging.ERROR and reason not in {
                'TelegramBadRequest','TelegramForbiddenError','TelegramRetryAfter'}:
            journal.incident(job,'failed',0)
    if journal.log_overflow:
        journal.count('warning_overflow',journal.log_overflow)
        journal.log_overflow = 0
        journal.sql.commit()


async def worker(journal, bot, pool, admin_id):
    delay = 1
    while True:
        token = _silent.set(True)
        try:
            drain_logs(journal)
            await asyncio.wait_for(journal.mirror(pool),10)
            journal.resolve('system:postgres')
            delay = 1
        except Exception as exc:
            journal.pg_ready = False
            job = Job('mirror','system:postgres',admin_id,'mirror',reason=type(exc).__name__)
            try:
                journal.incident(job,'failed',admin_id)
            except sqlite3.Error:
                pass
            delay = min(60,delay*2)
        try:
            await asyncio.wait_for(journal.notify(bot,admin_id),30)
        except Exception:
            # A telemetry delivery failure must not kill polling or recursively
            # create an alert about sending its own alert.
            pass
        finally:
            _silent.reset(token)
        await asyncio.sleep(delay)
