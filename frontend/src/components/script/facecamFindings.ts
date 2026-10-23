import type { Node as ProseMirrorNode } from '@tiptap/pm/model';
import type { Transaction } from '@tiptap/pm/state';
import type { Finding } from '@/api/facecam';

// Backend offsets count Unicode code points in spoken_text(), while ProseMirror
// positions count UTF-16 units and structural tokens. Raw markers aren't spoken.
function coordinates(doc: ProseMirrorNode) {
  const chars: {text: string; from: number | null; to: number | null}[] = [];
  let spoken = false;
  doc.forEach((block, blockOffset) => {
    if (block.attrs.kind === 'raw') return;
    if (spoken) chars.push({text: '\n', from: null, to: null}, {text: '\n', from: null, to: null});
    spoken = true;
    if (block.attrs.kind === 'ending') {
      for (const text of Array.from(String(block.attrs.text))) chars.push({text, from: null, to: null});
      return;
    }
    block.forEach((paragraph, paragraphOffset, paragraphIndex) => {
      if (paragraphIndex) chars.push({text: '\n', from: null, to: null});
      paragraph.descendants((node, offset) => {
        let position = blockOffset + paragraphOffset + offset + 2;
        if (node.isText) {
          for (const text of Array.from(node.text ?? '')) {
            chars.push({text, from: position, to: position + text.length});
            position += text.length;
          }
        } else if (node.type.name === 'hardBreak') chars.push({text: '\n', from: position, to: position + 1});
      });
    });
  });
  return chars;
}

export function findingRanges(doc: ProseMirrorNode, findings: Finding[]) {
  const chars = coordinates(doc);
  return findings.flatMap((finding, index) => {
    const from = chars[finding.start]?.from, to = chars[finding.end - 1]?.to;
    if (from == null || to == null || finding.end <= finding.start ||
        chars.slice(finding.start, finding.end).map(c => c.text).join('') !== finding.quote) return [];
    return [{finding, index, from, to}];
  });
}

export function mapFindings(transaction: Transaction, findings: Finding[]): Finding[] {
  const chars = coordinates(transaction.doc);
  return findingRanges(transaction.before, findings).flatMap(({finding, from, to}) => {
    let touched = false;
    for (const map of transaction.mapping.maps) {
      map.forEach((start, end) => {
        if (start === end ? from < start && start < to : start < to && end > from) touched = true;
      });
      from = map.map(from, 1);
      to = map.map(to, -1);
    }
    if (touched) return [];
    const start = chars.findIndex(c => c.from === from), end = chars.findIndex(c => c.to === to) + 1;
    if (start < 0 || end <= start || chars.slice(start, end).map(c => c.text).join('') !== finding.quote) return [];
    return [{...finding, start, end}];
  });
}
