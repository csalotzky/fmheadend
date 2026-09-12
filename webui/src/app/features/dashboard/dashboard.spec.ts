import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Dashboard } from './dashboard';
import { DashboardState, compose } from './dashboard-state';
import { Shell } from '../../layout/shell';
import { sample } from './test-fixtures';

describe('Operational display', () => {
  beforeEach(() =>
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }),
  );
  function setup() {
    const state = TestBed.inject(DashboardState),
      data = sample();
    state.data.set(data);
    state.channels.set(compose(data));
    state.stale.set(false);
    state.loading.set(false);
    return state;
  }
  it('renders eight cards, prominent frequencies, PS/PI/ECC and null RF', () => {
    setup();
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('app-channel-card')).toHaveLength(8);
    expect([...el.querySelectorAll('.channel-id')].map((node) => node.textContent?.trim())).toEqual(
      Array.from({ length: 8 }, (_, id) => `TX ${id}`),
    );
    expect(el.textContent).toContain('91.60');
    expect(el.textContent).toContain('Not configured');
    expect(el.textContent).toContain('Studio feed');
    expect(el.textContent).toContain('A213');
    expect(el.textContent).toContain('E0');
    expect(el.querySelector('.rf')?.textContent).not.toContain('96');
  });
  it('distinguishes unassigned, missing and assigned source runtime states', () => {
    const state = setup();
    const data = sample();
    data.modulators[0].input_source_id = null;
    data.modulators[1].input_source_id = 999;
    data.status.modulators[0].source_state = 'IDLE';
    data.status.modulators[1].source_state = 'IDLE';
    data.status.modulators[2].source_state = 'IDLE';
    data.status.modulators[3].source_state = 'RUNNING';
    state.channels.set(compose(data));
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const cards = (fixture.nativeElement as HTMLElement).querySelectorAll('app-channel-card');
    const sourceState = (id: number) =>
      cards[id].querySelector('.runtime span')?.textContent?.replace(/\s+/g, ' ').trim();
    expect(sourceState(0)).toBe('Source Not assigned');
    expect(cards[0].querySelector('.source-name')?.textContent).toBe('No source assigned');
    expect(sourceState(1)).toBe('Source Unavailable');
    expect(cards[1].querySelector('.source-name')?.textContent).toBe('Source unavailable');
    expect(sourceState(2)).toBe('Source IDLE');
    expect(sourceState(3)).toBe('Source RUNNING');
    expect(cards[2].querySelector('.source-name')?.textContent).toBe('Studio feed');
    // Staleness hides runtime observations, not known assignment distinctions.
    state.stale.set(true);
    fixture.detectChanges();
    expect(sourceState(0)).toBe('Source Not assigned');
    expect(sourceState(1)).toBe('Source Unavailable');
    expect(sourceState(2)).toBe('Source Unknown');
    expect(data.status.modulators[0].source_state).toBe('IDLE');
  });
  it.each(['Flowing', 'Idle', 'Unavailable'] as const)(
    'labels the existing %s counter state as Digital audio',
    (expected) => {
      const state = setup();
      const previous = sample();
      const data = sample();
      if (expected === 'Flowing') data.status.modulators[0].pcm_bytes_consumed += 1;
      if (expected === 'Idle') data.status.modulators[0].tx_state = 'OFF';
      if (expected === 'Unavailable') data.status.system.mux_state = 'ERROR';
      state.channels.set(compose(data, previous));
      const fixture = TestBed.createComponent(Dashboard);
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;
      const audio = el.querySelector('.runtime span:nth-child(2)');
      expect(audio?.textContent?.replace(/\s+/g, ' ').trim()).toBe(`Digital audio ${expected}`);
      expect(el.textContent).not.toContain('PCM');
      expect(state.channels()[0].pcm).toBe(expected);
    },
  );
  it('labels analog input as Audio rather than an idle digital path', () => {
    const state = setup();
    const data = sample();
    data.modulators[0].audio_mode = 'ANALOG';
    data.modulators[0].input_source_id = null;
    state.channels.set(compose(data));
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const audio = el.querySelector('.runtime span');
    expect(audio?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Audio input: Analog');
    expect(state.channels()[0].pcm).toBe('Bypass');
    state.stale.set(true);
    fixture.detectChanges();
    expect(audio?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Audio input: Analog');
  });
  it('uses real START and STOP buttons without card navigation', () => {
    const state = setup();
    const action = vi.spyOn(state, 'operate').mockResolvedValue();
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    (el.querySelector('button[aria-label="STOP TX 0"]') as HTMLButtonElement).click();
    expect(action).toHaveBeenCalledWith(0, 'stop');
    (el.querySelector('button[aria-label="START TX 1"]') as HTMLButtonElement).click();
    expect(action).toHaveBeenCalledWith(1, 'start');
    expect(el.querySelector('a button')).toBeNull();
  });
  it('shows reported OFF and current ERROR with pending controls disabled', () => {
    const state = setup();
    const data = sample();
    data.status.modulators[0].tx_state = 'ERROR';
    data.status.modulators[0].hardware_tx_state = 'ERROR';
    data.status.modulators[0].last_error = 'RP unavailable';
    data.status.modulators[1].tx_state = 'STARTING';
    state.channels.set(compose(data));
    state.busy.set(0);
    state.failures.set([{ id: 2, message: 'TX configuration rejected' }]);
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('ERROR');
    expect(el.textContent).not.toContain('STARTING');
    expect(el.textContent).toContain('OFF');
    expect(el.textContent).toContain('RP unavailable');
    expect(el.querySelectorAll('app-channel-card')[2].textContent).toContain(
      'TX configuration rejected',
    );
    expect([...el.querySelectorAll('button')].every((b) => b.disabled)).toBe(true);
  });
  it('uses hardware-reported TX state instead of desired enabled or historical errors', () => {
    const state = setup();
    const data = sample();
    data.modulators[0].enabled = false;
    data.status.modulators[0].hardware_tx_state = 'ON';
    data.status.modulators[0].hardware_last_error = 'QN_ERROR';
    data.modulators[1].enabled = true;
    data.status.modulators[1].hardware_tx_state = 'OFF';
    data.status.modulators[2].hardware_tx_state = 'ERROR';
    data.status.modulators[2].hardware_last_error = 'QN_ERROR';
    data.modulators[3].enabled = true;
    data.status.modulators[3].tx_state = 'ERROR';
    data.status.modulators[3].hardware_tx_state = null;
    data.modulators[4].enabled = true;
    data.status.modulators[4].tx_state = 'ON';
    data.status.modulators[4].hardware_tx_state = null;
    state.channels.set(compose(data));
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const cards = el.querySelectorAll('app-channel-card');
    const reported = (id: number) =>
      cards[id].querySelector('.channel-control span')?.textContent?.trim();
    expect(reported(0)).toBe('Reported state: ON');
    expect(reported(1)).toBe('Reported state: OFF');
    expect(reported(2)).toBe('Reported state: ERROR');
    expect(reported(3)).toBe('Reported state: UNKNOWN');
    expect(reported(4)).toBe('Reported state: UNKNOWN');
    expect(cards[0].querySelector('.fault')).toBeNull();
    expect(cards[0].querySelector('.card-error')).toBeNull();
    expect(cards[2].querySelector('.fault')).not.toBeNull();
    expect(cards[2].querySelector('.card-error')?.textContent).toContain('QN_ERROR');
    expect(el.textContent).not.toContain('Desired');
    // Desired state remains available for the existing STOP behavior.
    expect(cards[1].querySelector('button')?.getAttribute('aria-label')).toBe('STOP TX 1');
    state.stale.set(true);
    fixture.detectChanges();
    expect(reported(0)).toBe('Reported state: UNKNOWN');
    expect(cards[0].querySelector('.on')).toBeNull();
  });
  it('shows DEGRADED and global operations in the persistent shell', () => {
    const state = setup();
    vi.spyOn(state, 'start').mockImplementation(() => {});
    const action = vi.spyOn(state, 'all').mockResolvedValue();
    const data = sample();
    data.status.system.system_state = 'DEGRADED';
    state.data.set(data);
    const fixture = TestBed.createComponent(Shell);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('DEGRADED');
    expect(el.textContent).toContain('1 / 8 TX ON');
    expect(el.textContent).toContain('PENDING');
    const buttons = el.querySelectorAll('.global-actions button');
    (buttons[0] as HTMLButtonElement).click();
    expect(action).toHaveBeenCalledWith('start');
    (buttons[1] as HTMLButtonElement).click();
    expect(action).toHaveBeenCalledWith('stop');
  });
  it('retains shell/navigation without displaying fake healthy status when offline', () => {
    const state = setup();
    vi.spyOn(state, 'start').mockImplementation(() => {});
    state.stale.set(true);
    const fixture = TestBed.createComponent(Shell);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('BACKEND UNAVAILABLE');
    expect(el.querySelectorAll('nav a')).toHaveLength(6);
    expect(el.textContent).not.toContain('1 / 8 TX ON');
  });
});
