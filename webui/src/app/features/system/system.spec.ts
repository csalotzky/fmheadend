import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SystemPage } from './system-page';
import { systemInfo } from './test-fixtures';
import { DashboardState } from '../dashboard/dashboard-state';
import { sample } from '../dashboard/test-fixtures';
import { planOptions, frequencyLabel } from '../../core/api/system-api';
import { routes } from '../../app.routes';

describe('System and region plans', () => {
  beforeEach(() =>
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }),
  );
  it.each([
    [0, 76000, 108000, 50, 641],
    [1, 87500, 108000, 100, 206],
    [2, 88100, 107900, 200, 100],
    [3, 76100, 98900, 100, 229],
  ])('generates exact plan %s', (region, min, max, step, count) => {
    const options = planOptions({
      region,
      region_name: 'Test',
      fm_min_khz: min,
      fm_max_khz: max,
      fm_step_khz: step,
    });
    expect(options).toHaveLength(count);
    expect(options[0].value).toBe(min);
    expect(options[count - 1].value).toBe(max);
    options.forEach((o, i) => expect(o.value).toBe(min + i * step));
    expect(frequencyLabel(76050)).toBe('76.05 MHz');
  });
  it.each(['OK', 'UNRESPONSIVE', 'INCOMPATIBLE'])('renders partial RP state %s', (state) => {
    const shared = TestBed.inject(DashboardState);
    const data = sample();
    data.status.system.rp2354_state = state;
    data.status.system.rp_last_error_code = 'QN_ERROR';
    shared.data.set(data);
    shared.stale.set(false);
    const f = TestBed.createComponent(SystemPage);
    f.detectChanges();
    TestBed.inject(HttpTestingController).expectOne('/api/v1/system/info').flush(systemInfo);
    TestBed.inject(HttpTestingController).expectOne('/api/v1/system/logs').flush([]);
    f.detectChanges();
    const text = f.nativeElement.textContent;
    expect(text).toContain(state);
    expect(text).toContain('Europe / standard');
    expect(text).toContain('100 kHz');
    expect(text).toContain('1 - Europe / standard');
    expect(text).toContain('87.5-108.0 MHz');
    expect(text).toContain('I2C errors');
    expect(text).toContain('FM frequency plan');
    expect(text).not.toContain('Database schema');
    expect(text).not.toContain('Read-only diagnostics');
    expect(text).not.toContain('Raster');
    expect(text).not.toContain('Counters and historical codes');
    expect(text).toContain('Multiplexer');
    expect(f.nativeElement.querySelector('button').textContent).toContain('Refresh logs');
    expect(f.nativeElement.querySelectorAll('.error')).toHaveLength(0);
    shared.stale.set(true);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Backend unavailable');
    TestBed.inject(HttpTestingController).verify();
  });
  it('keeps runtime visible if information request fails', () => {
    const shared = TestBed.inject(DashboardState);
    shared.data.set(sample());
    shared.stale.set(false);
    const f = TestBed.createComponent(SystemPage);
    f.detectChanges();
    TestBed.inject(HttpTestingController)
      .expectOne('/api/v1/system/info')
      .flush({}, { status: 503, statusText: 'Unavailable' });
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('System information unavailable');
    expect(f.nativeElement.textContent).toContain('Multiplexer');
  });
  it('completes the sitemap without removed feature routes', () => {
    expect(routes.find((r) => r.path === 'system')?.component).toBe(SystemPage);
    expect(routes.some((r) => /band.?scan/i.test(r.path ?? ''))).toBe(false);
  });
  it('renders bounded newest-first logs, refreshes without overlap and stops on destroy', () => {
    const shared = TestBed.inject(DashboardState);
    shared.data.set(sample());
    shared.stale.set(false);
    const f = TestBed.createComponent(SystemPage);
    f.detectChanges();
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/v1/system/info').flush(systemInfo);
    const first = http.expectOne('/api/v1/system/logs');
    f.componentInstance.refreshLogs();
    http.expectNone('/api/v1/system/logs');
    first.flush(
      Array.from({ length: 100 }, (_, i) => ({
        timestamp: '2026-09-24T12:00:00Z',
        severity: 'WARNING',
        logger: 'fmheadendd.rp2354',
        message: `entry ${100 - i}`,
      })),
    );
    f.detectChanges();
    const rows = f.nativeElement.querySelectorAll('.log-entry');
    expect(rows).toHaveLength(100);
    expect(rows[0].textContent).toContain('entry 100');
    expect(rows[99].textContent).toContain('entry 1');
    expect(rows[0].textContent).toContain('fmheadendd.rp2354');
    f.nativeElement.querySelector('button').click();
    http.expectOne('/api/v1/system/logs').flush([]);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('No recent backend log entries');
    shared.data.set(sample());
    f.detectChanges();
    http.expectOne('/api/v1/system/info').flush(systemInfo);
    const pending = http.expectOne('/api/v1/system/logs');
    f.destroy();
    expect(pending.cancelled).toBe(true);
    shared.data.set(sample());
    TestBed.tick();
    http.expectNone('/api/v1/system/logs');
    http.verify();
  });
  it('shows log fetch failure independently of counters and retries manually', () => {
    const shared = TestBed.inject(DashboardState);
    shared.data.set(sample());
    shared.stale.set(false);
    const f = TestBed.createComponent(SystemPage);
    f.detectChanges();
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/v1/system/info').flush(systemInfo);
    http.expectOne('/api/v1/system/logs').flush({}, { status: 503, statusText: 'Unavailable' });
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Logs unavailable');
    expect(f.nativeElement.textContent).toContain('I2C errors');
    expect(f.nativeElement.querySelector('button').disabled).toBe(false);
    f.nativeElement.querySelector('button').click();
    http.expectOne('/api/v1/system/logs').flush([]);
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain('Logs unavailable');
    http.verify();
  });
});
