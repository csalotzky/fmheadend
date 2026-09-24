import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
import { Modulator } from '../../core/api/models';
export type ModulatorDraft = Omit<Modulator, 'id' | 'timestamp' | 'enabled'>;
export interface ModulatorSaveResult {
  saved: boolean;
  stop_confirmed: boolean;
  restarted: boolean;
  failed_stage: 'stop' | 'save' | 'start' | null;
  message: string;
  configuration: Modulator | null;
}
@Injectable({ providedIn: 'root' })
export class ModulatorApi {
  private http = inject(HttpClient);
  get(id: number) {
    return this.http.get<Modulator>(`/api/v1/modulators/${id}`).pipe(timeout(15000));
  }
  save(id: number, body: ModulatorDraft) {
    return this.http.patch<Modulator>(`/api/v1/modulators/${id}`, body).pipe(timeout(15000));
  }
  saveAndResume(id: number, body: ModulatorDraft) {
    return this.http
      .post<ModulatorSaveResult>(`/api/v1/modulators/${id}/save-and-resume`, body)
      .pipe(timeout(60000));
  }
}
