import { Component, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
@Component({
  template: `<p class="eyebrow">M7B</p>
    <h1>{{ title }}</h1>
    <p class="muted">Configuration and page contents will be available in M7B.</p>`,
})
export class Placeholder {
  readonly title = inject(ActivatedRoute).snapshot.data['title'] as string;
}
