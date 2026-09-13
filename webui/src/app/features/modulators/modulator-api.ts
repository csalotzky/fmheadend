import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { timeout } from 'rxjs';
import { Modulator } from '../../core/api/models';
export type ModulatorDraft = Omit<Modulator, 'id' | 'timestamp' | 'enabled'>;
@Injectable({ providedIn: 'root' })
export class ModulatorApi {
  private http = inject(HttpClient);
  get(id: number) {
    return this.http.get<Modulator>(`/api/v1/modulators/${id}`).pipe(timeout(15000));
  }
  save(id: number, body: ModulatorDraft) {
    return this.http.patch<Modulator>(`/api/v1/modulators/${id}`, body).pipe(timeout(15000));
  }
}
