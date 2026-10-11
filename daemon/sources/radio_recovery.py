"""Control-owned recovery tickets and an async directory lookup supervisor."""
import asyncio
from dataclasses import dataclass, field
import logging

from ..errors import DomainError
from ..radio_browser import http_url

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 300
PERSIST_RETRY_SECONDS = 30
LOOKUP_DEADLINE_SECONDS = 60


@dataclass
class Ticket:
    source: object
    pipeline: object
    revision: tuple
    token: object = field(default_factory=object)
    persist_after: float = 0.0
    lookup_deadline: float = 0.0


class SourceRadioRecovery:
    """Only called on the existing control/SQLite executor."""
    def __init__(self, manager):
        self.manager = manager
        self.pending = {}
        self.cooldowns = {}
        self.closed = False

    def invalidate(self, source_id):
        ticket = self.pending.pop(source_id, None)
        if ticket:
            ticket.pipeline.radio_url.discard(ticket.token)
            self.cooldowns[source_id] = self.manager.clock() + COOLDOWN_SECONDS

    def valid(self, ticket):
        manager, source = self.manager, ticket.source
        return (not self.closed and manager.pipelines.get(source.id) is ticket.pipeline
                and bool(manager.consumers.get(source.id))
                and source.id not in manager.restart_errors
                and manager.database.source_revision(source.id) == ticket.revision)

    def collect(self, lookup_slots=8):
        manager, now = self.manager, self.manager.clock()
        for source_id, ticket in list(self.pending.items()):
            if not self.valid(ticket):
                self.invalidate(source_id)
                log.info('Radio Browser recovery superseded for Source %s', source_id)
                continue
            _, trial = ticket.pipeline.radio_url.observe()
            if trial is None:  # Also bound failed/lost control handoffs.
                if now >= ticket.lookup_deadline:
                    self.invalidate(source_id)
                    log.warning('Radio Browser lookup expired for Source %s', source_id)
                continue
            if trial['state'] in ('failed', 'obsolete'):
                self.invalidate(source_id)
                log.info('Radio Browser candidate not accepted for Source %s', source_id)
            elif trial['state'] == 'pcm' and now >= ticket.persist_after:
                try:
                    timestamp = manager.database.replace_radio_browser_url(source_id, ticket.revision,
                        ticket.source.config['radio_browser_uuid'], ticket.source.config['uri'], trial['uri'])
                except Exception:
                    # Keep confirmed audio playing. Retry only the guarded write;
                    # never lookup/probe/restart the stream because storage failed.
                    ticket.persist_after = now + PERSIST_RETRY_SECONDS
                    log.error('Radio Browser Source %s is playing a recovered URL but persistence failed; guarded write will retry', source_id)
                    continue
                if timestamp is None:
                    self.invalidate(source_id)
                    log.info('Radio Browser recovery superseded for Source %s', source_id)
                    continue
                ticket.pipeline.source = ticket.source.model_copy(update={
                    'config': ticket.source.config | {'uri': trial['uri']}, 'timestamp': timestamp})
                ticket.pipeline.radio_url.accept(ticket.token)
                self.pending.pop(source_id)
                self.cooldowns[source_id] = now + COOLDOWN_SECONDS
                log.info('Radio Browser recovery confirmed by decoded PCM and persisted for Source %s', source_id)
        tickets = []
        if self.closed:
            return tickets
        self.cooldowns = {sid: until for sid, until in self.cooldowns.items() if until > now}
        for source_id, pipeline in list(manager.pipelines.items()):
            state = getattr(pipeline, 'radio_url', None)
            if (state is None or source_id in self.pending or source_id in self.cooldowns
                    or not manager.consumers.get(source_id) or source_id in manager.restart_errors
                    or not state.observe()[0]):
                continue
            if len(tickets) >= lookup_slots:
                break
            source = manager.database.get('sources', source_id)
            if source.type != 'HTTP' or not source.config.get('radio_browser_uuid'):
                continue
            ticket = Ticket(source, pipeline, manager.database.source_revision(source_id),
                            lookup_deadline=now + LOOKUP_DEADLINE_SECONDS)
            self.pending[source_id] = ticket
            self.cooldowns[source_id] = now + COOLDOWN_SECONDS
            tickets.append(ticket)
            log.info('Radio Browser recovery lookup for Source %s', source_id)
        return tickets

    def resolved(self, ticket, station):
        sid = ticket.source.id
        if self.pending.get(sid) is not ticket:
            return
        if not self.valid(ticket):
            self.invalidate(sid)
            return
        candidate = station.playable_url if station is not None else None
        if (station is None or str(station.stationuuid) != ticket.source.config['radio_browser_uuid']
                or not http_url(candidate) or candidate == ticket.source.config['uri']
                or not ticket.pipeline.radio_url.offer(ticket.token, candidate)):
            self.invalidate(sid)
            log.info('Radio Browser lookup supplied no new usable candidate for Source %s', sid)
            return
        log.info('Radio Browser candidate queued in existing retry loop for Source %s', sid)

    def close(self):
        self.closed = True
        for source_id in list(self.pending):
            self.invalidate(source_id)


class RadioRecoveryTask:
    """ASGI-loop supervisor. HTTP awaits never occupy control or media workers."""
    def __init__(self, control, recovery, client):
        self.control, self.recovery, self.client = control, recovery, client
        self.lookups = set()
        self.task = None

    async def lookup(self, ticket):
        try:
            station = await self.client.lookup(ticket.source.config['radio_browser_uuid'])
        except Exception:
            log.warning('Radio Browser unavailable during recovery of Source %s', ticket.source.id)
            station = None
        while True:
            try:
                await self.control.call(lambda: self.recovery.resolved(ticket, station))
                return
            except DomainError as exc:
                if exc.error != 'CONTROL_BUSY':
                    raise
                await asyncio.sleep(0.25)

    def lookup_done(self, task):
        self.lookups.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error('Radio Browser recovery handoff failed')

    async def run(self):
        while True:
            try:
                tickets = await self.control.call(lambda: self.recovery.collect(lookup_slots=max(0, 8 - len(self.lookups))))
                for ticket in tickets:
                    task = asyncio.create_task(self.lookup(ticket))
                    self.lookups.add(task)
                    task.add_done_callback(self.lookup_done)
            except DomainError:
                pass
            except Exception:
                log.error('Radio Browser recovery observation failed; will retry')
            await asyncio.sleep(1)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        tasks = list(self.lookups)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
