import { Component, ElementRef, OnDestroy, ViewChild, inject, output, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { Subscription } from 'rxjs';
import { errorMessage } from '../../core/api/headend-api';
import { RADIO_BATCH_SIZE, RadioBrowserApi, RadioOption, RadioStation } from './radio-browser-api';

@Component({
  selector: 'app-radio-browser-picker',
  imports: [ReactiveFormsModule],
  templateUrl: './radio-browser-picker.html',
  styleUrl: './radio-browser-picker.css',
})
export class RadioBrowserPicker implements OnDestroy {
  @ViewChild('dialog', { static: true }) dialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('search', { static: true }) searchInput!: ElementRef<HTMLInputElement>;
  private api = inject(RadioBrowserApi);
  readonly chosen = output<RadioStation>();
  readonly opened = signal(false);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly countriesError = signal('');
  readonly tagsError = signal('');
  readonly countries = signal<RadioOption[]>([]);
  readonly tags = signal<RadioOption[]>([]);
  readonly stations = signal<RadioStation[]>([]);
  readonly selected = signal<RadioStation | null>(null);
  readonly hasMore = signal(false);
  readonly filters = new FormGroup({
    name: new FormControl('', { nonNullable: true }),
    countrycode: new FormControl('', { nonNullable: true }),
    tag: new FormControl('', { nonNullable: true }),
    hidebroken: new FormControl(true, { nonNullable: true }),
  });
  private names =
    typeof Intl.DisplayNames === 'function'
      ? new Intl.DisplayNames(['en'], { type: 'region' })
      : null;
  private requests = new Subscription();
  private searchRequest?: Subscription;
  private tagRequest?: Subscription;
  private debounce?: ReturnType<typeof setTimeout>;
  private generation = 0;
  private previousFocus: HTMLElement | null = null;
  private previousOverflow = '';
  private changes = this.filters.valueChanges.subscribe(() => this.filtersChanged());

  countryName(code: string) {
    try {
      return this.names?.of(code.toUpperCase()) || code;
    } catch {
      return code;
    }
  }
  open() {
    if (this.opened()) return;
    this.previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    this.opened.set(true);
    this.selected.set(null);
    this.requests = new Subscription();
    const dialog = this.dialog.nativeElement;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute('open', '');
    this.searchInput.nativeElement.focus();
    this.loadCountries();
    this.filtersChanged(0);
  }
  close() {
    if (!this.opened()) return;
    this.opened.set(false);
    ++this.generation;
    clearTimeout(this.debounce);
    this.searchRequest?.unsubscribe();
    this.tagRequest?.unsubscribe();
    this.requests.unsubscribe();
    const dialog = this.dialog.nativeElement;
    if (dialog.close) dialog.close();
    else dialog.removeAttribute('open');
    document.body.style.overflow = this.previousOverflow;
    this.previousFocus?.focus();
  }
  cancel(event: Event) {
    event.preventDefault();
    this.close();
  }
  ngOnDestroy() {
    this.close();
    this.changes.unsubscribe();
  }
  loadCountries() {
    this.countriesError.set('');
    this.requests.add(
      this.api.countries().subscribe({
        next: (rows) =>
          this.countries.set(
            [...rows].sort((a, b) =>
              this.countryName(a.name).localeCompare(this.countryName(b.name)),
            ),
          ),
        error: (e) => this.countriesError.set(errorMessage(e)),
      }),
    );
  }
  filtersChanged(delay = 300) {
    if (!this.opened()) return;
    ++this.generation;
    clearTimeout(this.debounce);
    this.searchRequest?.unsubscribe();
    this.tagRequest?.unsubscribe();
    this.selected.set(null);
    this.stations.set([]);
    this.tags.set([]);
    this.hasMore.set(false);
    this.loading.set(true);
    this.error.set('');
    this.tagsError.set('');
    this.debounce = setTimeout(() => {
      this.searchBatch();
      this.loadTags();
    }, delay);
  }
  loadTags() {
    this.tagRequest?.unsubscribe();
    this.tagsError.set('');
    const name = this.filters.controls.tag.value.trim();
    if (!name) {
      this.tags.set([]);
      return;
    }
    const generation = this.generation;
    this.tagRequest = this.api.tags(name).subscribe({
      next: (rows) => {
        if (generation === this.generation) this.tags.set(rows);
      },
      error: (e) => {
        if (generation === this.generation) this.tagsError.set(errorMessage(e));
      },
    });
  }
  private searchBatch() {
    this.searchRequest?.unsubscribe();
    this.loading.set(true);
    this.error.set('');
    const generation = this.generation;
    const filters = this.filters.getRawValue();
    this.searchRequest = this.api
      .search(
        { ...filters, name: filters.name.trim(), tag: filters.tag.trim() },
        this.stations().length,
      )
      .subscribe({
        next: (batch) => {
          if (generation !== this.generation) return;
          this.stations.update((stations) => [...stations, ...batch.stations]);
          this.hasMore.set(batch.stations.length === RADIO_BATCH_SIZE);
          this.loading.set(false);
        },
        error: (e) => {
          if (generation !== this.generation) return;
          this.loading.set(false);
          this.error.set(errorMessage(e));
        },
      });
  }
  loadMore() {
    if (this.opened() && !this.loading() && this.hasMore()) this.searchBatch();
  }
  retry() {
    if (this.opened() && !this.loading() && this.error()) this.searchBatch();
  }
  useStation() {
    const station = this.selected();
    if (this.loading() || !station?.playable_url) return;
    this.close();
    this.chosen.emit(station);
  }
}
