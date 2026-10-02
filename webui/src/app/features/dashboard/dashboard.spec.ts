import { TestBed } from '@angular/core/testing';
import { provideRouter, Router, NavigationEnd } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ChannelCard } from './channel-card';
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
  it('presents feeding RDS as Running without changing other states', () => {
    const state = setup(),
      data = sample();
    const states = ['FEEDING', 'DISABLED', 'ERROR', 'IDLE'] as const;
    states.forEach((value, i) => (data.status.modulators[i].rds_state = value));
    state.channels.set(compose(data));
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    const cards = (fixture.nativeElement as HTMLElement).querySelectorAll('app-channel-card');
    const label = (i: number) =>
      cards[i].querySelector('.runtime span:last-child')?.textContent?.replace(/\s+/g, ' ').trim();
    expect(label(0)).toBe('RDS Running');
    states
      .slice(1)
      .forEach((value, i) =>
        expect(label(i + 1)).toBe(`RDS ${value[0] + value.slice(1).toLowerCase()}`),
      );
    expect(data.status.modulators[0].rds_state).toBe('FEEDING');
    state.stale.set(true);
    fixture.detectChanges();
    expect(label(0)).toBe('RDS Unknown');
  });
  it.each([
    ['RUNNING', 'Running'],
    ['FLOWING', 'Flowing'],
    ['ERROR', 'Error'],
    ['RECONNECT_PENDING', 'Reconnect pending'],
    ['Not assigned', 'Not assigned'],
    ['No flow observed', 'No flow observed'],
  ])('humanizes runtime status %s without changing prose', (raw, expected) => {
    const card = TestBed.createComponent(ChannelCard);
    expect(card.componentInstance.humanizeStatus(raw)).toBe(expected);
  });
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
    expect(sourceState(2)).toBe('Source Idle');
    expect(sourceState(3)).toBe('Source Running');
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
    'labels the existing %s counter state as Digital Audio',
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
      expect(audio?.textContent?.replace(/\s+/g, ' ').trim()).toBe(`Digital Audio ${expected}`);
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
    expect(el.querySelectorAll('app-channel-card')[2].textContent).not.toContain(
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
    // A bulk-stopped channel remains desired-enabled but offers START while OFF.
    expect(cards[1].querySelector('button')?.getAttribute('aria-label')).toBe('START TX 1');
    state.stale.set(true);
    fixture.detectChanges();
    expect(reported(0)).toBe('Reported state: UNKNOWN');
    expect(cards[0].querySelector('.on')).toBeNull();
  });
  it('shows profile names only for digital sources with a profile', () => {
    const state = setup();
    const data = sample();
    data.sources[0].audio_profile_path = 'custom/studio.json';
    data.modulators[1].audio_mode = 'ANALOG';
    state.channels.set(compose(data));
    state.profileNames.set({ 'custom/studio.json': 'Studio processing' });
    const f = TestBed.createComponent(Dashboard);
    f.detectChanges();
    const cards = f.nativeElement.querySelectorAll('app-channel-card');
    expect(cards[0].textContent).toContain('Audio Profile Studio processing');
    expect(cards[1].textContent).not.toContain('Audio Profile');
    expect(f.nativeElement.textContent).not.toContain('8 physical channels');
  });
  it('loads real version branding and clears transient messages on navigation', () => {
    const state = setup();
    vi.spyOn(state, 'start').mockImplementation(() => {});
    state.notice.set('Operation accepted');
    const f = TestBed.createComponent(Shell);
    f.detectChanges();
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/v1/system/info').flush({ fmheadend_version: '1.2.3' });
    http.expectOne('/api/v1/audio-profiles/files').flush([]);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('Version 1.2.3');
    expect(f.nativeElement.textContent).toContain('MULTI-CHANNEL FM MODULATOR');
    const events = TestBed.inject(Router).events as import('rxjs').Subject<
      import('@angular/router').Event
    >;
    events.next(new NavigationEnd(1, '/sources', '/sources'));
    expect(state.notice()).toBe('');
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
    expect(
      Array.from(el.querySelectorAll('nav a')).map((link) => link.getAttribute('href')),
    ).toEqual(['/dashboard', '/sources', '/modulators', '/audio-profiles', '/system']);
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
    expect(el.querySelectorAll('nav a')).toHaveLength(5);
    expect(el.textContent).not.toContain('1 / 8 TX ON');
  });
  it('omits global banners on every route and retains action notifications', () => {
    const state = setup();
    vi.spyOn(state, 'start').mockImplementation(() => {});
    const data = sample();
    data.status.system.last_error = 'GET_INFO response CRC mismatch';
    data.status.system.system_state = 'DEGRADED';
    state.data.set(data);
    const f = TestBed.createComponent(Shell);
    const events = TestBed.inject(Router).events as import('rxjs').Subject<
      import('@angular/router').Event
    >;
    for (const path of ['dashboard', 'sources', 'modulators', 'audio-profiles', 'system']) {
      events.next(new NavigationEnd(1, '/' + path, '/' + path));
      state.notice.set('STOP ALL: 7 accepted, 1 failed.');
      state.failures.set([{ id: 3, message: 'SET_TX_ENABLED outcome unknown (AMBIGUOUS)' }]);
      f.detectChanges();
      expect(f.nativeElement.textContent).not.toContain('GET_INFO response CRC mismatch');
      expect(f.nativeElement.querySelector('.banner.warning')).toBeNull();
      expect(f.nativeElement.textContent).toContain('DEGRADED');
      expect(f.nativeElement.textContent).toContain('STOP ALL: 7 accepted, 1 failed.');
      expect(f.nativeElement.textContent).toContain('SET_TX_ENABLED outcome unknown (AMBIGUOUS)');
    }
  });
  it('uses error scope, preserving a target fault despite global stale observations', () => {
    const state = setup(),
      data = sample();
    data.status.system.last_error = 'global read failed';
    for (const row of data.status.modulators) {
      row.last_error = 'global read failed';
      row.last_error_scope = 'GLOBAL';
      row.rds_last_error = 'global session failed';
      row.rds_last_error_scope = 'GLOBAL';
    }
    data.status.modulators[3].last_error = 'QN configuration rejected';
    data.status.modulators[3].last_error_scope = 'TX';
    state.channels.set(compose(data));
    state.stale.set(true);
    state.failures.set(
      data.status.modulators.map((r) => ({ id: r.id, message: 'global read failed' })),
    );
    const f = TestBed.createComponent(Dashboard);
    f.detectChanges();
    expect(f.nativeElement.textContent).not.toContain('global read failed');
    expect(f.nativeElement.textContent).not.toContain('global session failed');
    expect(f.nativeElement.querySelectorAll('.card-error')).toHaveLength(1);
    expect(f.nativeElement.querySelectorAll('app-channel-card')[3].textContent).toContain(
      'QN configuration rejected',
    );
  });
  it('shows persisted OFF configuration on refresh and fresh load without fabricating readback', () => {
    const state = setup(),
      data = sample();
    data.status.modulators[0].tx_state = 'OFF';
    data.status.modulators[0].hardware_tx_state = 'OFF';
    data.status.modulators[0].rf_attenuation_db = 6;
    data.modulators[0] = {
      ...data.modulators[0],
      frequency_khz: 99500,
      rf_attenuation_db: 9,
      audio_mode: 'ANALOG',
      stereo: false,
      preemphasis_us: 75,
      rds_pi: 0x1234,
      rds_ecc: 0xe1,
      rds_ps: [{ text: 'NEW PS', duration_s: 5 }],
      rds_af_khz: [101100],
    };
    state.channels.set(compose(data, sample()));
    const f = TestBed.createComponent(Dashboard);
    f.detectChanges();
    for (const channels of [compose(data, sample()), compose(structuredClone(data))]) {
      state.channels.set(channels);
      f.detectChanges();
      const card = f.nativeElement.querySelector('app-channel-card');
      for (const text of [
        '99.50',
        '-9.0 dB',
        'Analog',
        'Mono',
        '75',
        '1234',
        'E1',
        'NEW PS',
        '101.1',
      ])
        expect(card.textContent).toContain(text);
      expect(data.status.modulators[0].rf_attenuation_db).toBe(6);
    }
  });
  it.each(['ON', 'STARTING', 'stale', 'unknown'] as const)(
    'does not switch %s RF observation to saved OFF values',
    (mode) => {
      const state = setup(),
        data = sample();
      data.modulators[0].rf_attenuation_db = 9;
      data.status.modulators[0].rf_attenuation_db = 6;
      data.status.modulators[0].tx_state =
        mode === 'ON' ? 'ON' : mode === 'STARTING' ? 'STARTING' : 'OFF';
      data.status.modulators[0].hardware_tx_state =
        mode === 'unknown' ? null : mode === 'ON' ? 'ON' : 'OFF';
      data.status.system.rp_observation_stale = mode === 'stale';
      state.channels.set(compose(data));
      const f = TestBed.createComponent(Dashboard);
      f.detectChanges();
      const rf = f.nativeElement.querySelector('.rf').textContent;
      expect(rf).not.toContain('-9.0 dB');
      expect(rf).toContain('-6.0 dB');
      state.stale.set(true);
      f.detectChanges();
      expect(f.nativeElement.querySelector('.rf').textContent).not.toContain('-6.0 dB');
    },
  );
  it('shows published selected RT and QN soft clipping without frontend RT selection', () => {
    const state = setup(),
      data = sample();
    for (const config of data.modulators) {
      config.rds_enabled = true;
      config.rds_rt_enabled = true;
      config.rds_rt = 'Saved fallback must not override selection';
    }
    data.status.modulators[0].rds_rt_effective = 'X'.repeat(64);
    data.status.modulators[1].rds_rt_effective = '';
    data.modulators[0].soft_clipping = true;
    state.channels.set(compose(data));
    const f = TestBed.createComponent(Dashboard);
    f.detectChanges();
    const cards = f.nativeElement.querySelectorAll('app-channel-card');
    expect(cards[0].querySelector('.rt dd').textContent).toBe('X'.repeat(64));
    expect(cards[0].querySelector('.rt').title).toContain('queued RDS transmission may lag');
    expect(cards[1].querySelector('.rt dd').textContent).toBe('\u2014');
    expect(cards[0].querySelector('.audio-settings').textContent).toContain('Soft clipping ON');
    expect(cards[1].querySelector('.audio-settings').textContent).toContain('Soft clipping OFF');
    for (const selected of ['Artist - Track', 'Runtime override', 'Selected fallback']) {
      data.status.modulators[0].rds_rt_effective = selected;
      state.channels.set(compose(data));
      f.detectChanges();
      expect(cards[0].querySelector('.rt dd').textContent).toBe(selected);
    }
    expect(cards[0].textContent).not.toContain('Saved fallback must not override selection');
  });
  it.each([
    [false, true],
    [true, false],
    [false, false],
  ])(
    'hides the whole RT row with RDS=%s RT=%s and preserves configuration',
    (rdsEnabled, rtEnabled) => {
      const state = setup(),
        data = sample();
      const config = data.modulators[0];
      config.rds_enabled = rdsEnabled;
      config.rds_rt_enabled = rtEnabled;
      config.rds_rt = 'Saved fallback';
      data.status.modulators[0].rds_rt_effective = 'Selected metadata';
      state.channels.set(compose(data));
      const f = TestBed.createComponent(Dashboard);
      f.detectChanges();
      const card = f.nativeElement.querySelector('app-channel-card');
      expect(card.querySelector('.rt')).toBeNull();
      expect(
        Array.from(card.querySelectorAll('dt') as NodeListOf<HTMLElement>).map(
          (e) => e.textContent,
        ),
      ).not.toContain('RT');
      expect(card.textContent).not.toContain('Selected metadata');
      expect(config.rds_rt).toBe('Saved fallback');
      config.rds_enabled = true;
      config.rds_rt_enabled = true;
      state.channels.set(compose(data));
      f.detectChanges();
      expect(card.querySelector('.rt dd').textContent).toBe('Selected metadata');
    },
  );
  it('separates pending and command-confirmed state from hardware observations', () => {
    const state = setup();
    const f = TestBed.createComponent(Dashboard);
    state.busy.set(1);
    state.pendingAction.set('start');
    f.detectChanges();
    const card = f.nativeElement.querySelectorAll('app-channel-card')[1];
    expect(card.querySelector('button').disabled).toBe(true);
    expect(card.querySelector('.tx-state').textContent.trim()).toBe('Starting...');
    state.pendingAction.set('stop');
    f.detectChanges();
    expect(card.querySelector('.tx-state').textContent.trim()).toBe('Stopping...');
    state.busy.set(null);
    state.pendingAction.set(null);
    state.commandStates.set({ 1: 'ON' });
    f.detectChanges();
    expect(card.querySelector('.tx-state').textContent.trim()).toBe('ON (command)');
    expect(card.querySelector('.channel-control span').textContent).toContain(
      'Reported state: OFF',
    );
    expect(card.querySelector('button').textContent.trim()).toBe('STOP');
    state.stale.set(true);
    f.detectChanges();
    expect(card.querySelector('.channel-control span').textContent).toContain('UNKNOWN');
  });
});
