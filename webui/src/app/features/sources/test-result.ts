import { Component, input } from '@angular/core';
import { SourceTest } from './source-api';
import { feedErrorText, feedStatus } from './podcast-form';

@Component({
  selector: 'app-source-test-result',
  template: ` @if (result(); as r) {
    @if (r.podcast; as p) {
      <div
        role="status"
        [class.error]="podcastLevel(r) === 'error'"
        [class.warning]="podcastLevel(r) === 'warning'"
      >
        <strong>{{ podcastTitle(r) }}</strong>
        <dl class="podcast-summary">
          <dt>Feeds</dt>
          <dd>{{ p.feeds_succeeded }} / {{ p.feeds_attempted }}</dd>
          <dt>Episodes</dt>
          <dd>{{ p.discovered_episodes }} discovered</dd>
          <dt>Eligible</dt>
          <dd>{{ p.eligible_episodes }}</dd>
        </dl>
        @if (showFeeds()) {
          <ul class="podcast-feeds">
            @for (feed of p.feeds; track $index) {
              <li>
                <span class="feed-name" [attr.title]="feedLabel($index)"
                  >{{ feed.title || feedLabel($index) }}
                  @if (feed.title && feeds()[$index]) {
                    <small class="muted"> {{ feeds()[$index] }}</small>
                  }
                </span>
                <span>{{ feed.error ? errorText(feed.error) : status(feed) }}</span>
              </li>
            }
          </ul>
        }
        @if (r.ok) {
          <p>{{ r.message }}</p>
        }
      </div>
    } @else {
      <div role="status" [class.error]="!r.ok">
        <strong
          >{{ r.ok ? 'Test successful' : 'Test failed'
          }}{{ r.mocked ? ' (mock probe)' : '' }}</strong
        >
        @if (r.ok && r.station_name) {
          <p>
            <strong>Station: {{ r.station_name }}</strong>
          </p>
        }
        <p>{{ r.message }}</p>
        @if (r.codec) {
          <span>{{ r.codec }} </span>
        }
        @if (r.sample_rate) {
          <span>{{ r.sample_rate }} Hz </span>
        }
        @if (r.channels) {
          <span>{{ r.channels }} channels </span>
        }
        @if (r.ok && bitrate(r.input_bitrate_bps); as rate) {
          <p>Bitrate: ~{{ rate }} kb/s</p>
        }
        @if (r.output_caps) {
          <p>{{ r.output_caps }}</p>
        }
        @if (r.stream_title) {
          <p>{{ r.stream_title }}</p>
        }
      </div>
    }
  }`,
  styles: `
    :host {
      display: block;
      overflow-wrap: anywhere;
    }
    p {
      margin: 6px 0;
    }
    .podcast-summary {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 2px 16px;
      margin: 6px 0;
    }
    dd {
      margin: 0;
    }
    .podcast-feeds {
      list-style: none;
      margin: 6px 0;
      padding: 0;
    }
    .podcast-feeds li {
      display: flex;
      gap: 12px;
      justify-content: space-between;
    }
    .feed-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  `,
})
export class TestResult {
  readonly result = input<SourceTest | null>(null);
  readonly feeds = input<string[]>([]);
  readonly showFeeds = input(true);
  bitrate(value: number | null | undefined): number | null {
    return value != null && Number.isFinite(value) && value > 0
      ? Math.max(1, Math.round(value / 1000))
      : null;
  }
  feedLabel(index: number): string {
    return this.feeds()[index] ?? `Feed ${index + 1}`;
  }
  status(feed: { discovered_episodes: number; eligible_episodes?: number | null }): string {
    return feedStatus(feed);
  }
  errorText(code: string): string {
    return feedErrorText(code);
  }
  podcastLevel(r: SourceTest): 'ok' | 'warning' | 'error' {
    const p = r.podcast;
    if (!p || !r.ok || p.feeds_succeeded === 0) return 'error';
    return p.feeds_succeeded < p.feeds_attempted ? 'warning' : 'ok';
  }
  podcastTitle(r: SourceTest): string {
    const p = r.podcast!;
    const failed = p.feeds_attempted - p.feeds_succeeded;
    if (p.feeds_succeeded === 0) return 'Feed test failed';
    if (failed > 0) return `${failed} of ${p.feeds_attempted} feeds failed`;
    return r.ok ? 'Feed test successful' : 'Feed test found no eligible episodes';
  }
}
