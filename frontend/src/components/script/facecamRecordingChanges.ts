import type { Node as ProseMirrorNode } from '@tiptap/pm/model';
import type { Transaction } from '@tiptap/pm/state';
import type { RecordingWordChange } from '@/api/facecam';

function coordinates(doc: ProseMirrorNode) {
  const blocks = new Map<string, {text: string; from: number; to: number}[]>();
  doc.forEach((block, blockOffset) => {
    if (block.type.name !== 'facecamSection') return;
    const chars: {text: string; from: number; to: number}[] = [];
    block.forEach((paragraph, paragraphOffset, index) => {
      if (index) chars.push({text: '\n', from: blockOffset + paragraphOffset, to: blockOffset + paragraphOffset + 2});
      paragraph.descendants((node, offset) => {
        let position = blockOffset + paragraphOffset + offset + 2;
        if (node.isText) for (const text of Array.from(node.text ?? '')) {
          chars.push({text, from: position, to: position + text.length}); position += text.length;
        }
        else if (node.type.name === 'hardBreak') chars.push({text: '\n', from: position, to: position + 1});
      });
    });
    blocks.set(block.attrs.id, chars);
  });
  return blocks;
}

export function recordingChangeRanges(doc: ProseMirrorNode, changes: RecordingWordChange[]) {
  const blocks = coordinates(doc);
  return changes.flatMap(change => {
    const chars = blocks.get(change.block_id);
    if (!chars) return [];
    const from = chars[change.start]?.from ?? (change.start === chars.length ? chars.at(-1)?.to : undefined);
    const to = change.start === change.end ? from : chars[change.end - 1]?.to;
    if (from == null || to == null || chars.slice(change.start, change.end).map(c => c.text).join('') !== change.expected) return [];
    return [{change, from, to}];
  });
}

export function mapRecordingChanges(transaction: Transaction, changes: RecordingWordChange[]) {
  const blocks = coordinates(transaction.doc);
  return recordingChangeRanges(transaction.before, changes).flatMap(({change, from, to}) => {
    let touched = false;
    for (const map of transaction.mapping.maps) {
      map.forEach((start, end) => {
        if (from === to ? start <= from && end >= from : start === end ? from < start && start < to : start < to && end > from) touched = true;
      });
      from = map.map(from, 1); to = map.map(to, -1);
    }
    if (touched) return [];
    const chars = blocks.get(change.block_id) ?? [];
    let start = chars.findIndex(c => c.from === from);
    if (start < 0 && from === chars.at(-1)?.to) start = chars.length;
    const end = change.start === change.end ? start : chars.findIndex(c => c.to === to) + 1;
    if (start < 0 || end < start || chars.slice(start, end).map(c => c.text).join('') !== change.expected) return [];
    return [{...change, start, end}];
  });
}
