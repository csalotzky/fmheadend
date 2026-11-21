"""Persistent rotation history, using the repository's SQLite connection/owner."""
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import islice

from .podcast import utc


_EPISODE_BATCH_SIZE = 500


@dataclass(frozen=True)
class EpisodeHistory:
    play_count: int = 0
    last_played_at: datetime | None = None
    last_seen_at: datetime | None = None


class PodcastHistory:
    """Use on the Database owner thread. Runtime I/O owns a separate connection.

    Scheduling consumes a detached snapshot, never a shared SQLite connection.
    Playback calls mark_played once after its successful-PCM threshold;
    selection, source tests and refresh do not count as playback.
    """
    def __init__(self, database, source_id: int):
        self.database, self.source_id = database, source_id

    def snapshot(self) -> dict[tuple[str, str], EpisodeHistory]:
        rows = self.database.connection.execute(
            'SELECT * FROM podcast_history WHERE source_id = ? AND play_count > 0', (self.source_id,))
        return dict(self._entries(rows))

    def snapshot_for_episodes(
        self, keys: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], EpisodeHistory]:
        """Read durable history for the supplied feed and episode identities."""
        by_feed: dict[str, set[str]] = defaultdict(set)
        for feed_id, episode_id in keys:
            by_feed[feed_id].add(episode_id)
        if not by_feed:
            return {}

        connection = self.database.connection
        # SELECT does not open a persistent transaction in sqlite3's legacy
        # transaction mode. Keep all batches on one read view.
        own_transaction = not connection.in_transaction
        if own_transaction:
            connection.execute('BEGIN')
        try:
            state = {}
            for feed_id, episode_ids in by_feed.items():
                remaining = iter(episode_ids)
                while batch := tuple(islice(remaining, _EPISODE_BATCH_SIZE)):
                    placeholders = ', '.join('?' for _ in batch)
                    rows = connection.execute(
                        'SELECT * FROM podcast_history WHERE source_id = ? AND feed_id = ? '
                        f'AND episode_id IN ({placeholders}) AND play_count > 0',
                        (self.source_id, feed_id, *batch))
                    state.update(self._entries(rows))
            return state
        finally:
            if own_transaction:
                connection.rollback()

    @staticmethod
    def _entries(rows):
        for row in rows:
            played_at = row['last_played_at']
            yield (row['feed_id'], row['episode_id']), EpisodeHistory(
                row['play_count'], datetime.fromisoformat(played_at) if played_at else None,
                datetime.fromisoformat(row['last_seen_at']))

    def mark_played(self, episode, played_at: datetime):
        stamp = utc(played_at).isoformat()
        seen = utc(episode.last_seen_at).isoformat()
        with self.database.connection:
            self.database.connection.execute(
                'INSERT INTO podcast_history '
                '(source_id, feed_id, episode_id, play_count, last_played_at, last_seen_at) '
                'VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(source_id, feed_id, episode_id) DO UPDATE SET '
                'play_count = play_count + 1, last_played_at = excluded.last_played_at, '
                'last_seen_at = MAX(last_seen_at, excluded.last_seen_at)',
                (self.source_id, episode.feed_id, episode.episode_id, stamp, seen))
