/** The shapes an assistant reply uses: paragraphs, lists, pipe tables.
 *
 * Pure, so it can be checked without a browser. `components/reply-text.tsx`
 * turns these into elements, as text and never as HTML.
 */

export type Block =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "bullets"; items: string[] }
  | { kind: "numbered"; items: string[] }
  | { kind: "table"; header: string[] | null; rows: string[][] };

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

export function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

export function blocksOf(text: string): Block[] {
  const blocks: Block[] = [];
  const lines = text.split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (TABLE_ROW.test(line)) {
      const rows: string[][] = [];
      let header: string[] | null = null;
      while (index < lines.length && (TABLE_ROW.test(lines[index]) || TABLE_RULE.test(lines[index]))) {
        if (TABLE_RULE.test(lines[index])) {
          if (header === null && rows.length === 1) header = rows.pop() ?? null;
        } else {
          rows.push(cells(lines[index]));
        }
        index += 1;
      }
      blocks.push({ kind: "table", header, rows });
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);
    if (bullet || numbered) {
      const kind = bullet ? "bullets" : "numbered";
      const pattern = bullet ? BULLET : NUMBERED;
      const items: string[] = [];
      while (index < lines.length) {
        const match = pattern.exec(lines[index]);
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }
      blocks.push({ kind, items });
      continue;
    }
    const paragraph: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !TABLE_ROW.test(lines[index]) &&
      !BULLET.test(lines[index]) &&
      !NUMBERED.test(lines[index])
    ) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push({ kind: "paragraph", lines: paragraph });
  }
  return blocks;
}
