import XCTest
import SwiftTreeSitter
import TreeSitterDiff

final class TreeSitterDiffTests: XCTestCase {
    func testCanLoadGrammar() throws {
        let parser = Parser()
        let language = Language(language: tree_sitter_diff())
        XCTAssertNoThrow(try parser.setLanguage(language),
                         "Error loading Diff grammar")
    }
}
