import { Component, computed, input, output } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ChannelView } from './dashboard-state';
@Component({
  selector: 'app-channel-card',
  imports: [DecimalPipe, RouterLink],
  templateUrl: './channel-card.html',
  styleUrl: './channel-card.css',
})
export class ChannelCard {
  readonly channel = input.required<ChannelView>();
  readonly stale = input(false);
  readonly locked = input(false);
  readonly pending = input(false);
  readonly actionError = input<string | null>(null);
  readonly operate = output<'start' | 'stop'>();
  readonly stopping = computed(
    () =>
      this.channel().config?.enabled ||
      ['ON', 'STARTING'].includes(this.channel().runtime?.tx_state ?? ''),
  );
  readonly reportedState = computed(() => {
    if (this.stale()) return 'UNKNOWN';
    const state = this.channel().runtime?.hardware_tx_state;
    return state === 'ON' || state === 'OFF' || state === 'ERROR' ? state : 'UNKNOWN';
  });
  readonly reportedError = computed(
    () =>
      this.channel().error ||
      (this.reportedState() === 'ERROR'
        ? this.channel().runtime?.hardware_last_error || 'Hardware/control error'
        : null),
  );
  sourceState() {
    const channel = this.channel();
    if (!channel.config) return 'Unknown';
    if (channel.config.input_source_id === null) return 'Not assigned';
    if (!channel.source) return 'Unavailable';
    return this.stale() ? 'Unknown' : (channel.runtime?.source_state ?? 'Unknown');
  }
  hex(value: number | null | undefined, length: number) {
    return value == null ? '\u2014' : value.toString(16).toUpperCase().padStart(length, '0');
  }
  ps() {
    return (
      this.channel()
        .config?.rds_ps.map((p) => p.text)
        .join(' / ') ?? '\u2014'
    );
  }
  af() {
    return (
      this.channel()
        .config?.rds_af_khz.map((f) => (f / 1000).toFixed(1))
        .join(', ') || '\u2014'
    );
  }
}
