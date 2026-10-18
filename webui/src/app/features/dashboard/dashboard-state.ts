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
  desiredOn?: boolean;
}
export function compose(snapshot: Snapshot, previous?: Snapshot): ChannelView[] {
  return snapshot.modulators.map((config) => {
    const id = config.id;
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
      runtime?.source_last_error ||
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
      desiredOn: config?.enabled === true && snapshot.status.system.global_stopped !== true,
      observationStale: snapshot.status.system.controller_observation_stale === true,
      confirmedOff:
        snapshot.status.system.controller_observation_stale !== true &&
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
  readonly channels = signal<ChannelView[]>([]);
  readonly stale = signal(true);
  readonly loading = signal(true);
  readonly updated = signal<Date | null>(null);
  private readonly now = signal(Date.now());
  private readonly lastResponseAt = signal(Date.now());
  private readonly httpFailed = signal(false);
  readonly backendUnavailable = computed(
    () => this.httpFailed() && this.now() - this.lastResponseAt() >= 15000,
  );
  readonly observationAge = computed(() => {
    const system = this.data()?.status.system;
    if (!system) return 0;
    const ages = [system.snapshot_age_s, system.controller_observation_age_s]
      .filter((age): age is number => typeof age === 'number' && Number.isFinite(age) && age >= 0);
    // Server observations do not age locally between normal polling requests.
    return Math.floor(Math.max(0, ...ages));
  });
  readonly busy = signal<number | 'start-all' | 'stop-all' | null>(null);
  readonly notice = signal('');
  readonly bulkPendingIds = signal<number[]>([]);
  isPending(id: number) {
    return this.busy() === id || this.bulkPendingIds().includes(id);
  }
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
      commandConfirmed: boolean;
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
  readonly lastValidActiveCount = signal<number | null>(null);
  readonly activeCount = computed(
    () =>
      this.channels().filter((c) => c.runtime?.hardware_tx_state === 'ON')
        .length,
  );
  private polling?: Subscription;
  private readonly cancelled = new Subject<void>();
  private closed = false;
  private inFlight?: Promise<void>;

  start() {
    this.closed = false;
    if (!this.updated()) this.lastResponseAt.set(Date.now());
    this.nextRefreshAt = 0;
    if (!this.polling)
      this.polling = timer(0, 1000)
        .pipe(
          tap(() => {
            this.now.set(Date.now());
            this.expireConfirmations();
          }),
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
    this.bulkPendingIds.set([]);
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
    let statusReceived = false;
    this.inFlight = firstValueFrom(this.api.snapshot(() => {
        statusReceived = true;
        if (!this.closed) {
          this.lastResponseAt.set(Date.now());
          this.httpFailed.set(false);
        }
      }).pipe(takeUntil(this.cancelled)))
      .then((snapshot) => {
        if (this.closed || generation !== this.snapshotGeneration) return;
        this.confirmObservations(snapshot, requestedAt);
        this.channels.set(compose(snapshot, this.data() ?? undefined));
        if (snapshot.status.system.snapshot_stale !== true &&
            snapshot.status.system.controller_observation_stale === false &&
            this.channels().length > 0 && this.channels().every((channel) =>
              channel.runtime?.hardware_tx_state === 'ON' || channel.runtime?.hardware_tx_state === 'OFF')) {
          this.lastValidActiveCount.set(this.activeCount());
        }
        this.data.set(snapshot);
        this.stale.set(snapshot.status.system.snapshot_stale === true);
        this.updated.set(new Date());
      })
      .catch(() => {
        if (!this.closed && !statusReceived) this.httpFailed.set(true);
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
            [id]: c.commandConfirmed
              ? `${c.state} command confirmed; Controller confirmation delayed.`
              : 'Outcome unknown; Controller confirmation delayed.',
          }));
      }
    }
  }
  private confirmObservations(snapshot: Snapshot, requestedAt: number) {
    const system = snapshot.status.system;
    const age = system.controller_observation_age_s;
    if (
      system.snapshot_stale === true ||
      system.controller_observation_stale !== false ||
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
        this.actionFeedback.update((v) => ({
          ...v,
          [id]: `Controller observation confirms ${c.state}.`,
        }));
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
      this.acceptResult(result, generation);
      if (generation === this.messageGeneration)
        this.notice.set(`TX ${id}: command returned ${result.tx_state}. Refreshing observations.`);
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
  private trackConfirmation(
    id: number,
    state: 'ON' | 'OFF',
    generation: number,
    commandConfirmed: boolean,
  ) {
    const receivedAt = Date.now();
    this.confirmations.set(id, {
      state,
      receivedAt,
      deadline: receivedAt + 10000,
      expired: false,
      messageGeneration: generation,
      commandConfirmed,
    });
    if (commandConfirmed) this.commandStates.update((v) => ({ ...v, [id]: state }));
  }
  private acceptResult(result: ModulatorStatus, generation: number) {
    // Only tx_state is command-confirmed; never copy RP readbacks or source state.
    if (result.tx_state === 'ON' || result.tx_state === 'OFF')
      this.trackConfirmation(result.id, result.tx_state, generation, true);
    if (generation === this.messageGeneration)
      this.actionFeedback.update((v) => ({
        ...v,
        [result.id]: `Command returned ${result.tx_state}; awaiting Controller observation.`,
      }));
  }
  private unconfirmedResult(
    id: number,
    action: 'start' | 'stop',
    message: string,
    generation: number,
  ) {
    this.trackConfirmation(id, action === 'start' ? 'ON' : 'OFF', generation, false);
    if (generation === this.messageGeneration)
      this.actionFeedback.update((v) => ({
        ...v,
        [id]: `${message} Outcome unconfirmed; reconciling status.`,
      }));
  }
  async all(action: 'start' | 'stop') {
    if (this.closed || this.busy() !== null) return;
    const generation = this.messageGeneration;
    // Match the backend's skip rules; dependency validation remains backend-owned.
    const eligible = this.channels().filter(
      (c) =>
        action === 'stop' ||
        (c.config?.enabled &&
          c.config.frequency_khz != null &&
          (c.config.audio_mode === 'ANALOG' || c.config.input_source_id != null)),
    );
    const pending = eligible.filter(
      (c) =>
        action === 'start' ||
        this.stale() ||
        !c.confirmedOff ||
        this.data()?.status.system.controller_observation_stale !== false ||
        this.confirmations.has(c.id),
    );
    this.busy.set(action === 'start' ? 'start-all' : 'stop-all');
    this.pendingAction.set(action);
    this.bulkPendingIds.set(pending.map((c) => c.id));
    this.confirmations.clear();
    this.commandStates.set({});
    this.actionFeedback.set(
      Object.fromEntries(
        pending.map((c) => [c.id, action === 'start' ? 'Starting...' : 'Stopping...']),
      ),
    );
    this.failures.set([]);
    this.notice.set(`${action === 'start' ? 'START' : 'STOP ALL'} pending`);
    const failed: { id: number; message: string }[] = [];
    let accepted = 0;
    try {
      const results = await firstValueFrom(
        (action === 'start' ? this.api.startAll() : this.api.stopAll()).pipe(
          takeUntil(this.cancelled),
        ),
      );
      if (this.closed) return;
      for (const c of eligible) {
        const matches = results.filter((r) => r.id === c.id);
        const result = matches.length === 1 ? matches[0] : undefined;
        if (
          !result ||
          'error' in result ||
          (result.tx_state !== 'ON' && result.tx_state !== 'OFF')
        ) {
          const message = !result
            ? 'No unique result returned.'
            : 'error' in result
              ? result.message
              : `Command returned ${result.tx_state}.`;
          failed.push({ id: c.id, message });
          this.unconfirmedResult(c.id, action, message, generation);
        } else {
          accepted++;
          this.acceptResult(result, generation);
        }
      }
      if (generation === this.messageGeneration)
        this.notice.set(
          `${action === 'start' ? 'START' : 'STOP ALL'}: ${accepted} accepted, ${failed.length} failed or unconfirmed.` +
            (action === 'start' && eligible.length < this.channels().length
              ? ' Skipped disabled or unconfigured channels.'
              : ''),
        );
    } catch (error) {
      if (this.closed) return;
      for (const c of eligible) {
        const message = errorMessage(error);
        failed.push({ id: c.id, message });
        this.unconfirmedResult(c.id, action, message, generation);
      }
      if (generation === this.messageGeneration)
        this.notice.set(`${action === 'start' ? 'START' : 'STOP ALL'} failed or outcome unknown.`);
    } finally {
      if (generation === this.messageGeneration) this.failures.set(failed);
      this.bulkPendingIds.set([]);
      this.pendingAction.set(null);
      this.busy.set(null);
      await this.refreshAfterMutation();
    }
  }
}
