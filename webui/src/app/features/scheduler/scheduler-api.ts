import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { timeout } from 'rxjs';

export interface ScheduleDraft {
  enabled: boolean;
  days_of_week: number[];
  time: string;
  tx_id: number;
  action: 'start' | 'stop' | 'switch_source';
  source_id: number | null;
}

export interface ScheduleRule extends ScheduleDraft {
  id: number;
}

const endpoint = '/api/v1/schedules';

@Injectable({ providedIn: 'root' })
export class SchedulerApi {
  private http = inject(HttpClient);

  list() {
    return this.http.get<ScheduleRule[]>(endpoint).pipe(timeout(15000));
  }

  create(draft: ScheduleDraft) {
    return this.http.post<ScheduleRule>(endpoint, draft).pipe(timeout(15000));
  }

  update(id: number, changes: Partial<ScheduleDraft>) {
    return this.http.patch<ScheduleRule>(`${endpoint}/${id}`, changes).pipe(timeout(15000));
  }

  delete(id: number) {
    return this.http.delete<void>(`${endpoint}/${id}`).pipe(timeout(15000));
  }
}
