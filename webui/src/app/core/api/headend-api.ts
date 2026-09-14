import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { forkJoin, timeout } from 'rxjs';
import { HeadendStatus, InputSource, LifecycleResult, Modulator, ModulatorStatus } from './models';

@Injectable({ providedIn: 'root' })
export class HeadendApi {
  private http = inject(HttpClient);
  snapshot() {
    return forkJoin({
      status: this.http.get<HeadendStatus>('/api/v1/status'),
      modulators: this.http.get<Modulator[]>('/api/v1/modulators'),
      sources: this.http.get<InputSource[]>('/api/v1/sources'),
    }).pipe(timeout(15000));
  }
  channel(id: number, action: 'start' | 'stop') {
    return this.http
      .post<ModulatorStatus>(`/api/v1/modulators/${id}/${action}`, {})
      .pipe(timeout(120000));
  }
  startAll() {
    return this.http
      .post<LifecycleResult[]>('/api/v1/modulators/start-all', {})
      .pipe(timeout(120000));
  }
  stopAll() {
    return this.http
      .post<LifecycleResult[]>('/api/v1/modulators/stop-all', {})
      .pipe(timeout(120000));
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof HttpErrorResponse && typeof error.error?.message === 'string') {
    return error.error.message.slice(0, 300);
  }
  return 'Backend unavailable or request timed out. Outcome may be unknown; check refreshed status.';
}
