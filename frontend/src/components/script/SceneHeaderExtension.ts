import { Node, mergeAttributes } from "@tiptap/core";

export const SceneHeader = Node.create({
  name: "sceneHeader",
  group: "block",
  atom: true,
  selectable: false,
  draggable: false,
  isolating: true,

  addAttributes() {
    return {
      sceneIndex: {
        default: 0,
        parseHTML: (element) => parseInt(element.getAttribute("data-scene-index") || "0"),
        renderHTML: (attributes) => ({ "data-scene-index": attributes.sceneIndex }),
      },
    };
  },

  parseHTML() {
    return [{ tag: 'div[data-scene-header="true"]' }];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "div",
      mergeAttributes(HTMLAttributes, {
        "data-scene-header": "true",
        class:
          "scene-header-chip bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] text-xs font-semibold uppercase select-none pointer-events-none px-2 py-0.5 rounded w-fit whitespace-nowrap",
        contenteditable: "false",
      }),
      `Scene ${HTMLAttributes["data-scene-index"] + 1}`,
    ];
  },

  addKeyboardShortcuts() {
    return {
      Backspace: ({ editor }) => {
        const { $anchor } = editor.state.selection;
        const nodeBefore = $anchor.nodeBefore;
        // Prevent backspace from deleting scene header when cursor is right after one
        if (nodeBefore?.type.name === "sceneHeader") {
          return true;
        }
        // Also prevent when at start of a textblock that follows a scene header
        if ($anchor.parentOffset === 0) {
          const resolvedPos = editor.state.doc.resolve($anchor.pos - 1);
          if (resolvedPos.nodeBefore?.type.name === "sceneHeader") {
            return true;
          }
        }
        return false;
      },
      Delete: ({ editor }) => {
        const { $anchor } = editor.state.selection;
        const nodeAfter = $anchor.nodeAfter;
        if (nodeAfter?.type.name === "sceneHeader") {
          return true;
        }
        return false;
      },
    };
  },
});
