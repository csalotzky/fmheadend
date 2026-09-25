import { TestBed } from '@angular/core/testing';
import { DspNumber } from './dsp-number';
import { DspSchema } from './profile-api';

describe('DSP slider with precise numeric entry', () => {
  async function control(schema: DspSchema, value: number) {
    const f = TestBed.createComponent(DspNumber);
    f.componentRef.setInput('schema', schema);
    f.componentRef.setInput('label', 'Gain (dB)');
    f.componentRef.setInput('value', value);
    const received = vi.fn((next: number) => f.componentRef.setInput('value', next));
    f.componentInstance.changed.subscribe(received);
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    return { f, received };
  }
  const gain: DspSchema = {
    type: 'number',
    maximum: 20,
    slider: { min: -60, max: 20, scale: 'linear', step: 0.1 },
  };
  it('does not round on load; both controls update the same dB value', async () => {
    const { f, received } = await control(gain, -3.123456789);
    const numeric = f.nativeElement.querySelector('input[type=number]');
    const slider = f.nativeElement.querySelector('input[type=range]');
    expect(numeric.value).toBe('-3.123456789');
    expect(received).not.toHaveBeenCalled();
    slider.value = '500';
    slider.dispatchEvent(new Event('input'));
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(numeric.value).toBe('-20');
    numeric.value = '-6.54321';
    numeric.dispatchEvent(new Event('input'));
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    expect(f.componentInstance.value()).toBe(-6.54321);
    expect(Number(slider.value)).toBeCloseTo(668.209875);
    expect(numeric.step).toBe('any');
    expect(f.nativeElement.querySelector('label').htmlFor).toBe(numeric.id);
  });
  it('keeps a valid gain below the finite slider window, without clamping it', async () => {
    const { f, received } = await control(gain, -87.654321);
    expect(f.componentInstance.bounds()[0]).toBe(-87.654321);
    expect(f.componentInstance.value()).toBe(-87.654321);
    expect(received).not.toHaveBeenCalled();
  });
  it.each([
    [10, 10000, 0, 316],
    [0, 2000, 0.1, 14],
    [1, 100, 0, 10],
  ])('uses a logarithmic scale in real units (%s..%s)', async (min, max, offset, midpoint) => {
    const { f } = await control(
      {
        type: 'number',
        minimum: min,
        maximum: max,
        slider: { min, max, scale: 'log', offset, significant_digits: 3 },
      },
      min,
    );
    f.componentInstance.slide(500);
    expect(f.componentInstance.value()).toBeCloseTo(midpoint, 0);
    f.componentInstance.slide(0);
    expect(f.componentInstance.value()).toBe(min);
  });
  it('rejects invalid numbers without clamping and honors disabled controls', async () => {
    const { f } = await control(
      { type: 'number', maximum: 0, slider: { ...gain.slider!, max: 0 } },
      1,
    );
    expect(f.componentInstance.invalid()).toBe(true);
    expect(f.componentInstance.value()).toBe(1);
    expect(f.nativeElement.textContent).toContain('supported limits');
    f.componentRef.setInput('disabled', true);
    f.detectChanges();
    await f.whenStable();
    expect(
      [...f.nativeElement.querySelectorAll('input')].every((i: HTMLInputElement) => i.disabled),
    ).toBe(true);
  });
});
