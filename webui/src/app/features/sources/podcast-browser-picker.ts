import {
  Component,
  ElementRef,
  OnDestroy,
  ViewChild,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { Subscription, distinctUntilChanged, map } from 'rxjs';
import { errorMessage } from '../../core/api/headend-api';
import { PodcastBrowserApi, PodcastSearchResult } from './podcast-browser-api';
import { PODCAST_MAX_FEEDS } from './podcast-form';

const BATCH_SIZE = 20;
const SEARCH_DELAY = 800;

@Component({
  selector: 'app-podcast-browser-picker',
  imports: [ReactiveFormsModule],
  templateUrl: './podcast-browser-picker.html',
  styleUrl: './podcast-browser-picker.css',
})
export class PodcastBrowserPicker implements OnDestroy {
  @ViewChild('dialog', { static: true }) dialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('search', { static: true }) searchInput!: ElementRef<HTMLInputElement>;
  @ViewChild('results', { static: true }) resultsPane!: ElementRef<HTMLElement>;
  private api = inject(PodcastBrowserApi);
  readonly feedUrls = input<readonly string[]>([]);
  readonly addRequested = output<PodcastSearchResult>();
  readonly addedUrls = computed(() => new Set(this.feedUrls()));
  readonly atCapacity = computed(
    () => this.feedUrls().length >= PODCAST_MAX_FEEDS && !this.addedUrls().has(''),
  );
  readonly maxFeeds = PODCAST_MAX_FEEDS;
  readonly name = new FormControl('', { nonNullable: true });
  readonly query = signal('');
  readonly loading = signal(false);
  readonly error = signal('');
  readonly addedTitle = signal('');
  readonly failedArtwork = signal<ReadonlySet<string>>(new Set());
  private podcasts = signal<PodcastSearchResult[]>([]);
  private visibleCount = signal(BATCH_SIZE);
  readonly visiblePodcasts = computed(() => this.podcasts().slice(0, this.visibleCount()));
  readonly hasMore = computed(() => this.podcasts().length > this.visibleCount());
  readonly opened = signal(false);
  private searchRequest?: Subscription;
  private debounce?: ReturnType<typeof setTimeout>;
  private generation = 0;
  private previousFocus: HTMLElement | null = null;
  private previousOverflow = '';
  private changes = this.name.valueChanges
    .pipe(
      map((name) => name.trim()),
      distinctUntilChanged(),
    )
    .subscribe((name) => this.queryChanged(name));

  open() {
    if (this.opened()) return;
    this.previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    this.opened.set(true);
    const dialog = this.dialog.nativeElement;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute('open', '');
    this.searchInput.nativeElement.focus();
  }

  close() {
    if (!this.opened()) return;
    this.opened.set(false);
    this.resetSearch();
    this.name.setValue('');
    this.addedTitle.set('');
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

  private resetSearch() {
    ++this.generation;
    clearTimeout(this.debounce);
    this.searchRequest?.unsubscribe();
    this.query.set('');
    this.loading.set(false);
    this.error.set('');
    this.podcasts.set([]);
    this.visibleCount.set(BATCH_SIZE);
    this.failedArtwork.set(new Set());
    this.resultsPane.nativeElement.scrollTop = 0;
  }

  private queryChanged(name: string) {
    if (!this.opened()) return;
    this.resetSearch();
    this.addedTitle.set('');
    this.query.set(name);
    if (!name) return;
    this.loading.set(true);
    this.debounce = setTimeout(() => this.search(name), SEARCH_DELAY);
  }

  private search(name: string) {
    this.error.set('');
    this.loading.set(true);
    const generation = this.generation;
    this.searchRequest = this.api.search(name).subscribe({
      next: ({ podcasts }) => {
        if (generation !== this.generation) return;
        this.podcasts.set(podcasts);
        this.loading.set(false);
      },
      error: (e) => {
        if (generation !== this.generation) return;
        this.error.set(errorMessage(e));
        this.loading.set(false);
      },
    });
  }

  retry() {
    if (this.opened() && this.query() && !this.loading() && this.error()) {
      this.search(this.query());
    }
  }

  loadMore() {
    if (this.hasMore()) this.visibleCount.update((count) => count + BATCH_SIZE);
  }

  artworkFailed(url: string) {
    this.failedArtwork.update((failed) => new Set(failed).add(url));
  }

  confirmAdded(title: string) {
    this.addedTitle.set(title);
  }
}
