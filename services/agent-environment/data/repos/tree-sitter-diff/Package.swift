// swift-tools-version:5.5

import PackageDescription

let package = Package(
    name: "TreeSitterDiff",
    platforms: [.macOS(.v10_13), .iOS(.v11)],
    products: [
        .library(name: "TreeSitterDiff", targets: ["TreeSitterDiff"]),
    ],
    dependencies: [],
    targets: [
        .target(name: "TreeSitterDiff",
                path: ".",
                exclude: [
                    "assets",
                    "binding.gyp",
                    "bindings",
                    "Cargo.toml",
                    "docs",
                    "grammar.js",
                    "LICENSE",
                    "package-lock.json",
                    "package.json",
                    "README.md",
                    "src/grammar.json",
                    "src/node-types.json",
                    "test",
                ],
                sources: [
                    "src/parser.c",
                ],
                resources: [
                    .copy("queries")
                ],
                publicHeadersPath: "bindings/swift",
                cSettings: [.headerSearchPath("src")])
    ]
)
