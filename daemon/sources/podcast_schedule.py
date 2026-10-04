"""Single-owner transient selection over current eligibility/history snapshots."""
import random
from datetime import datetime

from ..errors import DomainError
from .podcast import PodcastEpisode, PodcastScheduleMode, newest_key, utc
from .podcast_history import EpisodeHistory


class PodcastScheduler:
    def __init__(self, mode=PodcastScheduleMode.SMART, *, rng=None):
        self.mode = PodcastScheduleMode(mode)
        self.rng = rng if rng is not None else random.Random()
        self._bag = []
        self._last = None

    def choose_next(self, episodes, history, now: datetime, *, replay_cooldown_hours=24) -> PodcastEpisode:
        now = utc(now)
        current = {episode.key: episode for episode in episodes}
        if not current:
            self._bag.clear()
            raise DomainError(409, "NO_PLAYABLE_EPISODES", "No eligible podcast enclosures")
        if self.mode == PodcastScheduleMode.RANDOM:
            # Removed entries are pruned immediately; additions enter the next
            # bag. Return current objects so changed enclosure URLs take effect.
            self._bag = [key for key in self._bag if key in current]
            if not self._bag:
                self._bag = sorted(current)
                self.rng.shuffle(self._bag)
                if len(self._bag) > 1 and self._bag[-1] == self._last:
                    index = self.rng.randrange(len(self._bag) - 1)
                    self._bag[-1], self._bag[index] = self._bag[index], self._bag[-1]
            result = current[self._bag.pop()]
        else:
            candidates = list(current.values())
            def record(e):
                return history.get(e.key, EpisodeHistory())
            outside = [e for e in candidates if record(e).last_played_at is None or
                (now - record(e).last_played_at).total_seconds() >= replay_cooldown_hours * 3600]
            if outside:
                candidates = outside
            def rank(e):
                h = record(e)
                same_feed = self._last is not None and e.feed_id == self._last[0]
                if h.play_count == 0:
                    # Global unplayed preference after cooldown. Publication is
                    # ranking only; undated entries remain candidates. Put feed
                    # diversity before newest_key's final identity tie-break.
                    return (0, newest_key(e)[:2], same_feed, e.key)
                # Once played, rotation fairness precedes publication freshness.
                return (1, h.play_count, h.last_played_at.timestamp() if h.last_played_at else 0,
                        same_feed, newest_key(e), e.key)
            result = min(candidates, key=rank)
        self._last = result.key
        return result
