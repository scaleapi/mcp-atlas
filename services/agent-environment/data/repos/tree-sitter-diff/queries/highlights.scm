; These highlights are intentionally very simple so that they work
; well with `tree-sitter-cli`'s `highlight` command.
;
; For use in editors see:
; * <https://github.com/nvim-treesitter/nvim-treesitter/blob/master/queries/diff/highlights.scm>
; * <https://github.com/helix-editor/helix/blob/master/runtime/queries/diff/highlights.scm>
; * <https://github.com/zed-industries/zed/blob/main/crates/languages/src/diff/highlights.scm>

[(addition) (new_file)] @string
[(deletion) (old_file)] @keyword

(commit) @constant
(location) @attribute
(command) @variable.builtin
