package tree_sitter_diff_test

import (
	"testing"

	tree_sitter "github.com/tree-sitter/go-tree-sitter"
	tree_sitter_diff "github.com/tree-sitter/tree-sitter-diff/bindings/go"
)

func TestCanLoadGrammar(t *testing.T) {
	language := tree_sitter.NewLanguage(tree_sitter_diff.Language())
	if language == nil {
		t.Errorf("Error loading Diff grammar")
	}
}
