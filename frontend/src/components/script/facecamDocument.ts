import { Node, type JSONContent } from '@tiptap/core';
import type { FacecamBlock } from '@/api/facecam';

export const Section = Node.create({name: 'facecamSection', group: 'block', content: 'paragraph+', isolating: true, defining: true,
  addAttributes() { return {id: {default: ''}, kind: {default: 'body'}, scene_indices: {default: []}}; },
  parseHTML() {return [{tag: 'section[data-facecam]'}];},
  renderHTML({node}) {return ['section', {'data-facecam': node.attrs.id, 'data-label': ({hook: 'Accroche', body: 'Récit', opinion: 'Point de vue', ending: 'Révélation'} as Record<string,string>)[node.attrs.kind] || ''}, 0];},
});
export const Marker = Node.create({name: 'facecamMarker', group: 'block', atom: true, selectable: false,
  addAttributes() {return {id: {default: ''}, kind: {default: 'raw'}, text: {default: ''}, scene_indices: {default: []}};},
  parseHTML() {return [{tag: 'div[data-facecam-marker]'}];},
  renderHTML({node}) {return ['div', {'data-facecam-marker': node.attrs.id, contenteditable: 'false', class: 'facecam-marker'}, node.attrs.kind === 'raw' ? 'Extrait audio original' : node.attrs.text];},
});
export function signature(doc: {content?: JSONContent[]}) {return JSON.stringify(doc.content?.map(n => [n.type, n.attrs?.id, n.attrs?.kind, n.attrs?.scene_indices, n.type === 'facecamMarker' ? n.attrs?.text : undefined]));}
export function document(blocks: FacecamBlock[], editEnding = false): JSONContent {return {type: 'doc', content: blocks.map(b => b.kind === 'raw' || (b.kind === 'ending' && !editEnding) ? {type: 'facecamMarker', attrs: b} : {type: 'facecamSection', attrs: b, content: b.text.split('\n').map(t => ({type: 'paragraph', content: t ? [{type: 'text', text: t}] : []}))})};}
export function blocksFrom(doc: JSONContent): FacecamBlock[] {return (doc.content || []).map(n => ({...n.attrs, text: n.type === 'facecamMarker' ? n.attrs?.text || '' : (n.content || []).map(p => (p.content || []).map(t => t.type === 'hardBreak' ? '\n' : t.text || '').join('')).join('\n')} as FacecamBlock));}

