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
  readonly profileName = input<string | null | undefined>(null);
  readonly locked = input(false);
  readonly pending = input(false);
  readonly pendingAction = input<'start' | 'stop' | null>(null);
  readonly commandState = input<'ON' | 'OFF' | undefined>();
  readonly feedback = input('');
  readonly displayState = computed(() => {
    if (this.pending()) return this.pendingAction() === 'stop' ? 'Stopping...' : 'Starting...';
    if (this.reportedState() === 'ERROR') return 'ERROR';
    return this.commandState() ? `${this.commandState()} (requested)` : this.reportedState();
  });
  readonly operate = output<'start' | 'stop'>();
  readonly attenuation = computed(() =>
    this.stale()
      ? null
      : this.channel().confirmedOff
        ? this.channel().config?.rf_attenuation_db
        : this.channel().runtime?.rf_attenuation_db,
  );
  readonly stopping = computed(
    () =>
      ['ON', 'STARTING'].includes(this.commandState() ?? this.channel().runtime?.tx_state ?? '') ||
      (this.channel().runtime?.tx_state === 'ERROR' && !!this.channel().config?.enabled),
  );
  readonly reportedState = computed(() => {
    if (this.stale() || this.channel().observationStale) return 'UNKNOWN';
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
  humanizeStatus(value: string) {
    // Preserve prose labels; normalize enum tokens, including future multiword states.
    return value.replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*\b/g, (token) => {
      const words = token.toLowerCase().replace(/_/g, ' ');
      return words.charAt(0).toUpperCase() + words.slice(1);
    });
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
