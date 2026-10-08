import {basicSetup} from "codemirror";
import {Compartment} from "@codemirror/state";
import {EditorView, keymap} from "@codemirror/view";
import {StreamLanguage, HighlightStyle, syntaxHighlighting} from "@codemirror/language";
import {indentWithTab} from "@codemirror/commands";
import {openSearchPanel} from "@codemirror/search";
import {cpp} from "@codemirror/legacy-modes/mode/clike";
import {tags} from "@lezer/highlight";

const darkTheme = EditorView.theme({
  "&": {backgroundColor: "#020907", color: "#d8e9e3", height: "min(68vh, 760px)"},
  ".cm-content": {caretColor: "#59e1ae", fontFamily: '"Cascadia Mono","JetBrains Mono",Consolas,monospace', padding: "12px 0"},
  ".cm-cursor, .cm-dropCursor": {borderLeftColor: "#59e1ae"},
  "&.cm-focused .cm-selectionBackground, ::selection": {backgroundColor: "#245fa8 !important"},
  ".cm-selectionBackground": {backgroundColor: "#173f6f"},
  ".cm-gutters": {backgroundColor: "#06130f", color: "#66877b", border: "none", minWidth: "48px"},
  ".cm-activeLine, .cm-activeLineGutter": {backgroundColor: "#0d211b"},
  ".cm-foldPlaceholder": {backgroundColor: "#163029", border: "none", color: "#b7d2c8"},
  ".cm-panels": {backgroundColor: "#0b1d17", color: "#d8e9e3"},
  ".cm-panels.cm-panels-top": {borderBottom: "1px solid #285044"},
  ".cm-search input": {background: "#020907", color: "#e7f3ef", border: "1px solid #285044"},
  ".cm-search button": {background: "#163029", color: "#e7f3ef", border: "0"},
  ".cm-tooltip": {backgroundColor: "#0b1d17", color: "#d8e9e3", border: "1px solid #285044"},
}, {dark: true});

const smithproxyHighlight = HighlightStyle.define([
  {tag: [tags.keyword, tags.bool], color: "#68d9ff"},
  {tag: [tags.string, tags.special(tags.string)], color: "#a8e69d"},
  {tag: [tags.number, tags.integer, tags.float], color: "#eacb78"},
  {tag: [tags.comment, tags.lineComment, tags.blockComment], color: "#6f9387", fontStyle: "italic"},
  {tag: [tags.propertyName, tags.definition(tags.variableName)], color: "#77e3bb"},
  {tag: [tags.operator, tags.punctuation], color: "#a9c8bd"},
  {tag: tags.invalid, color: "#ff8b91", textDecoration: "underline"},
]);

function fontTheme(size) {
  return EditorView.theme({".cm-content, .cm-gutter": {fontSize: `${size}px`}});
}

function initializeEditor() {
  const mount = document.querySelector("#config-editor");
  const source = document.querySelector("#config-editor-content");
  if (!mount || !source) return;
  if (!mount.isConnected || mount.sasEditor) return;
  const wrap = new Compartment();
  const font = new Compartment();
  let wrapped = false;
  let fontSize = Number(localStorage.getItem("smithproxy-config-font-size") || 13);
  if (!Number.isFinite(fontSize) || fontSize < 10 || fontSize > 24) fontSize = 13;
  const initial = source.value;
  const stateLabel = document.querySelector("#editor-state");
  const view = new EditorView({
    doc: initial,
    parent: mount,
    extensions: [
      basicSetup, keymap.of([indentWithTab]), StreamLanguage.define(cpp),
      syntaxHighlighting(smithproxyHighlight), darkTheme, wrap.of([]),
      font.of(fontTheme(fontSize)),
      EditorView.updateListener.of(update => {
        if (!update.docChanged) return;
        source.value = update.state.doc.toString();
        source.dispatchEvent(new Event("input", {bubbles: true}));
        if (stateLabel) {
          stateLabel.textContent = source.value === initial ? window.sasTr("ui.314b7f35e20f") : window.sasTr("ui.eb98d79c9feb");
          stateLabel.classList.toggle("dirty", source.value !== initial);
        }
      }),
    ],
  });
  mount.sasEditor = view;
  mount.closest("dialog")?.addEventListener("close", () => {
    view.destroy();
    delete mount.sasEditor;
  }, {once: true});
  document.querySelector("#editor-find")?.addEventListener("click", () => {
    openSearchPanel(view); view.focus();
  });
  document.querySelector("#editor-wrap")?.addEventListener("click", event => {
    wrapped = !wrapped;
    view.dispatch({effects: wrap.reconfigure(wrapped ? EditorView.lineWrapping : [])});
    event.currentTarget.classList.toggle("active", wrapped); view.focus();
  });
  const resizeFont = delta => {
    fontSize = Math.max(10, Math.min(24, fontSize + delta));
    localStorage.setItem("smithproxy-config-font-size", String(fontSize));
    view.dispatch({effects: font.reconfigure(fontTheme(fontSize))}); view.focus();
  };
  document.querySelector("#editor-font-down")?.addEventListener("click", () => resizeFont(-1));
  document.querySelector("#editor-font-up")?.addEventListener("click", () => resizeFont(1));
  document.querySelector("#config-editor-form")?.addEventListener("submit", () => {
    source.value = view.state.doc.toString();
  });
  view.focus();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initializeEditor, {once: true});
else initializeEditor();
