"""Persistent rotation history, using the repository's SQLite connection/owner."""
from dataclasses import dataclass
from datetime import datetime

from .podcast import utc


@dataclass(frozen=True)
class EpisodeHistory:
    play_count: int = 0
    last_played_at: datetime | None = None
    last_seen_at: datetime | None = None


class PodcastHistory:
    """Use on the Database owner thread (the control executor in the daemon).

    Scheduling consumes a detached snapshot, never a shared SQLite connection.
    Future playback calls mark_played once after its successful-play threshold;
    selection, source tests and refresh do not count as playback.
    """
    def __init__(self, database, source_id: int):
        self.database, self.source_id = database, source_id

    def snapshot(self) -> dict[tuple[str, str], EpisodeHistory]:
        return {(r['feed_id'], r['episode_id']): EpisodeHistory(
            r['play_count'], datetime.fromisoformat(r['last_played_at']) if r['last_played_at'] else None,
            datetime.fromisoformat(r['last_seen_at']))
            for r in self.database.connection.execute(
                'SELECT * FROM podcast_history WHERE source_id = ?', (self.source_id,))}

    def observe(self, episodes, seen_at: datetime):
        stamp = utc(seen_at).isoformat()
        with self.database.connection:
            self.database.connection.executemany(
                'INSERT INTO podcast_history (source_id, feed_id, episode_id, last_seen_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(source_id, feed_id, episode_id) DO UPDATE SET '
                'last_seen_at = MAX(last_seen_at, excluded.last_seen_at)',
                [(self.source_id, e.feed_id, e.episode_id, stamp) for e in episodes])

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
