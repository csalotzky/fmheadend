import { Component, input } from '@angular/core';
import { SourceTest } from './source-api';
@Component({
  selector: 'app-source-test-result',
  template: ` @if (result(); as r) {
    <div role="status" [class.error]="!r.ok">
      <strong
        >{{ r.ok ? 'Test successful' : 'Test failed' }}{{ r.mocked ? ' (mock probe)' : '' }}</strong
      >
      @if (r.ok && r.station_name) {
        <p>
          <strong>Station: {{ r.station_name }}</strong>
        </p>
      }
      <p>{{ r.message }}</p>
      @if (r.codec) {
        <span>{{ r.codec }} </span>
      }
      @if (r.sample_rate) {
        <span>{{ r.sample_rate }} Hz </span>
      }
      @if (r.channels) {
        <span>{{ r.channels }} channels </span>
      }
      @if (r.ok && bitrate(r.input_bitrate_bps); as rate) {
        <p>Bitrate: ~{{ rate }} kb/s</p>
      }
      @if (r.output_caps) {
        <p>{{ r.output_caps }}</p>
      }
      @if (r.stream_title) {
        <p>{{ r.stream_title }}</p>
      }
    </div>
  }`,
  styles: `
    :host {
      display: block;
      overflow-wrap: anywhere;
    }
    p {
      margin: 6px 0;
    }
  `,
})
export class TestResult {
  readonly result = input<SourceTest | null>(null);
  bitrate(value: number | null | undefined): number | null {
    return value != null && Number.isFinite(value) && value > 0
      ? Math.max(1, Math.round(value / 1000))
      : null;
  }
}
