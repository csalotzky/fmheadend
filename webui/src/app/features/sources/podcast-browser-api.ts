import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { timeout } from 'rxjs';

export interface PodcastSearchResult {
  title: string;
  publisher: string | null;
  category: string | null;
  episode_count: number | null;
  artwork_url: string | null;
  feed_url: string;
}

export interface PodcastSearchResponse {
  podcasts: PodcastSearchResult[];
}

@Injectable({ providedIn: 'root' })
export class PodcastBrowserApi {
  private http = inject(HttpClient);

  search(name: string) {
    return this.http
      .get<PodcastSearchResponse>('/api/v1/podcast-browser/search', { params: { name } })
      .pipe(timeout(20000));
  }
}
