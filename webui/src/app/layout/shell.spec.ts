import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { Shell } from './shell';
import { DashboardState } from '../features/dashboard/dashboard-state';
import { SystemApi } from '../core/api/system-api';
import { ProfileApi } from '../features/audio-profiles/profile-api';

describe('Shell header sizing', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('tracks header resizing independently of menu state and disconnects on destroy', () => {
    let resize!: ResizeObserverCallback;
    const observe = vi.fn();
    const disconnect = vi.fn();
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: ResizeObserverCallback) {
          resize = callback;
        }
        observe = observe;
        disconnect = disconnect;
      },
    );
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: SystemApi, useValue: { info: () => of({ fmheadend_version: '1.0.0' }) } },
        { provide: ProfileApi, useValue: { list: () => of([]) } },
      ],
    });
    const state = TestBed.inject(DashboardState);
    vi.spyOn(state, 'start').mockImplementation(() => {});
    const stop = vi.spyOn(state, 'stop');
    const fixture = TestBed.createComponent(Shell);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    const header = host.querySelector('header')!;
    expect(observe).toHaveBeenCalledWith(header);
    const height = vi.spyOn(header, 'getBoundingClientRect');
    height.mockReturnValue({ height: 142.5 } as DOMRect);
    resize([], {} as ResizeObserver);
    expect(host.style.getPropertyValue('--shell-header-height')).toBe('142.5px');
    fixture.componentInstance.menu.set(true);
    height.mockReturnValue({ height: 206 } as DOMRect);
    resize([], {} as ResizeObserver);
    fixture.detectChanges();
    expect(host.style.getPropertyValue('--shell-header-height')).toBe('206px');
    expect(host.querySelector('aside')!.classList.contains('expanded')).toBe(true);
    fixture.destroy();
    expect(disconnect).toHaveBeenCalledOnce();
    expect(stop).toHaveBeenCalledOnce();
  });
});
