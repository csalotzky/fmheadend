import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { ChannelCard } from './channel-card';
import { compose } from './dashboard-state';
import { sample } from './test-fixtures';

describe('Requested and reported RF lifecycle', () => {
  it.each(['ON','OFF'] as const)('keeps %s yellow until the reported state agrees', (target) => {
    TestBed.configureTestingModule({providers:[provideRouter([])]});
    const data=sample();
    data.modulators[0].enabled=target==='ON';
    Object.assign(data.status.modulators[0], {desired_on:target==='ON', tx_state:target==='ON'?'STARTING':'OFF', hardware_tx_state:target==='ON'?'OFF':'ON', start_pending:target==='ON'});
    const fixture=TestBed.createComponent(ChannelCard);
    fixture.componentRef.setInput('channel',compose(data)[0]);fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.channel').classList.contains('blocked')).toBe(true);
    expect(fixture.nativeElement.querySelector('.tx-state').textContent).toContain(`${target} (requested)`);
    Object.assign(data.status.modulators[0], {tx_state:target, hardware_tx_state:target, start_pending:false});
    fixture.componentRef.setInput('channel',compose(data)[0]);fixture.detectChanges();
    const classes=fixture.nativeElement.querySelector('.channel').classList;
    expect(classes.contains('blocked')).toBe(false);
    expect(classes.contains('on')).toBe(target==='ON');
    expect(fixture.nativeElement.querySelector('.tx-state').textContent.trim()).toBe(target);
    fixture.destroy();
  });

  it('does not show ON requested after a failed save leaves the enabled TX inhibited', () => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const data = sample();
    expect(data.modulators[0].enabled).toBe(true);
    Object.assign(data.status.modulators[0], {
      desired_on: false, intent_action: 'SAVE', tx_state: 'OFF', hardware_tx_state: 'OFF',
    });
    const fixture = TestBed.createComponent(ChannelCard);
    fixture.componentRef.setInput('channel', compose(data)[0]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.tx-state').textContent.trim()).toBe('OFF');
    expect(fixture.nativeElement.textContent).toContain('Reported state: OFF');
    expect(fixture.nativeElement.querySelector('button').textContent.trim()).toBe('START');
  });
});
