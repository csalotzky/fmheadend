import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
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
import { ApiError, InputSource, Modulator, ModulatorStatus, Snapshot } from '../../core/api/models';

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
        : !prior
            ? 'Awaiting sample'
            : (runtime?.pcm_bytes_consumed ?? 0) > prior.pcm_bytes_consumed
              ? 'Flowing'
              : runtime?.tx_state === 'OFF' ? 'Idle' : 'No flow observed';
    // Latched hardware_last_error alone is not an active fault (M4 semantics).
    const error =
      runtime?.start_error ||
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
      desiredOn: runtime?.desired_on,
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

export function startDisposition(row: ModulatorStatus, revision: number) {
  if (row.intent_revision < revision) return 'older';
  if (row.intent_revision > revision) return row.intent_action === 'STOP' ? 'stopped' : 'superseded';
  if (row.start_error) return 'failed';
  if (!row.desired_on) return 'stopped';
  return row.start_pending ? 'preparing' : 'awaiting-rf';
}

function controllerObservedAfter(snapshot: Snapshot, requestedAt: number, receivedAt: number) {
  const system = snapshot.status.system;
  const age = system.controller_observation_age_s;
  // Request start minus server-reported age is a conservative lower bound.
  return system.snapshot_stale !== true && system.controller_observation_stale === false &&
    age != null && Number.isFinite(age) && age >= 0 && requestedAt - age * 1000 >= receivedAt;
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
  private readonly feedbackExpiry = new Map<number, number>();
  private confirmations = new Map<
    number,
    {
      state: 'ON' | 'OFF';
      receivedAt: number;
      deadline: number;
      expired: boolean;
      messageGeneration: number;
      commandConfirmed: boolean;
      revision?: number;
      inferredAction?: 'START' | 'STOP';
      cleanupFailed?: boolean;
      audioPending?: boolean;
    }
  >();
  private snapshotGeneration = 0;
  private snapshotRequestedAt = 0;
  private mutationRequestedAt = 0;
  private nextRefreshAt = 0;
  readonly profileNames = signal<Record<string, string>>({});
  readonly defaultAudioProfileId = signal<string | null>(null);
  profileId(source: InputSource): string | null {
    return source.use_default_audio_profile ? this.defaultAudioProfileId() : source.audio_profile_id;
  }
  private messageGeneration = 0;
  clearMessages() {
    this.feedbackExpiry.clear();
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
    this.feedbackExpiry.clear();
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
    this.mutationRequestedAt = Date.now();
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
        // Both published lists describe the same physical inventory, including
        // disabled/offline TXs. Separate HTTP reads can straddle a publication;
        // retain the previous coherent snapshot until the next normal refresh.
        const configuredIds = new Set(snapshot.modulators.map((row) => row.id));
        const runtimeIds = new Set(snapshot.status.modulators.map((row) => row.id));
        if (configuredIds.size !== snapshot.modulators.length ||
            runtimeIds.size !== snapshot.status.modulators.length ||
            configuredIds.size !== runtimeIds.size ||
            [...configuredIds].some((id) => !runtimeIds.has(id))) {
          this.stale.set(true);
          return;
        }
        this.confirmObservations(snapshot, requestedAt);
        const publicationAt = requestedAt - (snapshot.status.system.snapshot_age_s ?? 0) * 1000;
        if (snapshot.status.system.snapshot_stale !== true && publicationAt >= this.mutationRequestedAt &&
            this.busy() === null) {
          for (const row of snapshot.status.modulators) {
            if (row.start_pending && row.desired_on && !this.confirmations.has(row.id) &&
                !this.failures().some((failure) => failure.id === row.id)) {
              this.trackPendingStart(row.id, row.intent_revision);
            }
          }
        }
        this.channels.set(compose(snapshot, this.data() ?? undefined));
        if (snapshot.status.system.snapshot_stale !== true &&
            snapshot.status.system.controller_observation_stale === false &&
            this.channels().length > 0 && this.channels().every((channel) =>
              channel.runtime?.hardware_tx_state === 'ON' || channel.runtime?.hardware_tx_state === 'OFF')) {
          this.lastValidActiveCount.set(this.activeCount());
        }
        this.snapshotRequestedAt = requestedAt;
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
  observedAfter(receivedAt: number) {
    const snapshot = this.data();
    return !!snapshot && !this.stale() &&
      controllerObservedAfter(snapshot, this.snapshotRequestedAt, receivedAt);
  }
  trackPendingStart(id: number, revision: number, generation = this.messageGeneration) {
    this.trackConfirmation(id, 'ON', generation, false, revision);
    const confirmation = this.confirmations.get(id)!;
    confirmation.audioPending = true;
    confirmation.deadline = Date.now() + 60000;
    this.commandStates.update(v => ({...v, [id]: 'ON'}));
    if (generation === this.messageGeneration)
      this.actionFeedback.update(v => ({...v, [id]: 'Preparing audio…'}));
    this.nextRefreshAt = 0;
  }
  private expireConfirmations() {
    for (const [id, deadline] of this.feedbackExpiry) {
      if (Date.now() >= deadline) {
        this.feedbackExpiry.delete(id);
        this.actionFeedback.update((feedback) => {
          const next = { ...feedback };
          delete next[id];
          return next;
        });
      }
    }
    for (const [id, c] of this.confirmations) {
      if (!c.expired && Date.now() >= c.deadline) {
        c.expired = true;
        if (!c.audioPending && c.messageGeneration === this.messageGeneration)
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
    if (snapshot.status.system.snapshot_stale === true) return;
    for (const [id, c] of this.confirmations) {
      const row = snapshot.status.modulators.find((r) => r.id === id);
      if (!row || (c.revision !== undefined && row.intent_revision < c.revision)) continue;
      const superseded = c.revision !== undefined && row.intent_revision > c.revision;
      const start = c.audioPending && c.revision !== undefined ? startDisposition(row, c.revision) : null;
      if (superseded || start === 'failed' || start === 'stopped') {
        this.clearConfirmation(id);
        if (c.messageGeneration === this.messageGeneration) {
          const message = start === 'failed' ? row.start_error! : start === 'stopped'
            ? 'Start cancelled.' : 'Operation superseded by a newer TX action.';
          this.actionFeedback.update((v) => ({ ...v, [id]: message }));
        }
        continue;
      }
      // A lost reply carries no receipt. A matching RF state from another
      // kind of action cannot reconcile even the expected next revision.
      if (c.inferredAction && row.intent_action !== c.inferredAction) continue;
      if (start === 'awaiting-rf' && row.tx_state === 'ON' && !row.last_error && !row.source_last_error) {
        c.audioPending = false;
        c.commandConfirmed = true;
        c.expired = false;
        c.deadline = Date.now() + 10000;
        if (c.messageGeneration === this.messageGeneration)
          this.actionFeedback.update((v) => ({ ...v, [id]: 'Awaiting Controller observation.' }));
      }
      if (!controllerObservedAfter(snapshot, requestedAt, c.receivedAt) ||
          row.hardware_tx_state !== c.state ||
          (c.state === 'ON' && (!row.desired_on || row.start_pending || row.last_error ||
            row.source_last_error || row.tx_state !== 'ON'))) continue;
      this.clearConfirmation(id);
      if (c.messageGeneration === this.messageGeneration) {
        const resolvedFailure = this.failures().some((failure) => failure.id === id);
        if (!c.cleanupFailed) this.failures.update((failures) => failures.filter((failure) => failure.id !== id));
        if (
          resolvedFailure &&
          this.failures().length === 0 &&
          this.notice() === 'Operation failed or outcome unknown.'
        )
          this.notice.set('');
        this.actionFeedback.update((v) => ({
          ...v,
          [id]: `Controller observation confirms ${c.state}.`,
        }));
        this.feedbackExpiry.set(id, Date.now() + 3000);
      }
    }
  }
  private clearConfirmation(id: number) {
    this.confirmations.delete(id);
    this.commandStates.update((v) => {
      const next = { ...v };
      delete next[id];
      return next;
    });
  }
  async operate(id: number, action: 'start' | 'stop') {
    if (this.closed || this.busy() !== null) return;
    this.feedbackExpiry.delete(id);
    const generation = this.messageGeneration;
    const expectedRevision = this.nextIntentRevision(id);
    this.busy.set(id);
    this.pendingAction.set(action);
    this.clearConfirmation(id);
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
      const cleanupError = this.acceptResult(result, generation);
      if (generation === this.messageGeneration) {
        if (cleanupError) this.failures.set([{ id, message: cleanupError }]);
        this.notice.set(`TX ${id}: command returned ${result.tx_state}. Refreshing observations.`);
      }
    } catch (error) {
      if (!this.closed && generation === this.messageGeneration) {
        const message = errorMessage(error);
        const details = error instanceof HttpErrorResponse ? (error.error as ApiError | null) : null;
        this.commandFailure(id, action, message, generation, expectedRevision, details, true);
        this.failures.set([{ id, message }]);
        this.notice.set(details?.rf_outcome === 'REJECTED' ? 'Command rejected.' : 'Operation failed or outcome unknown.');
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
    revision: number | undefined,
    pollingOnly = false,
  ) {
    const receivedAt = Date.now();
    this.confirmations.set(id, {
      state,
      receivedAt,
      // Failed requests retain their error until normal polling confirms the state.
      deadline: pollingOnly ? receivedAt : receivedAt + 10000,
      expired: pollingOnly,
      messageGeneration: generation,
      commandConfirmed,
      revision,
    });
    if (commandConfirmed) this.commandStates.update((v) => ({ ...v, [id]: state }));
  }
  private acceptResult(result: ModulatorStatus, generation: number) {
    if (result.start_pending) {
      this.trackPendingStart(result.id, result.intent_revision, generation);
      return;
    }
    // Only tx_state is command-confirmed; never copy RP readbacks or source state.
    const cleanupError = result.tx_state === 'OFF' ? result.last_error : null;
    if (result.tx_state === 'ON' || result.tx_state === 'OFF') {
      this.trackConfirmation(result.id, result.tx_state, generation, true, result.intent_revision);
      if (cleanupError) this.confirmations.get(result.id)!.cleanupFailed = true;
    }
    if (generation === this.messageGeneration)
      this.actionFeedback.update((v) => ({
        ...v,
        [result.id]: `Command returned ${result.tx_state}; awaiting Controller observation.`,
      }));
    return cleanupError;
  }
  private nextIntentRevision(id: number) {
    const previous = this.channels().find((c) => c.id === id)?.runtime?.intent_revision;
    return previous === undefined ? undefined : previous + 1;
  }
  private commandFailure(
    id: number,
    action: 'start' | 'stop',
    message: string,
    generation: number,
    expectedRevision: number | undefined,
    details?: ApiError | null,
    pollingOnly = false,
  ) {
    const rejected = details?.rf_outcome === 'REJECTED';
    if (!rejected) {
      this.trackConfirmation(id, action === 'start' ? 'ON' : 'OFF', generation, false,
        details?.intent_revision ?? expectedRevision, pollingOnly);
      if (details?.intent_revision === undefined)
        this.confirmations.get(id)!.inferredAction = action === 'start' ? 'START' : 'STOP';
    }
    if (generation === this.messageGeneration)
      this.actionFeedback.update((v) => ({
        ...v,
        [id]: rejected ? message : `${message} Outcome unconfirmed; reconciling status.`,
      }));
  }
  async all(action: 'start' | 'stop') {
    if (this.closed || this.busy() !== null) return;
    this.feedbackExpiry.clear();
    const generation = this.messageGeneration;
    // Match the backend's skip rules; dependency validation remains backend-owned.
    const eligible = this.channels().filter(
      (c) =>
        action === 'stop' ||
        (c.config?.enabled &&
          c.config.frequency_khz != null &&
          (c.config.audio_mode === 'ANALOG' || c.config.input_source_id != null)),
    );
    const expectedRevisions = new Map(eligible.map((c) => [c.id, this.nextIntentRevision(c.id)]));
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
    let cleanupFailures = 0;
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
          (!result.start_pending && result.tx_state !== 'ON' && result.tx_state !== 'OFF')
        ) {
          const message = !result
            ? 'No unique result returned.'
            : 'error' in result
              ? result.message
              : `Command returned ${result.tx_state}.`;
          failed.push({ id: c.id, message });
          this.commandFailure(c.id, action, message, generation, expectedRevisions.get(c.id),
            result && 'error' in result ? result : null);
        } else {
          accepted++;
          const cleanupError = this.acceptResult(result, generation);
          if (cleanupError) {
            cleanupFailures++;
            failed.push({ id: c.id, message: cleanupError });
          }
        }
      }
      if (generation === this.messageGeneration)
        this.notice.set(
          `${action === 'start' ? 'START' : 'STOP ALL'}: ${accepted} accepted, ${failed.length - cleanupFailures} failed or unconfirmed.` +
            (cleanupFailures ? ` Cleanup failed for ${cleanupFailures} TX.` : '') +
            (action === 'start' && eligible.length < this.channels().length
              ? ' Skipped disabled or unconfigured channels.'
              : ''),
        );
    } catch (error) {
      if (this.closed) return;
      const details = error instanceof HttpErrorResponse ? (error.error as ApiError | null) : null;
      const message = errorMessage(error);
      for (const c of eligible) {
        failed.push({ id: c.id, message });
        this.commandFailure(c.id, action, message, generation, expectedRevisions.get(c.id), details);
      }
      if (generation === this.messageGeneration)
        this.notice.set(`${action === 'start' ? 'START' : 'STOP ALL'} ` +
          (details?.rf_outcome === 'REJECTED' ? `rejected. ${message}` : 'failed or outcome unknown.'));
    } finally {
      if (generation === this.messageGeneration) this.failures.set(failed);
      this.bulkPendingIds.set([]);
      this.pendingAction.set(null);
      this.busy.set(null);
      await this.refreshAfterMutation();
    }
  }
}
