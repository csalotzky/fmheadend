import { HttpErrorResponse } from '@angular/common/http';
import { DestroyRef } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { firstValueFrom, TimeoutError } from 'rxjs';
import { errorMessage } from '../../core/api/headend-api';
import { PODCAST_FEED_TEST_TIMEOUT_MS, PodcastProbe, SourceApi, SourceDraft, SourceTest } from './source-api';

export interface PodcastTestProgress {
  urls: readonly string[];
  completed: number;
}

export async function testPodcastFeeds(
  api: SourceApi,
  body: SourceDraft,
  destroyRef: DestroyRef,
  isCurrent: () => boolean,
  update: (progress: PodcastTestProgress, result: SourceTest | null) => void,
) {
  const feeds: readonly { url: string }[] = body.config['feeds'] as { url: string }[];
  const progress = { urls: feeds.map((feed) => feed.url), completed: 0 };
  update(progress, null);
  const podcast: PodcastProbe = {
    feeds_attempted: 0,
    feeds_succeeded: 0,
    discovered_episodes: 0,
    eligible_episodes: 0,
    feeds: [],
  };
  for (const feed of feeds) {
    const result = await firstValueFrom(
      api
        .test({ ...body, config: { ...body.config, feeds: [feed] } })
        .pipe(takeUntilDestroyed(destroyRef)),
    );
    // Let an in-flight request settle after edits, then stop the old batch.
    if (!isCurrent() || destroyRef.destroyed) return;
    const probe = result.podcast;
    if (!probe || probe.feeds_attempted !== 1 || probe.feeds.length !== 1) {
      throw new Error(result.message || 'The backend did not return a result for this feed.');
    }
    podcast.feeds_attempted += probe.feeds_attempted;
    podcast.feeds_succeeded += probe.feeds_succeeded;
    podcast.discovered_episodes += probe.discovered_episodes;
    podcast.eligible_episodes += probe.eligible_episodes;
    podcast.feeds.push(probe.feeds[0]);
    update({ ...progress, completed: podcast.feeds_attempted }, {
      ...result,
      ok: podcast.eligible_episodes > 0,
      podcast: { ...podcast, feeds: [...podcast.feeds] },
    });
  }
}

export function podcastTestError(error: unknown): string {
  if (error instanceof TimeoutError)
    return `The feed test request timed out after ${PODCAST_FEED_TEST_TIMEOUT_MS / 1000} seconds.`;
  if (error instanceof HttpErrorResponse) {
    const code = error.error?.error;
    const message = error.error?.message;
    if (typeof message === 'string') {
      return (typeof code === 'string' ? `${code}: ${message}` : message).slice(0, 300);
    }
    return error.status ? error.message : 'The feed test could not reach the backend.';
  }
  return error instanceof Error ? error.message : errorMessage(error);
}
