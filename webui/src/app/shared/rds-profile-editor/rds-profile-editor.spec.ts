import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { SourceEditor } from '../../features/sources/source-editor';
import { SourceApi } from '../../features/sources/source-api';
import { ModulatorEditor } from '../../features/modulators/modulator-editor';
import { ModulatorApi } from '../../features/modulators/modulator-api';
import { DashboardState, compose } from '../../features/dashboard/dashboard-state';
import { ChannelCard } from '../../features/dashboard/channel-card';
import { sample } from '../../features/dashboard/test-fixtures';
import { SystemApi } from '../../core/api/system-api';
import { systemInfo } from '../../features/system/test-fixtures';
import { InputSource, RdsProfile } from '../../core/api/models';

const profile: RdsProfile = {
  rds_pi: 0x1234,
  rds_ecc: 224,
  rds_pty: 10,
  rds_tp: false,
  rds_ps: [{ text: 'STATION', duration_s: 5 }],
  rds_af_khz: [98100],
  rds_rt: 'Fallback',
  rds_rt_enabled: true,
  rds_rt_termination: 'PAD',
  rds_metadata_to_rt: true,
};

describe('Owned RDS editors', () => {
  afterEach(() => vi.restoreAllMocks());
  function setup(sourceProfile: RdsProfile | null = null, edit = false) {
    const data = sample();
    const source: InputSource = {
      ...data.sources[0],
      rds_profile: sourceProfile,
      rds_profile_id: sourceProfile ? 9 : null,
    };
    data.sources = [source];
    const route = {
      snapshot: { paramMap: convertToParamMap(edit ? { id: '1' } : {}) },
      paramMap: of(convertToParamMap({ id: '0' })),
    };
    const api = { profiles: () => of([]), get: () => of(source), save: vi.fn(() => of(source)) };
    const shared = {
      data: signal(data),
      stale: signal(false),
      channels: signal(compose(data)),
      refreshAfterMutation: vi.fn().mockResolvedValue(undefined),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: route },
        { provide: SourceApi, useValue: api },
        { provide: DashboardState, useValue: shared },
        { provide: SystemApi, useValue: { info: () => of(systemInfo) } },
        {
          provide: ModulatorApi,
          useValue: {
            get: () =>
              of({
                ...data.modulators[0],
                rds_override_profile: profile,
                rds_follow_source: false,
              }),
          },
        },
      ],
    });
    return { data, api, shared };
  }
  it('starts without Source RDS and uses the shared content editor in a separate RDS section', async () => {
    setup();
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    await f.whenStable();
    const c = f.componentInstance;
    expect(f.nativeElement.querySelector('app-rds-profile-editor')).toBeNull();
    c.configureRds.setValue(true);
    c.toggleRds();
    c.rds.load(profile);
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(f.nativeElement.querySelector('app-rds-profile-editor')).not.toBeNull();
    expect(f.nativeElement.textContent).toContain('Fill from source');
    expect(f.nativeElement.querySelector('.editor-actions app-rds-profile-editor')).toBeNull();
    expect(
      f.nativeElement.querySelector('[aria-labelledby=source-rds-heading] app-rds-profile-editor'),
    ).not.toBeNull();
    c.form.controls.name.setValue('Station');
    c.config.controls['uri'].setValue('https://example.org/live');
    expect(c.draft()?.rds_profile).toEqual(profile);
    expect(c.draft()).not.toHaveProperty('rds_enabled');
  });
  it('fills only the unsaved Source PS/RT draft, preserving identity and policy', async () => {
    const { api } = setup(profile, true);
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    await f.whenStable();
    const c = f.componentInstance;
    expect(c.rds.draft()).toEqual(profile);
    c.form.controls.name.setValue('Caf\u00e9 R\u00e1di\u00f3 Budapest');
    c.fillFromSource();
    expect(c.rds.draft()).toEqual({
      ...profile,
      rds_ps: [{ text: 'Caf\u00e9 R\u00e1d', duration_s: 5 }],
      rds_rt: 'Caf\u00e9 R\u00e1di\u00f3 Budapest',
    });
    expect(c.rds.form.dirty).toBe(true);
    expect(api.save).not.toHaveBeenCalled();
    const filled = c.rds.draft();
    c.form.controls.name.setValue('Later name');
    expect(c.rds.draft()).toEqual(filled);
    c.rds.addPs();
    const carousel = c.rds.form.getRawValue();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    c.fillFromSource();
    expect(c.rds.form.getRawValue()).toEqual(carousel);
    confirm.mockReturnValue(true);
    c.fillFromSource();
    expect(c.rds.ps.length).toBe(1);
    expect(c.rds.draft().rds_metadata_to_rt).toBe(true);
    expect(api.save).not.toHaveBeenCalled();
  });
  it('keeps identification, PS, AF and RT in distinct shared groups and actions below RDS', async () => {
    setup(profile, true);
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    const root: HTMLElement = f.nativeElement;
    expect(root.querySelectorAll('app-rds-profile-editor .profile-group').length).toBe(3);
    expect(root.querySelector('.ps-group .list-entry input')).not.toBeNull();
    expect(root.querySelector('.rt-group textarea')).not.toBeNull();
    const panel = root.querySelector('[aria-labelledby=source-rds-heading]')!;
    const actions = root.querySelector('.editor-actions')!;
    expect(panel.compareDocumentPosition(actions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(panel.textContent).not.toContain('Testing does not save');
    expect(actions.querySelector('button[type=submit]')?.textContent).toContain('Save');
  });
  it.each([false, true])(
    'disables RDS as a draft without confirmation (persisted=%s)',
    async (persisted) => {
      const { api } = setup(persisted ? profile : null, persisted);
      const f = TestBed.createComponent(SourceEditor);
      f.detectChanges();
      await f.whenStable();
      f.detectChanges();
      const c = f.componentInstance;
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
      const checkbox: HTMLInputElement = f.nativeElement.querySelector('.source-rds-toggle input');
      const toggle = async (enabled: boolean) => {
        checkbox.checked = enabled;
        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
        await f.whenStable();
      };
      await toggle(true);
      c.rds.load(profile);
      c.rds.form.controls.rds_rt.setValue('Unsaved draft');
      c.rds.form.markAsDirty();
      c.rds.form.markAsTouched();
      await toggle(false);
      expect(c.configureRds.value).toBe(false);
      expect(f.nativeElement.querySelector('app-rds-profile-editor')).toBeNull();
      expect(confirm).not.toHaveBeenCalled();
      expect(api.save).not.toHaveBeenCalled();
      await toggle(true);
      expect(f.nativeElement.querySelector('app-rds-profile-editor')).not.toBeNull();
      expect(c.rds.draft()).toEqual({ ...profile, rds_rt: 'Unsaved draft' });
      c.configureRds.setValue(false);
      c.toggleRds();
      f.destroy();
      expect(api.save).not.toHaveBeenCalled();
    },
  );
  it('removes only on Save and retains the draft on follower rejection', async () => {
    const { api } = setup(profile, true);
    const f = TestBed.createComponent(SourceEditor);
    f.detectChanges();
    await f.whenStable();
    const c = f.componentInstance;
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    c.configureRds.setValue(false);
    c.toggleRds();
    expect(confirm).not.toHaveBeenCalled();
    expect(api.save).not.toHaveBeenCalled();
    api.save.mockImplementation(() =>
      throwError(
        () =>
          new HttpErrorResponse({
            status: 409,
            error: { message: 'Followed by TX0, TX3. Switch to Custom first.' },
          }),
      ),
    );
    await c.save();
    expect(c.configureRds.value).toBe(true);
    expect(c.rds.draft()).toEqual(profile);
    expect(c.error()).toContain('TX0, TX3');
  });
  it('selects Follow for a source profile and preserves the Custom draft across switching', async () => {
    const { shared } = setup(profile);
    const f = TestBed.createComponent(ModulatorEditor);
    f.detectChanges();
    await f.whenStable();
    const c = f.componentInstance;
    const before = c.rds.draft();
    expect(before).toEqual(profile);
    expect(c.draft()).not.toHaveProperty('rds_pi');
    expect(f.nativeElement.textContent).not.toContain('Fill from source');
    c.changeSource();
    expect(c.form.controls.rds_follow_source.value).toBe(true);
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(f.nativeElement.querySelector('app-rds-profile-editor')).toBeNull();
    expect(f.nativeElement.textContent).toContain('Edit it in Sources');
    c.form.controls.rds_follow_source.setValue(false);
    f.changeDetectorRef.markForCheck();
    f.detectChanges();
    expect(f.nativeElement.querySelector('app-rds-profile-editor')).not.toBeNull();
    expect(c.rds.draft()).toEqual(before);
    shared.data.update((d) => ({
      ...d,
      sources: d.sources.map((s) => ({ ...s, rds_profile: null })),
    }));
    c.changeSource();
    expect(c.form.controls.rds_follow_source.value).toBe(false);
    c.form.controls.audio_mode.setValue('ANALOG');
    c.changeMode();
    expect(c.form.controls.rds_follow_source.value).toBe(false);
    expect(c.rds.draft()).toEqual(before);
  });
  it('renders effective content with the existing Dashboard card regardless of ownership', () => {
    setup();
    const data = sample();
    data.modulators[0] = {
      ...data.modulators[0],
      ...profile,
      rds_follow_source: true,
      rds_override_profile: { ...profile, rds_pi: 0xfff0 },
    };
    const f = TestBed.createComponent(ChannelCard);
    f.componentRef.setInput('channel', compose(data)[0]);
    f.detectChanges();
    expect(f.nativeElement.textContent).toContain('1234');
    expect(f.nativeElement.textContent).toContain('STATION');
    expect(f.nativeElement.textContent).not.toContain('FFF0');
    const inherited = f.nativeElement.innerHTML;
    data.modulators[0].rds_follow_source = false;
    f.componentRef.setInput('channel', compose(data)[0]);
    f.detectChanges();
    expect(f.nativeElement.innerHTML).toBe(inherited);
  });
});
