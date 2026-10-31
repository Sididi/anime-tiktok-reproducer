import { useEffect, useRef } from 'react';
import { Extension } from '@tiptap/core';
import { useEditor, EditorContent } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Plugin, PluginKey } from '@tiptap/pm/state';
import { Decoration, DecorationSet } from '@tiptap/pm/view';
import type { FacecamBlock, Finding } from '@/api/facecam';
import { findingRanges, mapFindings } from './facecamFindings';
import { Section, Marker, signature, document, blocksFrom } from './facecamDocument';


export function FacecamEditor({blocks, findings, showFindings = true, disabled, onChange, onFinding}: {blocks: FacecamBlock[]; findings: Finding[]; showFindings?: boolean; disabled: boolean; onChange: (blocks: FacecamBlock[], findings: Finding[]) => void; onFinding: (finding: Finding) => void}) {
  const showFindingsRef = useRef(showFindings);
  const findingsRef = useRef(findings), onChangeRef = useRef(onChange), onFindingRef = useRef(onFinding);
  showFindingsRef.current = showFindings;
  findingsRef.current = findings; onChangeRef.current = onChange; onFindingRef.current = onFinding;
  const editor = useEditor({extensions: [StarterKit.configure({heading: false, blockquote: false, codeBlock: false, bulletList: false, orderedList: false, horizontalRule: false}), Section, Marker,
    Extension.create({name: 'facecamReview', addProseMirrorPlugins() {return [new Plugin({key: new PluginKey('facecamReview'),
      filterTransaction(tr, state) {return !tr.docChanged || tr.getMeta('facecamLoad') || signature(tr.doc.toJSON()) === signature(state.doc.toJSON());},
      props: {decorations(state) {if (!showFindingsRef.current) return DecorationSet.empty; return DecorationSet.create(state.doc,
        findingRanges(state.doc, findingsRef.current).map(({finding: f, index, from, to}) =>
          Decoration.inline(from, to, {class: `facecam-finding ${f.kind}`, 'data-finding': String(index), title: f.explanation})));},
      handleClick(_view, _pos, event) {if (!showFindingsRef.current) return false; const target = (event.target as HTMLElement).closest('[data-finding]'); if (!target) return false; const finding = findingsRef.current[Number(target.getAttribute('data-finding'))]; if (finding) onFindingRef.current(finding); return true;}}
    })];}})],
    content: document(blocks), editable: !disabled,
    editorProps: {attributes: {class: 'facecam-prose', 'aria-label': 'Script Facecam complet'}},
    onUpdate: ({editor, transaction}) => {if (transaction.docChanged && !transaction.getMeta("facecamLoad")) {
      findingsRef.current = mapFindings(transaction, findingsRef.current);
      onChangeRef.current(blocksFrom(editor.getJSON()), findingsRef.current);
    }},
  });
  useEffect(() => {if (!editor) return; if (JSON.stringify(blocksFrom(editor.getJSON()).map(b => [b.id,b.kind,b.text,b.scene_indices])) !== JSON.stringify(blocks.map(b => [b.id,b.kind,b.text,b.scene_indices]))) {const doc = editor.schema.nodeFromJSON(document(blocks)); editor.view.dispatch(editor.state.tr.replaceWith(0, editor.state.doc.content.size, doc.content).setMeta('facecamLoad', true).setMeta('addToHistory', false));}}, [blocks, editor]);
  useEffect(() => {editor?.setEditable(!disabled, false);}, [disabled, editor]);
  useEffect(() => {if (editor) editor.view.dispatch(editor.state.tr.setMeta('review', true));}, [findings, showFindings, editor]);
  return <EditorContent editor={editor}/>;
}
