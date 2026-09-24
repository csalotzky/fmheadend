import { Component, computed, effect, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { catchError, firstValueFrom, from, map, mergeMap, of } from 'rxjs';
import { DashboardState } from '../dashboard/dashboard-state';
import { InputSource } from '../../core/api/models';
import { sortSources } from '../../core/api/source-order';
import { errorMessage } from '../../core/api/headend-api';
import { SourceApi, SourceRuntime, SourceTest } from './source-api';
import { TestResult } from './test-result';
export function endpoint(source: InputSource): string {
  if (source.type === 'ALSA') return String(source.config['device'] ?? '\u2014');
  if (source.type === 'HTTP') {
    try {
      const u = new URL(String(source.config['uri']));
      return u.origin + u.pathname;
    } catch {
      return 'Stream URL unavailable';
    }
  }
  return `${source.config['multicast_group'] || source.config['host'] || '0.0.0.0'}:${source.config['port'] ?? '\u2014'}`;
}
@Component({
  host: { class: 'page-content' },
  selector: 'app-sources-list',
  imports: [RouterLink, TestResult],
  templateUrl: './sources-list.html',
  styleUrl: './sources.css',
})
export class SourcesList {
  readonly shared = inject(DashboardState);
  private api = inject(SourceApi);
  readonly sources = computed(() => sortSources(this.shared.data()?.sources ?? []));
  readonly runtime = signal<Record<number, SourceRuntime | null>>({});
  readonly pending = signal<Set<number>>(new Set());
  readonly pendingLabel = signal<Record<number, string>>({});
  readonly results = signal<Record<number, SourceTest | null>>({});
  readonly errors = signal<Record<number, string>>({});
  readonly deleting = signal<number | null>(null);
  readonly endpoint = endpoint;
  constructor() {
    effect((cleanup) => {
      const snapshot = this.shared.data();
      const stale = this.shared.stale();
      if (!snapshot || stale) {
        this.runtime.set({});
        return;
      }
      const sub = from(snapshot.sources)
        .pipe(
          mergeMap(
            (s) =>
              this.api.status(s.id).pipe(
                map((value) => ({ id: s.id, value })),
                catchError(() => of({ id: s.id, value: null })),
              ),
            4,
          ),
        )
        .subscribe(({ id, value }) => this.runtime.update((r) => ({ ...r, [id]: value })));
      cleanup(() => sub.unsubscribe());
    });
  }
  references(id: number) {
    return this.shared.data()?.modulators.filter((m) => m.input_source_id === id).length ?? 0;
  }
  private busy(id: number, value: boolean, label = 'Testing...') {
    if (value) this.pendingLabel.update((labels) => ({ ...labels, [id]: label }));
    this.pending.update((p) => {
      const next = new Set(p);
      if (value) next.add(id);
      else next.delete(id);
      return next;
    });
  }
  async test(id: number) {
    if (this.pending().has(id)) return;
    this.busy(id, true);
    this.errors.update((e) => ({ ...e, [id]: '' }));
    this.results.update((r) => ({ ...r, [id]: null }));
    try {
      const result = await firstValueFrom(this.api.test(id));
      this.results.update((r) => ({ ...r, [id]: result }));
    } catch (e) {
      this.errors.update((v) => ({ ...v, [id]: errorMessage(e) }));
    } finally {
      this.busy(id, false);
      void this.shared.refresh();
    }
  }
  async remove(id: number) {
    if (this.deleting() !== id || this.pending().has(id)) return;
    this.busy(id, true, 'Deleting...');
    this.errors.update((e) => ({ ...e, [id]: '' }));
    try {
      await firstValueFrom(this.api.delete(id));
      this.deleting.set(null);
      await this.shared.refreshAfterMutation();
    } catch (e) {
      this.errors.update((v) => ({ ...v, [id]: errorMessage(e) }));
    } finally {
      this.busy(id, false);
    }
  }
}
