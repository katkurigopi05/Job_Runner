import Link from "next/link";
import { POSTING_LINK } from "@/lib/external";

/** What a listed posting needs to be shown; the assistant's `Source` has all of it. */
export interface ListedPosting {
  posting_id: string;
  title: string;
  company: string | null;
  location?: string | null;
  label?: string | null;
  /** Identical listings of one job (same company, title and text) shown as one. */
  listing_count?: number;
  locations?: string[];
}

/** One posting in the assistant's list, with its repeats collapsed into one line. */
export function PostingLink({ source, muted }: { source: ListedPosting; muted?: boolean }) {
  return (
    <li>
      {source.label ? (
        <>
          <span className="text-ink-faint">{source.label}</span>{" "}
        </>
      ) : null}
      <Link
        href={`/postings/${source.posting_id}`}
        {...POSTING_LINK}
        className={`${muted ? "text-ink-soft" : "text-ink"} underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft`}
      >
        {source.title}
      </Link>
      {source.company ? <span className="text-ink-faint"> · {source.company}</span> : null}
      {(source.listing_count ?? 1) > 1 ? (
        <span className="text-ink-faint"> · listed {source.listing_count} times · {source.locations?.join("; ")}</span>
      ) : source.location ? <span className="text-ink-faint"> · {source.location}</span> : null}
    </li>
  );
}
