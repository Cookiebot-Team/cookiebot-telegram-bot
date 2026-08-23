'use client';
import {
  SearchDialog,
  SearchDialogClose,
  SearchDialogContent,
  SearchDialogHeader,
  SearchDialogIcon,
  SearchDialogInput,
  SearchDialogList,
  SearchDialogOverlay,
  type SharedProps,
} from 'fumadocs-ui/components/dialog/search';
import { useDocsSearch } from 'fumadocs-core/search/client';
import { staticClient } from 'fumadocs-core/search/client/orama-static';
import { useI18n } from 'fumadocs-ui/contexts/i18n';
import { asset } from '@/lib/shared';

export default function DefaultSearchDialog(props: SharedProps) {
  const { locale } = useI18n(); // (optional) for i18n
  const { search, setSearch, query } = useDocsSearch({
    client: staticClient({
      locale,
      // The exported index is a static file like any other, so it needs the
      // same base-path prefix `asset()` gives brand images. Fumadocs' default
      // (`/api/search`) resolves the base path from `import.meta.env.BASE_URL`,
      // a Vite variable that Next never sets — so on GitHub Pages the client
      // fetched `cookiebot-team.github.io/api/search` instead of
      // `.../cookiebot-telegram-bot/api/search`, got the 404 page, and every
      // query came back empty with no error in the UI.
      from: asset('/api/search'),
    }),
  });

  return (
    <SearchDialog search={search} onSearchChange={setSearch} isLoading={query.isLoading} {...props}>
      <SearchDialogOverlay />
      <SearchDialogContent>
        <SearchDialogHeader>
          <SearchDialogIcon />
          <SearchDialogInput />
          <SearchDialogClose />
        </SearchDialogHeader>
        {/* The failure above was invisible for as long as it lasted: a 404 on
            the index reads exactly like "nothing matched". Whatever breaks it
            next — a missing export, an offline phone — says so out loud. */}
        {query.error ? (
          <p className="px-4 py-6 text-sm text-cb-error-ink dark:text-cb-error">
            The search index could not be loaded. Reload the page, or browse the sidebar.
          </p>
        ) : (
          <SearchDialogList items={query.data !== 'empty' ? query.data : null} />
        )}
      </SearchDialogContent>
    </SearchDialog>
  );
}
