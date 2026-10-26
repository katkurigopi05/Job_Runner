/** An assistant reply, with its lists and tables laid out.
 *
 * The dock printed replies as preformatted text, so a model's markdown table
 * arrived as rows of `|` characters and a list as hyphens. This reads the few
 * shapes a reply actually uses (paragraphs, bulleted and numbered lists, pipe
 * tables, `**bold**`) and builds elements from them. Every piece of text goes
 * through React as text, never as HTML: a reply quotes postings and mail,
 * which other people wrote.
 *
 * Partial text is fine: while a reply streams, a half-written table renders
 * as the rows that have arrived.
 */

import type { ReactNode } from "react";
import { blocksOf } from "@/lib/reply-blocks";

/** `**bold**` as <strong>; everything else as the text it is. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
      <strong key={i} className="font-semibold">
        {part.slice(2, -2)}
      </strong>
    ) : (
      part
    ),
  );
}

export function ReplyText({ text }: { text: string }) {
  return (
    <div className="space-y-2">
      {blocksOf(text).map((block, i) => {
        if (block.kind === "paragraph") {
          return (
            <p key={i} className="whitespace-pre-wrap">
              {inline(block.lines.join("\n"))}
            </p>
          );
        }
        if (block.kind === "bullets" || block.kind === "numbered") {
          const List = block.kind === "bullets" ? "ul" : "ol";
          return (
            <List
              key={i}
              className={`space-y-0.5 pl-5 ${block.kind === "bullets" ? "list-disc" : "list-decimal"}`}
            >
              {block.items.map((item, j) => (
                <li key={j}>{inline(item)}</li>
              ))}
            </List>
          );
        }
        return (
          <div key={i} className="overflow-x-auto">
            <table className="min-w-full border-collapse text-xs">
              {block.header ? (
                <thead>
                  <tr>
                    {block.header.map((cell, j) => (
                      <th
                        key={j}
                        scope="col"
                        className="border-b border-rule px-2 py-1 text-left font-medium text-ink-soft"
                      >
                        {inline(cell)}
                      </th>
                    ))}
                  </tr>
                </thead>
              ) : null}
              <tbody>
                {block.rows.map((row, j) => (
                  <tr key={j}>
                    {row.map((cell, k) => (
                      <td key={k} className="border-b border-rule-soft px-2 py-1 align-top">
                        {inline(cell)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}
