/** A chart the API drew from the database, under an assistant reply.
 *
 * Built as a table: each row is a label, a bar and its value, so the table
 * view and the screen-reader reading are the chart itself, and nothing is
 * behind a hover. Horizontal bars because the dock is narrow and the labels
 * (`needs_review`, `0.80–1.00`, `Oct 13`) are words. One series, one colour:
 * `--color-data`, which means nothing else on the dashboard.
 *
 * A value that stands on too few applications is drawn lighter and says
 * "too few" beside it, so the warning is never carried by colour alone.
 */

export interface ChartPoint {
  label: string;
  value: number;
  /** The counts behind a rate: "1 of 5 submitted". */
  note?: string | null;
  /** False when the value stands on too few applications to read. */
  reliable?: boolean;
}

export interface Chart {
  kind: string;
  title: string;
  /** "applications" for counts, "%" for rates. */
  unit: string;
  caption: string;
  points: ChartPoint[];
}

function shown(value: number, unit: string): string {
  if (unit === "%") return `${Number.isInteger(value) ? value : value.toFixed(1)}%`;
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

export function AssistantChart({ chart }: { chart: Chart }) {
  // Rates are drawn against 100%, counts against the largest count. A rate
  // scaled to its own maximum would make 20% look like a full bar.
  const max = chart.unit === "%" ? 100 : Math.max(1, ...chart.points.map((point) => point.value));

  return (
    <figure className="mt-2 max-w-[90%] rounded border border-rule bg-paper px-3 py-2.5">
      <figcaption className="text-xs font-medium text-ink">{chart.title}</figcaption>
      <table className="mt-2 w-full border-separate border-spacing-y-1 text-xs">
        <thead className="sr-only">
          <tr>
            <th scope="col">{chart.unit === "%" ? "Band" : "Label"}</th>
            <th scope="col">Bar</th>
            <th scope="col">{chart.unit === "%" ? "Rate" : "Count"}</th>
          </tr>
        </thead>
        <tbody>
          {chart.points.map((point) => {
            const reliable = point.reliable !== false;
            const width = Math.max(0, Math.min(100, (point.value / max) * 100));
            return (
              <tr key={point.label} title={point.note ?? undefined}>
                <th
                  scope="row"
                  className="w-[1%] whitespace-nowrap pr-2 text-left align-top font-mono font-normal text-ink-soft"
                >
                  {point.label}
                </th>
                <td className="w-full align-middle">
                  <div className="h-3 w-full" aria-hidden="true">
                    {width > 0 ? (
                      <div
                        className={`h-3 rounded-r-[4px] bg-data ${reliable ? "" : "opacity-40"}`}
                        style={{ width: `${width}%` }}
                      />
                    ) : (
                      <div className="h-3 w-px bg-rule" />
                    )}
                  </div>
                  {/* Under the bar rather than beside the value: beside it, the
                      words pushed the table past a phone-width dock. */}
                  {point.note || !reliable ? (
                    <p className="mt-0.5 font-mono text-[11px] leading-tight text-ink-faint">
                      {[point.note, reliable ? null : "too few"].filter(Boolean).join(" · ")}
                    </p>
                  ) : null}
                </td>
                <td className="whitespace-nowrap pl-2 text-right align-top font-mono tabular-nums text-ink">
                  {shown(point.value, chart.unit)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-1.5 text-[11px] leading-snug text-ink-faint">{chart.caption}</p>
    </figure>
  );
}
