# crapper

This is a fork of [unclebob/crapper](https://github.com/unclebob/crapper) that adds F#, Kotlin, and Swift support. Thanks to Robert C. Martin for the original tool.

CRAP scores for Clojure, Java, Go, TypeScript, Rust, Python, F#, Kotlin, and Swift. One run detects the language of each source file, applies that language's complexity and coverage rules, and writes the snapshot [uml-viewer](https://github.com/unclebob/uml-viewer) already reads.

The formula is the one from [crap4clj](https://github.com/unclebob/crap4clj), [crap4java](https://github.com/unclebob/crap4java), and [crap4go](https://github.com/unclebob/crap4go):

```text
CRAP = CC² × (1 − coverage)³ + CC
```

`CC` is cyclomatic complexity. `coverage` is the fraction of the function exercised by tests. A score of 1–5 is low risk, 5–30 is worth a look, and 30+ is complex and under-tested.

## Run

```bash
./crapper
```

The first run creates `.venv` and installs the tool. From a project root it walks the tree, skips `test`, `spec`, `vendor`, `node_modules`, and `target` folders inside the project, and the iOS build and dependency folders `.build`, `Pods`, `Carthage`, and `DerivedData`, for every language (a project that itself lives in a folder named `tests` is still scored), and writes two results:

- a table on stdout, worst score first
- `.metrics/crap.edn`, replaced on every successful analysis

```bash
./crapper --no-coverage                  # complexity only; coverage is N/A
./crapper --use-existing-coverage        # read reports already on disk
./crapper --coverage-report reports/     # read these reports; run no test
./crapper src/demo/core.clj src/ui       # these files and trees
./crapper --changed                      # git additions and edits
./crapper --changed-functions            # only the functions those edits touched
./crapper combat                         # path fragment, same idea as crap4clj
./crapper --threshold 30                 # exit 2 when the worst score is higher
```

`--changed-functions` analyzes the same files as `--changed`, then intersects each function's line range with the hunks of `git diff -U0 HEAD`, and counts every line of an untracked file. The table shows only the functions that overlap a changed line, and `--threshold` judges only those. `.metrics/crap.edn` still holds every function of the changed files, so uml-viewer sees whole files. Before the first commit, every changed file counts as wholly changed. A function whose coverage dropped because only a test file changed is not detected: no line of it changed.

## Snapshot

`.metrics/crap.edn` has the same shape crap4clj writes:

```clojure
{:entries [{:name "place"
            :namespace "demo.Board"
            :complexity 3
            :coverage 75.0
            :crap 3.1}]}
```

uml-viewer groups entries by `:namespace` and joins each operation on `:name`. `nil` coverage is what `--no-coverage` writes, what F#, Kotlin, and Swift get without `--coverage-report`, and what a language gets when no report given with `--coverage-report` matches its files: complexity only, and CRAP is `nil` too. A function the coverage report does not mention scores 0%.

| Language | `:namespace` | `:name` |
| --- | --- | --- |
| Clojure | the `ns` | the `defn` / `defn-` name |
| Java | `package.Class`, or `package.Outer.Inner` | the method name |
| Go | the package import path, or `import/path.Receiver` | the function or method name |
| TypeScript | the dotted module path, or `module.Class` | the function or method name |
| Rust | `crate::module`, or `crate::module::Type` | the function or method name |
| Python | the dotted module path, or `module.Class` | the function or method name |
| F# | the `namespace` and `module` path, or `Namespace.Module.Type` | the function, member, or active pattern name |
| Kotlin | the `package`, or `package.Class` (`package.Outer.Inner`, `package.Class.Companion`, `package.Enum.VALUE`) | the function name |
| Swift | `Module.Type` (`Module.Outer.Inner`; an extension as the type it extends, `[T]`, `[K: V]`, and `T?` as `Array`, `Dictionary`, and `Optional`) when the path has `Sources/<Module>/`, otherwise `Type`; a top-level function takes `Module`, or the file stem | the function or property name, `init`, `deinit`, or `subscript` |

Rename or move is a new entry. There is no identity matching across runs.

## What each language counts

Clojure follows crap4clj: `if` / `when` and their variants, `and`, `or`, `loop`, `catch`, and each clause of `cond`, `condp`, `case`, `cond->`, `cond->>`, `some->`, and `some->>`. Coverage prefers Cloverage's per-line form counts and falls back to LCOV.

Java follows crap4java: `if`, loops, `catch`, `?:`, each `switch` label, and `&&` / `||`. Constructors and methods inside anonymous classes are omitted. Coverage is JaCoCo's `INSTRUCTION` counter.

Go follows crap4go: `if`, `for`, `range`, each `switch` and `select` clause, and `&&` / `||`. Coverage is `go test -coverprofile`, which names each file by its module import path (the `module` line of the nearest `go.mod` plus the path inside that module). crapper and mutator look a file up by that name first, then by the longest matching path suffix.

TypeScript, TSX, and JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`) use the same structural decisions as Java, including `&&` inside JSX, plus `??` and `?.`. Top-level functions, class methods, and top-level arrow functions are entries. An inline Express callback — `.get`, `.post`, `.put`, `.patch`, `.delete`, `.head`, `.options`, `.all`, `.use`, and `.route(path).get(...)` — is its own entry, named `GET /users`. A second callback on that same route is `GET /users#2`. Its decisions are not also charged to the enclosing function. Other nested callbacks stay inside the enclosing function. Coverage is LCOV. When the report has branch records (`BRDA`) inside a function, the score uses those branches; a function with no branches uses line hits. A `coverage` script is used as-is. A Vitest project runs `vitest --coverage` (installing `@vitest/coverage-v8` into `node_modules` when it is missing, without editing `package.json`). Other test scripts are wrapped in `c8`.

Rust counts `if`, loops, each `match` arm, `?`, and `&&` / `||`. `mod tests` is skipped. Coverage is LCOV from `cargo llvm-cov` or `cargo tarpaulin`, run in the nearest directory that contains `Cargo.toml`. When neither tool is installed, the run installs `cargo-llvm-cov`.

Python counts `if`, `elif`, `for`, `while`, `except`, each `match` case, a comprehension filter, a conditional expression, and each `and` / `or`. Nested functions stay inside the enclosing function. Coverage is LCOV from `coverage.py`, running pytest when the project uses it and `unittest` otherwise. `coverage` and `pytest` are installed into the project's interpreter when they are missing.

F# (`.fs` and `.fsx`) counts `if`, `elif`, `for`, `while`, each arm of `match`, `function`, and `try ... with`, each `when` guard, and `&&` / `||`; `let!` and combinators such as `Option.map` add nothing. Module- and type-level functions, members, active patterns, and values bound to a lambda or an object expression are entries. Signature files (`.fsi`), `bin`, `obj`, and `*.Test` / `*.Tests` folders are skipped. Coverage comes only from `--coverage-report` (see below): the Cobertura reports that `dotnet test --collect:"Code Coverage;Format=cobertura"` writes, one per test project. crapper never starts an F# test run itself.

Kotlin (`.kt`) counts `if`, `for`, `while`, `do ... while`, each `when` arm except `else`, each `catch`, and each `&&`, `||`, `?:`, and `?.`. Top-level functions, extension functions, and the functions of classes, objects, companion objects, and enum values with their own body are entries; a local function, a lambda, or an anonymous object stays in the enclosing function. Constructors, `init` blocks, property accessors, and abstract functions are not entries. A file with no `package` line uses the file stem as the namespace of its top-level functions. Gradle scripts (`.kts`), `build`, `src/test`, and `src/*Test` source sets such as `src/androidTest` and `src/commonTest` are skipped. Coverage comes only from `--coverage-report` (see below) given Kover's XML report, such as the `build/reports/kover/report.xml` that the project's own `./gradlew :koverXmlReport` writes; crapper never starts a Gradle run itself. It is Kover's `INSTRUCTION` counter, read like JaCoCo's: a function takes every method of its JVM class named after it, including `name-<hash>` (value-class mangling) and `name$...` (lambdas, `internal` names, default-argument bridges). Kover records no line numbers, so overloads share one percentage.

Swift (`.swift`) counts `if`, `guard`, `for`, `while`, `repeat ... while`, each `switch` case except `default`, each `catch`, each `&&`, `||`, `??`, ternary `? :`, and optional chaining `?.`, each comma between the conditions of an `if`, `guard`, or `while`, and a `where` clause on a `for`, `case`, or `catch`. So `guard let a, let b, c else` adds 3, a `case .a, .b:` adds 1, and `try?` adds nothing. Functions, initializers (`init`), `deinit`, and `subscript` with a body are entries, and so are properties with a body: a computed property (getter and setter are one entry), `willSet` / `didSet` observers, and a property initialised by a closure, each named after the property. A nested function or a closure stays in the enclosing entry, and protocol requirements are not entries. The namespace starts with the module, the folder after `Sources/` in the file's absolute path (`Feature/Sources/Feature/...` gives `Feature.FeatureViewModel`), so it does not change with the root crapper runs from; a path without `Sources/<Module>/`, such as an app target, gets the type chain alone. `Package.swift` and folders named `Tests` or `*Tests` (`CoreTests`, `AppUITests`) are skipped, also under `--changed`. The `.build`, `Pods`, `Carthage`, and `DerivedData` folders are skipped for every language, like `vendor`, so the Python, C, or JavaScript files of a package's checkouts are not scored either; a project that itself lives in such a folder is still scored, and naming one on the command line (`crapper Pods/Lib`) scores it. Coverage comes only from `--coverage-report` (see below): crapper never starts a Swift test run itself.

## Coverage commands

By default a run deletes the previous report for each language it is about to measure and regenerates it:

| Language | Command | Report |
| --- | --- | --- |
| Clojure | `clj -M:cov --lcov` (needs `deps.edn` or `bb.edn`) | `target/coverage/` |
| Java | JaCoCo Maven plugin `0.8.12` in each module with `pom.xml` | `target/site/jacoco/jacoco.xml` |
| Go | `go test ./... -coverprofile=...` | `target/coverage/go/coverage.out` |
| TypeScript | `npm run coverage`, or Vitest `--coverage`, or `npx c8 ... npm test` | `coverage/lcov.info` or `target/coverage/typescript/lcov.info` |
| Rust | `cargo llvm-cov` or `cargo tarpaulin`, per Cargo package | `target/coverage/rust/lcov.info` |
| Python | `coverage run` with pytest or unittest, then `coverage lcov` | `target/coverage/python/lcov.info` |

A missing tool or a failed test run scores that language at 0% and still writes the snapshot. Pass `--coverage-command` to replace those defaults with one command of your own. Coverage and CRAP show N/A under `--no-coverage`, for F#, Kotlin, and Swift without `--coverage-report`, and for a language no report given with `--coverage-report` matches.

F#, Kotlin, and Swift have no row: they get coverage only from `--coverage-report`. crapper starts no `dotnet`, Gradle, `xcodebuild`, or `llvm-cov` run, converts nothing after a `--coverage-command`, deletes none of their reports, and does not look their functions up in any report it finds on its own, so a Cobertura report under `target/coverage/fsharp/`, Kover's `build/reports/kover/report.xml`, Xcode derived data or an `lcov.info` under `target/coverage/`, or a JaCoCo report holding Kotlin classes does not score them. Without the option, in a plain run, with `--use-existing-coverage`, and with `--coverage-command`, their functions show coverage and CRAP as N/A, in the table and in `.metrics/crap.edn`, and crapper prints one hint for each of the three languages present, such as `F#: coverage and CRAP show N/A; crapper reads F# coverage only from --coverage-report. Pass --coverage-report <the Cobertura reports of dotnet test>.` Under `--no-coverage` no hint is printed. The other languages of the same root still run and score as above.

Pass `--coverage-report <path>` to score from reports your own test run wrote, wherever it put them. With it crapper runs no test and no command, and reads coverage only from the paths given, not from the locations above. The path is a report file or a folder, in which every file that holds a report is read; the option may be repeated. A relative path is read inside the project root (`--root`, or the current directory), like every other path crapper takes. crapper tells a report by its content, not its name:

| What the path holds | Read as |
| --- | --- |
| A folder holding `Build/ProfileData/*/*.profdata`, directly or one folder down | Xcode derived data, as `xcodebuild test -enableCodeCoverage YES -derivedDataPath <folder>` leaves it, converted with `xcrun llvm-cov` (Swift) |
| XML whose root element is `coverage` | Cobertura, as `dotnet test --collect:"Code Coverage;Format=cobertura"` writes it (F#) |
| XML whose root element is `report`, with or without a DOCTYPE | JaCoCo or Kover, such as `build/reports/kover/report.xml` (Java, Kotlin) |
| Text with `SF:` records | LCOV (TypeScript, Rust, Python, and others) |
| Text starting with `mode:` | a Go coverage profile |
| JSON whose `type` is `llvm.coverage.json.export` | a `swift test` export, the file `swift test --show-codecov-path` prints (Swift) |

In a folder, every file under it that matches a row is read and any other file is skipped without a word. Several reports combine: Cobertura line by line, a line covered when any report covered it; the last LCOV record read for a file replaces an earlier one; JaCoCo and Kover methods are added together, and so are Go segments; a Cobertura record of a file wins over an LCOV record of it. A file reached twice, such as a file and the folder it is in, is read once. Clojure given a path is scored from LCOV lines, not Cloverage's per-line form counts, so its scores can differ slightly from the built-in run.

Swift's derived data and exports are converted in memory for that run: nothing is written into the project or the derived data, which may lie anywhere, such as Xcode's default `~/Library/Developer/Xcode/DerivedData/<App>-<id>`. crapper reads each profile under `Build/ProfileData/` with `xcrun llvm-cov export -format=lcov`, against the test bundles and apps in `Build/Products/` beside it, so packages linked into those binaries are covered too (`xccov` reports them as empty), and keeps the executable lines of the Swift files it scores; a file in several profiles or binaries counts a line as covered when any of them covered it. An export is read without `xcrun`: crapper derives each line's count from the export's regions the way `llvm-cov` does. A folder recognised as derived data is only converted, never searched for other reports, because its package checkouts hold test fixtures that look like reports; derived data two or more folders inside a given folder is searched like any other folder, not converted: its files are read when they are reports. Swift inputs given as separate paths, derived data and exports alike, are added together line by line: a line counts as covered when any of them covered it, whatever the order of the paths. For example:

```bash
xcodebuild test -workspace App.xcworkspace -scheme App -destination "platform=iOS Simulator,name=iPhone 17" -enableCodeCoverage YES -derivedDataPath build/DerivedData
crapper --coverage-report build/DerivedData

swift test --enable-code-coverage
crapper --coverage-report "$(swift test --show-codecov-path)"
```

When a path does not exist, or holds no report crapper reads, crapper prints that on stderr and exits 1 without a table, leaving `.metrics/crap.edn` as it was. A report that cannot be parsed, such as a half-written file, is an error in the same way when its path is given directly; inside a given folder it is skipped with a warning naming it, and the other reports still score, unless none could be read. Given derived data that cannot be converted is an error in the same way, naming the reason: no `xcrun`, as on Linux; `llvm-cov` failing on a profile; or no test bundle or app beside a profile. `--coverage-report` cannot be combined with `--no-coverage` or `--coverage-command`; either pair prints the error and the help and exits 1. With `--use-existing-coverage` it behaves as it does alone.

With `--coverage-report`, a language none of whose scored files is in any given report shows coverage and CRAP as N/A, in the table and in `.metrics/crap.edn`, and crapper prints one hint for that language on stderr, naming the reports to pass for it, such as `Swift: coverage and CRAP show N/A; no coverage report given matches its files. Pass --coverage-report <Xcode derived data or a swift test export>.` This holds for every language, since with the option crapper runs no test: in a root with Kotlin and Swift, a Kover report alone scores the Kotlin and leaves the Swift N/A. Once one file of a language is in a report, a function of that language the reports leave out scores 0%, as it does without the option. Java and Kotlin files count as in a report when one of their classes is.

Two warnings on stderr point at a report that is read but probably not the one meant; the table is printed after either:

- `--coverage-report <path>: matches none of the files being scored`: none of the reports at that given path holds a file being scored, as with a report made in another checkout (Cobertura and Xcode match files by their path in the project) or for another project. The languages it was meant for show N/A. With `--changed-functions`, a report can honestly hold none of the few files a change touched.
- `<report>: Cobertura report with branch lines that ran and carry no condition-coverage`, once per report: the report marks a line as a branch, with hits, but gives no `condition-coverage` value. A solution-wide `dotnet test` writes such a report when it merges its test projects' reports, and crapper, which scores a function by its branches when it has any and skips a branch line without that value, then scores those functions wrongly, some at 0% although their lines ran. Run `dotnet test` with `VSTEST_DISABLE_ARTIFACTS_POSTPROCESSING=1` in front, so each test project keeps its own report, and pass their folder.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
