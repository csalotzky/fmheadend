import { Injectable, computed, inject, signal } from '@angular/core';
import { Subject, Subscription, exhaustMap, filter, firstValueFrom, takeUntil, timer } from 'rxjs';
import { HeadendApi, errorMessage } from '../../core/api/headend-api';
import { InputSource, Modulator, ModulatorStatus, Snapshot } from '../../core/api/models';

export interface ChannelView {
  id: number;
  config?: Modulator;
  runtime?: ModulatorStatus;
  sourceName: string;
  source?: InputSource;
  analog: boolean;
  canStart: boolean;
  pcm: string;
  error: string | null;
}
export function compose(snapshot: Snapshot, previous?: Snapshot): ChannelView[] {
  return Array.from({ length: 8 }, (_, id) => {
    const config = snapshot.modulators.find((m) => m.id === id);
    const runtime = snapshot.status.modulators.find((m) => m.id === id);
    const source = snapshot.sources.find((s) => s.id === config?.input_source_id);
    const prior = previous?.status.modulators.find((m) => m.id === id);
    const analog = config?.audio_mode === 'ANALOG';
    const pcm = analog
      ? 'Bypass'
      : snapshot.status.system.mux_state !== 'RUNNING'
        ? 'Unavailable'
        : runtime?.tx_state === 'OFF'
          ? 'Idle'
          : !prior
            ? 'Awaiting sample'
            : (runtime?.pcm_bytes_consumed ?? 0) > prior.pcm_bytes_consumed
              ? 'Flowing'
              : 'No flow observed';
    // Latched hardware_last_error alone is not an active fault (M4 semantics).
    const error =
      runtime?.last_error ||
      runtime?.rds_last_error ||
      (runtime?.tx_state === 'ERROR'
        ? runtime.hardware_last_error || 'Hardware/control error'
        : runtime?.source_state === 'ERROR'
          ? 'Source unavailable; TX may remain on with silence.'
          : null);
    return {
      id,
      config,
      runtime,
      source,
      analog,
      pcm,
      error,
      sourceName: analog
        ? 'Dedicated analog input'
        : (source?.name ??
          (config?.input_source_id == null ? 'No source assigned' : 'Source unavailable')),
      canStart: config?.frequency_khz != null && (analog || !!source?.enabled),
    };
  });
}

@Injectable({ providedIn: 'root' })
export class DashboardState {
  private api = inject(HeadendApi);
  readonly data = signal<Snapshot | null>(null);
  readonly channels = signal<ChannelView[]>(
    Array.from({ length: 8 }, (_, id) => ({
      id,
      sourceName: '\u2014',
      analog: false,
      canStart: false,
      pcm: 'Unknown',
      error: null,
    })),
  );
  readonly stale = signal(true);
  readonly loading = signal(true);
  readonly updated = signal<Date | null>(null);
  readonly busy = signal<number | 'start-all' | 'stop-all' | null>(null);
  readonly notice = signal('');
  readonly failures = signal<{ id: number; message: string }[]>([]);
  readonly activeCount = computed(
    () => this.channels().filter((c) => c.runtime?.tx_state === 'ON').length,
  );
  private polling?: Subscription;
  private readonly cancelled = new Subject<void>();
  private closed = false;
  private inFlight?: Promise<void>;

  start() {
    this.closed = false;
    if (!this.polling)
      this.polling = timer(0, 5000)
        .pipe(
          filter(() => this.busy() === null),
          exhaustMap(() => this.refresh()),
        )
        .subscribe();
  }
  stop() {
    this.closed = true;
    this.polling?.unsubscribe();
    this.polling = undefined;
    this.cancelled.next();
  }
  async refreshAfterMutation() {
    await this.inFlight;
    await this.refresh();
  }
  refresh(): Promise<void> {
    if (this.closed) return Promise.resolve();
    if (this.inFlight) return this.inFlight;
    this.inFlight = firstValueFrom(this.api.snapshot().pipe(takeUntil(this.cancelled)))
      .then((snapshot) => {
        // Reject incomplete channel inventories instead of displaying fake healthy cards.
        for (const rows of [snapshot.modulators, snapshot.status.modulators]) {
          if (
            rows.length !== 8 ||
            new Set(rows.map((r) => r.id)).size !== 8 ||
            rows.some((r) => r.id < 0 || r.id > 7)
          ) {
            throw new Error('Invalid physical channel inventory');
          }
        }
        this.channels.set(compose(snapshot, this.data() ?? undefined));
        this.data.set(snapshot);
        this.stale.set(false);
        this.updated.set(new Date());
      })
      .catch(() => this.stale.set(true))
      .finally(() => {
        this.loading.set(false);
        this.inFlight = undefined;
      });
    return this.inFlight;
  }
  async operate(id: number, action: 'start' | 'stop') {
    if (this.busy() !== null) return;
    this.busy.set(id);
    this.failures.set([]);
    this.notice.set(`TX ${id}: ${action.toUpperCase()} pending`);
    try {
      await this.inFlight;
      await firstValueFrom(this.api.channel(id, action).pipe(takeUntil(this.cancelled)));
      this.notice.set(`TX ${id}: ${action.toUpperCase()} accepted. Status refreshed below.`);
    } catch (error) {
      this.failures.set([{ id, message: errorMessage(error) }]);
      this.notice.set('Operation failed or outcome unknown.');
    } finally {
      await this.refresh();
      this.busy.set(null);
    }
  }
  async all(action: 'start' | 'stop') {
    if (this.busy() !== null) return;
    this.busy.set(action === 'start' ? 'start-all' : 'stop-all');
    this.failures.set([]);
    this.notice.set(`${action.toUpperCase()} ALL pending`);
    let accepted = 0;
    const failed: { id: number; message: string }[] = [];
    let skipped: number[] = [];
    try {
      await this.inFlight;
      if (action === 'stop') {
        const results = await firstValueFrom(this.api.stopAll().pipe(takeUntil(this.cancelled)));
        for (let id = 0; id < 8; id++) {
          const result = results.find((r) => r.id === id);
          if (!result || 'error' in result)
            failed.push({
              id,
              message: result && 'message' in result ? result.message : 'No result returned',
            });
          else accepted++;
        }
      } else {
        const eligible = this.channels().filter((c) => c.canStart);
        skipped = this.channels()
          .filter((c) => !c.canStart)
          .map((c) => c.id);
        for (const channel of eligible) {
          if (this.closed) break;
          try {
            await firstValueFrom(
              this.api.channel(channel.id, 'start').pipe(takeUntil(this.cancelled)),
            );
            accepted++;
          } catch (error) {
            failed.push({ id: channel.id, message: errorMessage(error) });
          }
        }
      }
      this.notice.set(
        `${action.toUpperCase()} ALL: ${accepted} accepted, ${failed.length} failed.` +
          (skipped.length
            ? ` Skipped unconfigured/disabled-source channels: ${skipped.join(', ')}.`
            : ''),
      );
    } catch (error) {
      for (let id = 0; id < 8; id++) failed.push({ id, message: errorMessage(error) });
      this.notice.set('STOP ALL failed or outcome unknown.');
    } finally {
      this.failures.set(failed);
      await this.refresh();
      this.busy.set(null);
    }
  }
}
