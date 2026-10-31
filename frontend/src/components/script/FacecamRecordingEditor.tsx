import { useEffect, useRef } from 'react';
import { Extension } from '@tiptap/core';
import { useEditor, EditorContent } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Plugin, PluginKey } from '@tiptap/pm/state';
import { Decoration, DecorationSet } from '@tiptap/pm/view';
import type { FacecamBlock, RecordingWordChange } from '@/api/facecam';
import { Section, Marker, document as scriptDocument, blocksFrom, signature } from './facecamDocument';
import { recordingChangeRanges, mapRecordingChanges } from './facecamRecordingChanges';

const key = new PluginKey<RecordingWordChange[]>('facecamRecordingChanges');

export function FacecamRecordingEditor({blocks, changes, disabled, onChange}: {
  blocks: FacecamBlock[]; changes: RecordingWordChange[]; disabled: boolean;
  onChange: (blocks: FacecamBlock[], changes: RecordingWordChange[]) => void;
}) {
  const initial = useRef(changes), callback = useRef(onChange);
  callback.current = onChange;
  const editor = useEditor({
    extensions: [StarterKit.configure({heading: false, blockquote: false, codeBlock: false, bulletList: false, orderedList: false, horizontalRule: false}), Section, Marker,
      Extension.create({name: 'recordingReview', addProseMirrorPlugins() {const owner = this.editor; return [new Plugin({
        key,
        state: {
          init: () => initial.current,
          apply: (tr, value) => tr.getMeta('recordingLoad') ?? (tr.docChanged ? mapRecordingChanges(tr, value) : value),
        },
        filterTransaction: (tr, state) => !tr.docChanged || tr.getMeta('recordingLoad') || signature(tr.doc.toJSON()) === signature(state.doc.toJSON()),
        props: {
          decorations(state) {
            const decorations: Decoration[] = [];
            for (const {change, from, to} of recordingChangeRanges(state.doc, key.getState(state) ?? [])) {
              const title = change.kind === 'delete' ? `Accepter la suppression : ${change.expected.trim()}`
                : change.kind === 'insert' ? `Accepter l’ajout : ${change.spoken}`
                  : `Accepter le remplacement : ${change.expected.trim()} → ${change.spoken}`;
              if (from !== to) decorations.push(Decoration.inline(from, to, {
                class: 'facecam-recording-old', 'data-recording-change': change.id, title,
              }));
              decorations.push(Decoration.widget(to, view => {
                const button = window.document.createElement('button');
                button.type = 'button'; button.contentEditable = 'false';
                button.className = `facecam-recording-suggestion ${change.kind}`;
                button.textContent = change.kind === 'delete' ? '✓' : change.spoken;
                button.title = title; button.setAttribute('aria-label', title);
                button.disabled = !view.editable;
                button.addEventListener('mousedown', event => event.preventDefault());
                button.addEventListener('click', () => {
                  if (!view.editable) return;
                  const current = recordingChangeRanges(view.state.doc, key.getState(view.state) ?? []).find(r => r.change.id === change.id);
                  if (current) view.dispatch(view.state.tr.insertText(change.replacement, current.from, current.to));
                  view.focus();
                });
                return button;
              }, {key: `${change.id}:${from}:${to}:${owner.isEditable}`, stopEvent: () => true}));
            }
            return DecorationSet.create(state.doc, decorations);
          },
          handleClick(view, _pos, event) {
            if (!view.editable) return false;
            const id = (event.target as HTMLElement).closest('[data-recording-change]')?.getAttribute('data-recording-change');
            const current = recordingChangeRanges(view.state.doc, key.getState(view.state) ?? []).find(r => r.change.id === id);
            if (!current) return false;
            view.dispatch(view.state.tr.insertText(current.change.replacement, current.from, current.to));
            return true;
          },
        },
      })];}}),
    ],
    content: scriptDocument(blocks, true), editable: !disabled,
    editorProps: {attributes: {class: 'facecam-prose facecam-recording-prose', 'aria-label': 'Relecture du texte enregistré'}},
    onUpdate: ({editor, transaction}) => {
      if (transaction.docChanged && !transaction.getMeta('recordingLoad')) callback.current(blocksFrom(editor.getJSON()), key.getState(editor.state) ?? []);
    },
  });
  useEffect(() => {
    if (!editor) return;
    const tr = editor.state.tr;
    const values = (items: FacecamBlock[]) => items.map(b => [b.id, b.kind, b.text, b.scene_indices]);
    if (JSON.stringify(values(blocksFrom(editor.getJSON()))) !== JSON.stringify(values(blocks))) {
      const doc = editor.schema.nodeFromJSON(scriptDocument(blocks, true));
      tr.replaceWith(0, editor.state.doc.content.size, doc.content);
    }
    if (tr.docChanged || JSON.stringify(key.getState(editor.state)) !== JSON.stringify(changes)) {
      editor.view.dispatch(tr.setMeta('recordingLoad', changes).setMeta('addToHistory', false));
    }
  }, [blocks, changes, editor]);
  useEffect(() => {editor?.setEditable(!disabled, false);}, [editor, disabled]);
  return <div>
    <div className="facecam-recording-toolbar"><button type="button" disabled={disabled || !editor?.can().undo()} onClick={() => editor?.chain().focus().undo().run()}>Annuler la modification</button>
      <button type="button" disabled={disabled || !editor?.can().redo()} onClick={() => editor?.chain().focus().redo().run()}>Rétablir</button></div>
    <EditorContent editor={editor}/>
  </div>;
}
