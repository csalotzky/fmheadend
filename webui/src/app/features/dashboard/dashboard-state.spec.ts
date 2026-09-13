import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DashboardState, compose } from './dashboard-state';
import { Snapshot } from '../../core/api/models';
import { sample } from './test-fixtures';

describe('Dashboard API composition and operations', () => {
  let state: DashboardState, http: HttpTestingController;
  function respond(snapshot = sample()) {
    http.expectOne('/api/v1/status').flush(snapshot.status);
    http.expectOne('/api/v1/modulators').flush(snapshot.modulators);
    http.expectOne('/api/v1/sources').flush(snapshot.sources);
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
      await Promise.resolve();
      expect(state.busy()).toBe(1);
      expect(state.channels()[1].runtime?.tx_state).toBe(before);
      const request = http.expectOne(`/api/v1/modulators/1/${action}`);
      expect(request.request.method).toBe('POST');
      request.flush(sample().status.modulators[1]);
      await Promise.resolve();
      respond();
      await done;
      expect(state.busy()).toBeNull();
      expect(state.failures()).toEqual([]);
    });
  it('prevents duplicate and concurrent lifecycle requests', async () => {
    await load();
    const done = state.operate(0, 'stop');
    await Promise.resolve();
    await state.operate(0, 'stop');
    await state.all('stop');
    http.expectOne('/api/v1/modulators/0/stop').flush(sample().status.modulators[0]);
    await Promise.resolve();
    respond();
    await done;
  });
  it('START ALL runs eligible channels sequentially and reports partial failures', async () => {
    await load();
    const done = state.all('start');
    await Promise.resolve();
    for (let id = 0; id < 7; id++) {
      const request = http.expectOne(`/api/v1/modulators/${id}/start`);
      http.expectNone(`/api/v1/modulators/${id + 1}/start`);
      if (id === 2)
        request.flush(
          { error: 'DEPENDENCY_UNAVAILABLE', message: 'RP unavailable' },
          { status: 503, statusText: 'Unavailable' },
        );
      else request.flush(sample().status.modulators[id]);
      await Promise.resolve();
    }
    respond();
    await done;
    expect(state.notice()).toContain('6 accepted, 1 failed');
    expect(state.notice()).toContain('7');
    expect(state.failures()).toEqual([{ id: 2, message: 'RP unavailable' }]);
  });
  it('STOP ALL uses the bulk endpoint and preserves per-ID failures', async () => {
    await load();
    const done = state.all('stop');
    await Promise.resolve();
    http
      .expectOne('/api/v1/modulators/stop-all')
      .flush(
        sample().status.modulators.map((m) =>
          m.id === 4 ? { id: 4, error: 'ERROR', message: 'Disable not confirmed' } : m,
        ),
      );
    await Promise.resolve();
    respond();
    await done;
    expect(state.failures()[0].id).toBe(4);
    expect(state.notice()).toContain('7 accepted, 1 failed');
  });
  it('STOP ALL transport failure does not claim success', async () => {
    await load();
    const done = state.all('stop');
    await Promise.resolve();
    http.expectOne('/api/v1/modulators/stop-all').error(new ProgressEvent('network'));
    await Promise.resolve();
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
});
