import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DashboardState, compose } from './dashboard-state';
import { Snapshot } from '../../core/api/models';
import { sample } from './test-fixtures';

describe('Dashboard API composition and operations', () => {
  it('shows the assigned source error without inventing a hardware fault', () => {
    const data = sample();
    data.status.system.system_state = 'RUNNING';
    data.status.system.controller_observation_stale = false;
    data.status.modulators[0].source_state = 'ERROR';
    data.status.modulators[0].source_last_error = 'FOLDER_UNREADABLE';
    data.status.modulators[0].hardware_tx_state = 'OFF';
    data.status.modulators[0].tx_state = 'OFF';
    const channel = compose(data)[0];
    expect(channel.error).toBe('FOLDER_UNREADABLE');
    expect(channel.observationStale).toBe(false);
    expect(channel.runtime?.hardware_tx_state).toBe('OFF');
    expect(data.status.system.system_state).toBe('RUNNING');
  });
  let state: DashboardState, http: HttpTestingController;
  function respond(snapshot = sample()) {
    http.expectOne('/api/v1/status').flush(snapshot.status);
    http.expectOne('/api/v1/modulators').flush(snapshot.modulators);
    http.expectOne('/api/v1/sources').flush(snapshot.sources);
  }
  async function settle() {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  }
  async function load(snapshot = sample()) {
    const done = state.refresh();
    respond(snapshot);
    await done;
  }
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    state = TestBed.inject(DashboardState);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => {
    state.stop();
    http.verify({ ignoreCancelled: true });
    vi.useRealTimers();
  });
  it('loads exactly eight physical channels and joins source names', async () => {
    await load();
    expect(state.channels()).toHaveLength(8);
    expect(state.channels()[0].sourceName).toBe('Studio feed');
    expect(state.stale()).toBe(false);
  });
  it('keeps absent frequency and RF readback null', async () => {
    await load();
    expect(state.channels()[7].config?.frequency_khz).toBeNull();
    expect(state.channels()[7].canStart).toBe(false);
    expect(state.channels()[0].runtime?.rf_attenuation_db).toBeNull();
  });
  it('retains observations but marks an expired backend publication stale', async () => {
    const data = sample();
    data.status.system.snapshot_age_s = 3;
    data.status.system.snapshot_stale = true;
    data.status.system.controller_observation_stale = true;
    await load(data);
    expect(state.stale()).toBe(true);
    expect(state.channels()).toHaveLength(8);
    expect(state.channels()[0].confirmedOff).toBe(false);
    expect(state.data()?.status).toEqual(data.status);
  });
  it('handles missing sources without guessing', () => {
    const data = sample();
    data.sources = [];
    expect(compose(data)[0].sourceName).toBe('Source unavailable');
    expect(compose(data)[0].canStart).toBe(false);
  });
  it('derives PCM flow from consecutive counters, not cumulative totals', () => {
    const before = sample(),
      after = sample();
    after.status.modulators[0].pcm_bytes_consumed = 200;
    expect(compose(before)[0].pcm).toBe('Awaiting sample');
    expect(compose(before, before)[0].pcm).toBe('No flow observed');
    expect(compose(after, before)[0].pcm).toBe('Flowing');
  });
  it('keeps analog PCM bypass and latched errors distinct', () => {
    const data = sample();
    data.modulators[0].audio_mode = 'ANALOG';
    data.modulators[0].input_source_id = null;
    data.status.modulators[0].hardware_last_error = 'Historical QN_ERROR';
    expect(compose(data)[0].pcm).toBe('Bypass');
    expect(compose(data)[0].error).toBeNull();
  });
  it('displays current control errors', () => {
    const data = sample();
    data.status.modulators[0].tx_state = 'ERROR';
    data.status.modulators[0].hardware_last_error = 'RP unavailable';
    expect(compose(data)[0].error).toBe('RP unavailable');
  });
  it('rejects incomplete physical inventories', async () => {
    const data = sample();
    data.status.modulators.pop();
    await load(data);
    expect(state.stale()).toBe(true);
    expect(state.data()).toBeNull();
  });
  for (const action of ['start', 'stop'] as const)
    it(`sends per-channel ${action} and refreshes without optimistic TX changes`, async () => {
      await load();
      const before = state.channels()[1].runtime?.tx_state;
      const done = state.operate(1, action);
      await settle();
      expect(state.busy()).toBe(1);
      expect(state.channels()[1].runtime?.tx_state).toBe(before);
      const request = http.expectOne(`/api/v1/modulators/1/${action}`);
      expect(request.request.method).toBe('POST');
      request.flush(sample().status.modulators[1]);
      await settle();
      respond();
      await done;
      expect(state.busy()).toBeNull();
      expect(state.failures()).toEqual([]);
    });
  it('refreshes the replaced selection after individual START during global STOP ALL', async () => {
    const before = sample();
    before.status.system.global_stopped = true;
    before.modulators.forEach((m) => (m.enabled = m.id === 0 || m.id === 2));
    before.status.modulators.forEach((m) => {
      m.tx_state = 'OFF';
      m.hardware_tx_state = 'OFF';
    });
    await load(before);
    const done = state.operate(1, 'start');
    await settle();
    expect(state.busy()).toBe(1);
    expect(state.data()?.status.system.global_stopped).toBe(true);
    http.expectOne('/api/v1/modulators/1/start').flush({
      ...before.status.modulators[1],
      tx_state: 'ON',
    });
    await settle();
    // Neither the global intent nor other saved settings are guessed from a TX ACK.
    expect(state.data()?.status.system.global_stopped).toBe(true);
    expect(
      state
        .data()
        ?.modulators.filter((m) => m.enabled)
        .map((m) => m.id),
    ).toEqual([0, 2]);
    const after = structuredClone(before);
    after.status.system.global_stopped = false;
    after.modulators.forEach((m) => (m.enabled = m.id === 1));
    after.status.modulators[1].tx_state = 'ON';
    respond(after);
    await done;
    expect(state.busy()).toBeNull();
    expect(state.data()?.status.system.global_stopped).toBe(false);
    expect(
      state
        .data()
        ?.modulators.filter((m) => m.enabled)
        .map((m) => m.id),
    ).toEqual([1]);
    expect(state.channels()[1].runtime?.hardware_tx_state).toBe('OFF');
  });
  it('prevents duplicate and concurrent lifecycle requests', async () => {
    await load();
    const done = state.operate(0, 'stop');
    await settle();
    await state.operate(0, 'stop');
    await state.all('stop');
    http.expectOne('/api/v1/modulators/0/stop').flush(sample().status.modulators[0]);
    await settle();
    respond();
    await done;
  });
  it('START ALL uses desired-enabled bulk semantics and reports failures', async () => {
    await load();
    const done = state.all('start');
    await settle();
    http
      .expectOne('/api/v1/modulators/start-all')
      .flush(
        sample().status.modulators.map((m) =>
          m.id === 0 ? { id: 0, error: 'ERROR', message: 'RP unavailable' } : m,
        ),
      );
    await settle();
    respond();
    await done;
    expect(state.notice()).toContain('0 accepted, 1 failed');
    expect(state.notice()).toContain('Skipped disabled or unconfigured');
    expect(state.failures()).toEqual([{ id: 0, message: 'RP unavailable' }]);
  });
  it('navigation clears pending messages and prevents late result messages returning', async () => {
    await load();
    const done = state.operate(0, 'start');
    await settle();
    state.clearMessages();
    http.expectOne('/api/v1/modulators/0/start').flush(sample().status.modulators[0]);
    await settle();
    respond();
    await done;
    expect(state.notice()).toBe('');
    expect(state.failures()).toEqual([]);
  });
  it('STOP ALL uses the bulk endpoint and preserves per-ID failures', async () => {
    await load();
    const done = state.all('stop');
    await settle();
    http
      .expectOne('/api/v1/modulators/stop-all')
      .flush(
        sample().status.modulators.map((m) =>
          m.id === 4 ? { id: 4, error: 'ERROR', message: 'Disable not confirmed' } : m,
        ),
      );
    await settle();
    respond();
    await done;
    expect(state.failures()[0].id).toBe(4);
    expect(state.notice()).toContain('7 accepted, 1 failed');
  });
  it('STOP ALL transport failure does not claim success', async () => {
    await load();
    const done = state.all('stop');
    await settle();
    http.expectOne('/api/v1/modulators/stop-all').error(new ProgressEvent('network'));
    await settle();
    respond();
    await done;
    expect(state.failures()).toHaveLength(8);
    expect(state.notice()).toContain('outcome unknown');
  });
  it('retains previous configuration and recovers after a failed poll', async () => {
    await load();
    const previous = state.data();
    const failed = state.refresh();
    http.expectOne('/api/v1/status').error(new ProgressEvent('network'));
    http.match(() => true);
    await failed;
    expect(state.stale()).toBe(true);
    expect(state.data()).toBe(previous);
    await load();
    expect(state.stale()).toBe(false);
  });
  it('polls every five seconds without overlapping slow requests and stops cleanly', async () => {
    vi.useFakeTimers();
    state.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10000);
    const done = state.refresh();
    respond();
    await done;
    await vi.advanceTimersByTimeAsync(5000);
    const pending = http.match(() => true);
    expect(pending).toHaveLength(3);
    state.stop();
    expect(pending.every((r) => r.cancelled)).toBe(true);
    await vi.advanceTimersByTimeAsync(10000);
    http.expectNone('/api/v1/status');
  });
  function observed(on: boolean, age: number | null = 0, stale = false) {
    const value = sample();
    value.status.system.controller_observation_stale = stale;
    value.status.system.controller_observation_age_s = age;
    value.status.modulators[1].hardware_tx_state = on ? 'ON' : 'OFF';
    value.status.modulators[1].tx_state = on ? 'ON' : 'OFF';
    return value;
  }
  async function acceptedStart() {
    const done = state.operate(1, 'start');
    expect(state.pendingAction()).toBe('start');
    expect(state.actionFeedback()[1]).toBe('Starting...');
    http.expectOne('/api/v1/modulators/1/start').flush(observed(true).status.modulators[1]);
    await settle();
    expect(state.busy()).toBeNull();
    expect(state.commandStates()[1]).toBe('ON');
    return { done };
  }
  it('rejects a pre-action in-flight generation and performs a separate post-action refresh', async () => {
    await load(observed(false));
    const original = state.data();
    const old = state.refresh();
    const requests = http.match(() => true);
    const { done } = await acceptedStart();
    http.expectNone('/api/v1/status'); // Old request must finish; never overlap.
    const outdated = observed(false);
    outdated.modulators[1].rds_rt = 'Obsolete configuration';
    requests.find((r) => r.request.url.endsWith('/status'))!.flush(outdated.status);
    requests.find((r) => r.request.url.endsWith('/modulators'))!.flush(outdated.modulators);
    requests.find((r) => r.request.url.endsWith('/sources'))!.flush(outdated.sources);
    await old;
    await settle();
    expect(state.data()).toBe(original);
    expect(state.commandStates()[1]).toBe('ON');
    respond(observed(false));
    await done;
    expect(state.channels()[1].runtime?.hardware_tx_state).toBe('OFF');
    expect(state.channels()[1].runtime?.source_state).toBe('IDLE');
    expect(state.commandStates()[1]).toBe('ON');
    expect(state.notice()).not.toContain('Status refreshed below');
  });
  it('coalesces simultaneous mutation refreshes after the old request', async () => {
    const old = state.refresh();
    const first = state.refreshAfterMutation();
    const second = state.refreshAfterMutation();
    respond();
    await old;
    await settle();
    expect(state.data()).toBeNull();
    respond();
    await Promise.all([first, second]);
    http.expectNone('/api/v1/status');
  });
  it('accelerates only until a fresh post-command Controller observation confirms the target', async () => {
    vi.useFakeTimers();
    state.start();
    await vi.advanceTimersByTimeAsync(0);
    respond(observed(false));
    await settle();
    const { done } = await acceptedStart();
    respond(observed(false));
    await done;
    await vi.advanceTimersByTimeAsync(1000);
    respond(observed(true));
    await settle();
    expect(state.commandStates()[1]).toBeUndefined();
    expect(state.actionFeedback()[1]).toContain('Controller observation confirms ON');
    await vi.advanceTimersByTimeAsync(2999);
    expect(state.actionFeedback()[1]).toContain('Controller observation confirms ON');
    await vi.advanceTimersByTimeAsync(1001);
    expect(state.actionFeedback()[1]).toBeUndefined();
    await vi.advanceTimersByTimeAsync(999);
    http.expectNone('/api/v1/status');
    await vi.advanceTimersByTimeAsync(1);
    respond(observed(true));
    await settle();
  });
  it.each(['old', 'stale', 'unknown', 'publication stale'])(
    'does not mistake %s RP evidence for confirmation and bounds acceleration',
    async (kind) => {
      vi.useFakeTimers();
      state.start();
      await vi.advanceTimersByTimeAsync(0);
      respond(observed(false));
      await settle();
      const { done } = await acceptedStart();
      const lag = observed(
        true,
        kind === 'old' ? 100 : kind === 'unknown' ? null : 0,
        kind === 'stale',
      );
      lag.status.system.snapshot_stale = kind === 'publication stale';
      respond(lag);
      await done;
      for (let i = 0; i < 10; i++) {
        await vi.advanceTimersByTimeAsync(1000);
        respond(lag);
        await settle();
      }
      expect(state.commandStates()[1]).toBe('ON');
      expect(state.actionFeedback()[1]).toContain('confirmation delayed');
      expect(state.failures()).toEqual([]);
      await vi.advanceTimersByTimeAsync(4999);
      http.expectNone('/api/v1/status');
      await vi.advanceTimersByTimeAsync(1);
      respond(observed(true));
      await settle();
      expect(state.commandStates()[1]).toBeUndefined();
    },
  );
  it('never overlaps slow accelerated refreshes and cancels them on teardown', async () => {
    vi.useFakeTimers();
    state.start();
    await vi.advanceTimersByTimeAsync(0);
    respond(observed(false));
    await settle();
    const { done } = await acceptedStart();
    respond(observed(false));
    await done;
    await vi.advanceTimersByTimeAsync(1000);
    const requests = http.match(() => true);
    expect(requests).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(9000);
    http.expectNone('/api/v1/status');
    expect(state.actionFeedback()[1]).toContain('confirmation delayed');
    state.ngOnDestroy();
    expect(requests.every((r) => r.cancelled)).toBe(true);
    await vi.advanceTimersByTimeAsync(20000);
    http.expectNone('/api/v1/status');
    expect(state.commandStates()).toEqual({});
  });
  it('releases timed-out commands without retrying them or claiming a target state', async () => {
    vi.useFakeTimers();
    await load(observed(false));
    const done = state.operate(1, 'start');
    const request = http.expectOne('/api/v1/modulators/1/start');
    await vi.advanceTimersByTimeAsync(120000);
    expect(request.cancelled).toBe(true);
    expect(state.busy()).toBeNull();
    expect(state.commandStates()[1]).toBeUndefined();
    expect(state.actionFeedback()[1]).toContain('outcome unknown');
    respond(observed(true));
    await done;
    expect(state.channels()[1].runtime?.hardware_tx_state).toBe('ON');
    http.expectNone('/api/v1/modulators/1/start');
  });
  it('cancels a pending command without launching a refresh after teardown', async () => {
    await load();
    const done = state.operate(1, 'start');
    const request = http.expectOne('/api/v1/modulators/1/start');
    state.ngOnDestroy();
    await done;
    expect(request.cancelled).toBe(true);
    expect(state.busy()).toBeNull();
    http.expectNone('/api/v1/status');
  });
  it('shows START ALL progress only for backend-eligible channels and blocks conflicting commands', async () => {
    const data = observed(false);
    data.modulators[1].enabled = true;
    data.modulators[1].audio_mode = 'ANALOG';
    data.modulators[1].input_source_id = null;
    data.modulators[2].enabled = true;
    data.modulators[2].input_source_id = null;
    data.modulators[3].enabled = true;
    data.modulators[3].input_source_id = 999; // Backend attempts validation, not a skip.
    data.modulators[7].enabled = true; // Missing frequency: skipped.
    await load(data);
    const done = state.all('start');
    expect(state.bulkPendingIds()).toEqual([0, 1, 3]);
    expect(state.pendingAction()).toBe('start');
    expect(state.actionFeedback()[1]).toBe('Starting...');
    expect(state.commandStates()).toEqual({});
    await state.operate(0, 'stop');
    await state.all('stop');
    http.expectNone('/api/v1/modulators/0/stop');
    http.expectNone('/api/v1/modulators/stop-all');
    http.expectOne('/api/v1/modulators/start-all').flush([
      { ...data.status.modulators[0], tx_state: 'ON' },
      { id: 1, error: 'RP_ERROR', message: 'Enable failed' },
      // Missing id=3 is unconfirmed, not successful.
    ]);
    await settle();
    expect(state.bulkPendingIds()).toEqual([]);
    expect(state.busy()).toBeNull();
    expect(state.commandStates()).toEqual({ 0: 'ON' });
    expect(state.actionFeedback()[1]).toContain('Enable failed');
    expect(state.actionFeedback()[3]).toContain('No unique result');
    expect(state.actionFeedback()[2]).toBeUndefined();
    expect(state.channels()[1].runtime?.hardware_tx_state).toBe('OFF');
    respond(data);
    await done;
    expect(state.failures().map((v) => v.id)).toEqual([1, 3]);
  });
  it('shows STOP ALL progress for active, uncertain or stale channels, not freshly confirmed OFF', async () => {
    const data = observed(false);
    data.status.modulators[2].tx_state = 'ERROR';
    data.status.modulators[3].hardware_tx_state = 'ON';
    data.status.modulators[4].hardware_tx_state = null;
    await load(data);
    let done = state.all('stop');
    expect(state.bulkPendingIds()).toEqual([0, 2, 3, 4]);
    expect(state.pendingAction()).toBe('stop');
    expect(state.isPending(1)).toBe(false);
    http
      .expectOne('/api/v1/modulators/stop-all')
      .flush(data.status.modulators.map((r) => ({ ...r, tx_state: 'OFF' })));
    await settle();
    respond(data);
    await done;
    state.stale.set(true);
    done = state.all('stop');
    expect(state.bulkPendingIds()).toHaveLength(8);
    http
      .expectOne('/api/v1/modulators/stop-all')
      .flush(data.status.modulators.map((r) => ({ ...r, tx_state: 'OFF' })));
    await settle();
    respond(data);
    await done;
  });
  it('bulk actions reject pre-action refresh results and share accelerated confirmation polling', async () => {
    vi.useFakeTimers();
    state.start();
    await vi.advanceTimersByTimeAsync(0);
    const initial = observed(false);
    initial.modulators[1].enabled = true;
    respond(initial);
    await settle();
    const previous = state.data();
    const old = state.refresh();
    const requests = http.match(() => true);
    const done = state.all('start');
    http
      .expectOne('/api/v1/modulators/start-all')
      .flush(initial.status.modulators.map((r) => ({ ...r, tx_state: 'ON' })));
    await settle();
    expect(state.commandStates()).toEqual({ 0: 'ON', 1: 'ON' });
    expect(state.bulkPendingIds()).toEqual([]);
    for (const r of requests)
      r.flush(
        r.request.url.endsWith('/status')
          ? initial.status
          : r.request.url.endsWith('/sources')
            ? initial.sources
            : initial.modulators,
      );
    await old;
    await settle();
    expect(state.data()).toBe(previous);
    respond(initial);
    await done;
    await vi.advanceTimersByTimeAsync(1000);
    const confirmed = observed(true);
    respond(confirmed);
    await settle();
    expect(state.commandStates()).toEqual({});
    expect(state.actionFeedback()[0]).toContain('Controller observation confirms ON');
    expect(state.actionFeedback()[1]).toContain('Controller observation confirms ON');
    await vi.advanceTimersByTimeAsync(4999);
    http.expectNone('/api/v1/status');
    await vi.advanceTimersByTimeAsync(1);
    respond(confirmed);
    await settle();
  });
  it('bulk request failure releases progress, never retries and bounds unknown-outcome polling', async () => {
    vi.useFakeTimers();
    state.start();
    await vi.advanceTimersByTimeAsync(0);
    const lag = observed(false, 100, true);
    respond(lag);
    await settle();
    const done = state.all('stop');
    http.expectOne('/api/v1/modulators/stop-all').error(new ProgressEvent('network'));
    await settle();
    expect(state.busy()).toBeNull();
    expect(state.bulkPendingIds()).toEqual([]);
    expect(state.commandStates()).toEqual({});
    expect(state.actionFeedback()[0]).toContain('Outcome unconfirmed');
    respond(lag);
    await done;
    for (let i = 0; i < 10; i++) {
      await vi.advanceTimersByTimeAsync(1000);
      respond(lag);
      await settle();
    }
    expect(state.actionFeedback()[0]).toBe('Outcome unknown; Controller confirmation delayed.');
    http.expectNone('/api/v1/modulators/stop-all');
    await vi.advanceTimersByTimeAsync(4999);
    http.expectNone('/api/v1/status');
    await vi.advanceTimersByTimeAsync(1);
    const requests = http.match(() => true);
    expect(requests).toHaveLength(3);
    state.stop();
    expect(requests.every((r) => r.cancelled)).toBe(true);
    await vi.advanceTimersByTimeAsync(20000);
    http.expectNone('/api/v1/status');
  });
  it('teardown cancels a pending bulk command and clears every card without another refresh', async () => {
    await load();
    const done = state.all('stop');
    const request = http.expectOne('/api/v1/modulators/stop-all');
    state.stop();
    await done;
    expect(request.cancelled).toBe(true);
    expect(state.bulkPendingIds()).toEqual([]);
    expect(state.pendingAction()).toBeNull();
    expect(state.actionFeedback()).toEqual({});
    http.expectNone('/api/v1/status');
  });
});
