import { Injectable, computed, inject, signal } from '@angular/core';
import {
  Subject,
  Subscription,
  exhaustMap,
  filter,
  firstValueFrom,
  takeUntil,
  tap,
  timer,
} from 'rxjs';
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
  confirmedOff?: boolean;
  observationStale?: boolean;
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
      (runtime?.last_error_scope !== 'GLOBAL' ? runtime?.last_error : null) ||
      (runtime?.rds_last_error_scope !== 'GLOBAL' ? runtime?.rds_last_error : null) ||
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
      observationStale: snapshot.status.system.rp_observation_stale === true,
      confirmedOff:
        snapshot.status.system.rp_observation_stale !== true &&
        runtime?.hardware_tx_state === 'OFF' &&
        runtime?.tx_state === 'OFF',
      sourceName: analog
        ? 'Dedicated analog input'
        : (source?.name ??
          (config?.input_source_id == null ? 'No source assigned' : 'Source unavailable')),
      canStart: config?.frequency_khz != null && (analog || !!source),
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
  readonly pendingAction = signal<'start' | 'stop' | null>(null);
  readonly commandStates = signal<Record<number, 'ON' | 'OFF'>>({});
  readonly actionFeedback = signal<Record<number, string>>({});
  private confirmations = new Map<
    number,
    {
      state: 'ON' | 'OFF';
      receivedAt: number;
      deadline: number;
      expired: boolean;
      messageGeneration: number;
    }
  >();
  private snapshotGeneration = 0;
  private nextRefreshAt = 0;
  readonly profileNames = signal<Record<string, string>>({});
  private messageGeneration = 0;
  clearMessages() {
    this.messageGeneration++;
    this.notice.set('');
    this.failures.set([]);
    this.actionFeedback.set({});
  }
  readonly failures = signal<{ id: number; message: string }[]>([]);
  readonly activeCount = computed(
    () =>
      this.channels().filter((c) => (this.commandStates()[c.id] ?? c.runtime?.tx_state) === 'ON')
        .length,
  );
  private polling?: Subscription;
  private readonly cancelled = new Subject<void>();
  private closed = false;
  private inFlight?: Promise<void>;

  start() {
    this.closed = false;
    this.nextRefreshAt = 0;
    if (!this.polling)
      this.polling = timer(0, 1000)
        .pipe(
          tap(() => this.expireConfirmations()),
          filter(() => this.busy() === null && Date.now() >= this.nextRefreshAt),
          exhaustMap(() => this.refresh()),
        )
        .subscribe();
  }
  stop() {
    this.closed = true;
    this.polling?.unsubscribe();
    this.polling = undefined;
    this.snapshotGeneration++;
    this.messageGeneration++;
    this.cancelled.next();
    this.confirmations.clear();
    this.commandStates.set({});
    this.actionFeedback.set({});
    this.pendingAction.set(null);
    this.busy.set(null);
  }
  ngOnDestroy() {
    this.stop();
  }
  async refreshAfterMutation() {
    // An older in-flight request is neither post-action evidence nor our refresh.
    // Concurrent callers wait on it, then share the next request.
    this.snapshotGeneration++;
    await this.inFlight;
    await this.refresh();
  }
  refresh(): Promise<void> {
    if (this.closed) return Promise.resolve();
    if (this.inFlight) return this.inFlight;
    const generation = this.snapshotGeneration;
    const requestedAt = Date.now();
    this.inFlight = firstValueFrom(this.api.snapshot().pipe(takeUntil(this.cancelled)))
      .then((snapshot) => {
        if (this.closed || generation !== this.snapshotGeneration) return;
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
        this.confirmObservations(snapshot, requestedAt);
        this.channels.set(compose(snapshot, this.data() ?? undefined));
        this.data.set(snapshot);
        this.stale.set(snapshot.status.system.snapshot_stale === true);
        this.updated.set(new Date());
      })
      .catch(() => {
        if (!this.closed && generation === this.snapshotGeneration) this.stale.set(true);
      })
      .finally(() => {
        this.loading.set(false);
        this.inFlight = undefined;
        this.nextRefreshAt = Date.now() + (this.accelerating() ? 1000 : 5000);
      });
    return this.inFlight;
  }
  private accelerating() {
    return [...this.confirmations.values()].some((c) => c.deadline > Date.now());
  }
  private expireConfirmations() {
    for (const [id, c] of this.confirmations) {
      if (!c.expired && Date.now() >= c.deadline) {
        c.expired = true;
        if (c.messageGeneration === this.messageGeneration)
          this.actionFeedback.update((v) => ({
            ...v,
            [id]: `${c.state} command confirmed; RP confirmation delayed.`,
          }));
      }
    }
  }
  private confirmObservations(snapshot: Snapshot, requestedAt: number) {
    const system = snapshot.status.system;
    const age = system.rp_observation_age_s;
    if (
      system.snapshot_stale === true ||
      system.rp_observation_stale !== false ||
      age == null ||
      !Number.isFinite(age) ||
      age < 0
    )
      return;
    for (const [id, c] of this.confirmations) {
      // Request start minus server-reported age is a conservative lower bound:
      // network/serialization delay must never make an old observation look new.
      if (requestedAt - age * 1000 < c.receivedAt) continue;
      const row = snapshot.status.modulators.find((r) => r.id === id);
      if (row?.hardware_tx_state !== c.state) continue;
      this.confirmations.delete(id);
      this.commandStates.update((v) => {
        const next = { ...v };
        delete next[id];
        return next;
      });
      if (c.messageGeneration === this.messageGeneration)
        this.actionFeedback.update((v) => ({ ...v, [id]: `RP observation confirms ${c.state}.` }));
    }
  }
  async operate(id: number, action: 'start' | 'stop') {
    if (this.closed || this.busy() !== null) return;
    const generation = this.messageGeneration;
    this.busy.set(id);
    this.pendingAction.set(action);
    this.confirmations.delete(id);
    this.commandStates.update((v) => {
      const next = { ...v };
      delete next[id];
      return next;
    });
    this.failures.set([]);
    this.actionFeedback.update((v) => ({
      ...v,
      [id]: action === 'start' ? 'Starting...' : 'Stopping...',
    }));
    this.notice.set(`TX ${id}: ${action.toUpperCase()} pending`);
    try {
      const result = await firstValueFrom(
        this.api.channel(id, action).pipe(takeUntil(this.cancelled)),
      );
      if (this.closed) return;
      if (result.id !== id) throw new Error('Unexpected action target');
      // Only tx_state is a command result. Do not copy RP readbacks or source state.
      if (result.tx_state === 'ON' || result.tx_state === 'OFF') {
        const receivedAt = Date.now();
        this.confirmations.set(id, {
          state: result.tx_state,
          receivedAt,
          deadline: receivedAt + 10000,
          expired: false,
          messageGeneration: generation,
        });
        this.commandStates.update((v) => ({ ...v, [id]: result.tx_state as 'ON' | 'OFF' }));
      }
      if (generation === this.messageGeneration) {
        this.notice.set(`TX ${id}: command returned ${result.tx_state}. Refreshing observations.`);
        this.actionFeedback.update((v) => ({
          ...v,
          [id]: `Command returned ${result.tx_state}; awaiting RP observation.`,
        }));
      }
    } catch (error) {
      if (!this.closed && generation === this.messageGeneration) {
        this.failures.set([{ id, message: errorMessage(error) }]);
        this.notice.set('Operation failed or outcome unknown.');
        this.actionFeedback.update((v) => ({
          ...v,
          [id]: 'Operation failed or outcome unknown; reconciling status.',
        }));
      }
    } finally {
      // Release request pending even if the observation refresh is slow or fails.
      this.pendingAction.set(null);
      this.busy.set(null);
      await this.refreshAfterMutation();
    }
  }
  async all(action: 'start' | 'stop') {
    if (this.closed || this.busy() !== null) return;
    const generation = this.messageGeneration;
    this.busy.set(action === 'start' ? 'start-all' : 'stop-all');
    this.confirmations.clear();
    this.commandStates.set({});
    this.actionFeedback.set({});
    this.failures.set([]);
    this.notice.set(`${action.toUpperCase()} ALL pending`);
    const eligible = this.channels().filter(
      (c) => action === 'stop' || (c.config?.enabled && c.canStart),
    );
    const failed: { id: number; message: string }[] = [];
    let accepted = 0;
    try {
      await this.inFlight;
      if (this.closed) return;
      const results = await firstValueFrom(
        (action === 'start' ? this.api.startAll() : this.api.stopAll()).pipe(
          takeUntil(this.cancelled),
        ),
      );
      for (const c of eligible) {
        const result = results.find((r) => r.id === c.id);
        if (!result || 'error' in result)
          failed.push({
            id: c.id,
            message: result && 'message' in result ? result.message : 'No result returned',
          });
        else accepted++;
      }
      if (generation === this.messageGeneration)
        this.notice.set(
          `${action.toUpperCase()} ALL: ${accepted} accepted, ${failed.length} failed.` +
            (eligible.length < 8 ? ' Skipped disabled or unconfigured channels.' : ''),
        );
    } catch (error) {
      for (const c of eligible) failed.push({ id: c.id, message: errorMessage(error) });
      if (generation === this.messageGeneration)
        this.notice.set(`${action.toUpperCase()} ALL failed or outcome unknown.`);
    } finally {
      if (generation === this.messageGeneration) this.failures.set(failed);
      await this.refreshAfterMutation();
      this.busy.set(null);
    }
  }
}
