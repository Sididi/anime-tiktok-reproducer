import { useEffect, useRef } from 'react';
import { Node, Extension, type JSONContent } from '@tiptap/core';
import { useEditor, EditorContent } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Plugin, PluginKey } from '@tiptap/pm/state';
import { Decoration, DecorationSet } from '@tiptap/pm/view';
import type { FacecamBlock, Finding } from '@/api/facecam';

const Section = Node.create({name: 'facecamSection', group: 'block', content: 'paragraph+', isolating: true, defining: true,
  addAttributes() { return {id: {default: ''}, kind: {default: 'body'}, scene_indices: {default: []}}; },
  parseHTML() {return [{tag: 'section[data-facecam]'}];},
  renderHTML({node}) {return ['section', {'data-facecam': node.attrs.id, 'data-label': ({hook: 'Accroche', body: 'Récit', opinion: 'Point de vue'} as Record<string,string>)[node.attrs.kind] || ''}, 0];},
});
const Marker = Node.create({name: 'facecamMarker', group: 'block', atom: true, selectable: false,
  addAttributes() {return {id: {default: ''}, kind: {default: 'raw'}, text: {default: ''}, scene_indices: {default: []}};},
  parseHTML() {return [{tag: 'div[data-facecam-marker]'}];},
  renderHTML({node}) {return ['div', {'data-facecam-marker': node.attrs.id, contenteditable: 'false', class: 'facecam-marker'}, node.attrs.kind === 'raw' ? 'Extrait audio original' : node.attrs.text];},
});
function signature(doc: {content?: JSONContent[]}) {return JSON.stringify(doc.content?.map(n => [n.type, n.attrs?.id, n.attrs?.kind, n.attrs?.scene_indices, n.type === 'facecamMarker' ? n.attrs?.text : undefined]));}
function document(blocks: FacecamBlock[]): JSONContent {return {type: 'doc', content: blocks.map(b => b.kind === 'raw' || b.kind === 'ending' ? {type: 'facecamMarker', attrs: b} : {type: 'facecamSection', attrs: b, content: b.text.split('\n').map(t => ({type: 'paragraph', content: t ? [{type: 'text', text: t}] : []}))})};}
function blocksFrom(doc: JSONContent): FacecamBlock[] {return (doc.content || []).map(n => ({...n.attrs, text: n.type === 'facecamMarker' ? n.attrs?.text || '' : (n.content || []).map(p => (p.content || []).map(t => t.type === 'hardBreak' ? '\n' : t.text || '').join('')).join('\n')} as FacecamBlock));}

export function FacecamEditor({blocks, findings, disabled, onChange, onFinding}: {blocks: FacecamBlock[]; findings: Finding[]; disabled: boolean; onChange: (blocks: FacecamBlock[]) => void; onFinding: (finding: Finding) => void}) {
  const findingsRef = useRef(findings), onChangeRef = useRef(onChange), onFindingRef = useRef(onFinding);
  findingsRef.current = findings; onChangeRef.current = onChange; onFindingRef.current = onFinding;
  const editor = useEditor({extensions: [StarterKit.configure({heading: false, blockquote: false, codeBlock: false, bulletList: false, orderedList: false, horizontalRule: false}), Section, Marker,
    Extension.create({name: 'facecamReview', addProseMirrorPlugins() {return [new Plugin({key: new PluginKey('facecamReview'),
      filterTransaction(tr, state) {return !tr.docChanged || tr.getMeta('facecamLoad') || signature(tr.doc.toJSON()) === signature(state.doc.toJSON());},
      props: {decorations(state) {const spans: Decoration[] = []; const seen = new Set<number>(); state.doc.descendants((node, pos) => {
        if (!node.isTextblock) return;
        findingsRef.current.forEach((f, i) => {const at = node.textContent.indexOf(f.quote); if (at >= 0 && !seen.has(i)) {spans.push(Decoration.inline(pos + 1 + at, pos + 1 + at + f.quote.length, {class: `facecam-finding ${f.kind}`, 'data-finding': String(i), title: f.explanation})); seen.add(i);}});
      }); return DecorationSet.create(state.doc, spans);}, handleClick(_view, _pos, event) {const target = (event.target as HTMLElement).closest('[data-finding]'); if (!target) return false; const finding = findingsRef.current[Number(target.getAttribute('data-finding'))]; if (finding) onFindingRef.current(finding); return true;}}
    })];}})],
    content: document(blocks), editable: !disabled,
    editorProps: {attributes: {class: 'facecam-prose', 'aria-label': 'Script Facecam complet'}},
    onUpdate: ({editor, transaction}) => {if (transaction.docChanged && !transaction.getMeta("facecamLoad")) onChangeRef.current(blocksFrom(editor.getJSON()));},
  });
  useEffect(() => {if (!editor) return; if (JSON.stringify(blocksFrom(editor.getJSON()).map(b => [b.id,b.kind,b.text,b.scene_indices])) !== JSON.stringify(blocks.map(b => [b.id,b.kind,b.text,b.scene_indices]))) {const doc = editor.schema.nodeFromJSON(document(blocks)); editor.view.dispatch(editor.state.tr.replaceWith(0, editor.state.doc.content.size, doc.content).setMeta('facecamLoad', true).setMeta('addToHistory', false));}}, [blocks, editor]);
  useEffect(() => {editor?.setEditable(!disabled, false);}, [disabled, editor]);
  useEffect(() => {if (editor) editor.view.dispatch(editor.state.tr.setMeta('review', true));}, [findings, editor]);
  return <EditorContent editor={editor}/>;
}
