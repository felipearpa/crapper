import json
import subprocess
from pathlib import Path

import pytest

from crapper.languages.swift_coverage import read_derived_data, read_export


def _write(root: Path, relative: str, text: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _derived_data(
    root: Path, name: str = "DerivedData", device: str = "B2F2EDFE", folder: Path | None = None
) -> Path:
    """What `xcodebuild test -enableCodeCoverage YES -derivedDataPath ...` leaves."""

    derived = (folder or root) / name
    profile = _write(derived, f"Build/ProfileData/{device}/Coverage.profdata", "profile")
    products = derived / "Build" / "Products" / "Debug-iphonesimulator"
    _write(products, "FeatureTests.xctest/FeatureTests", "binary")
    _write(products, "Shop.app/Shop", "binary")
    _write(products, "Shop.app/Shop.debug.dylib", "binary")
    _write(products, "Shop.app/PlugIns/ShopTests.xctest/ShopTests", "binary")
    _write(products, "Feature.o", "object")
    return profile


def _record(path: Path | str, lines: dict[int, int]) -> str:
    """One file as `llvm-cov export -format=lcov` writes it: a DA line per executable line."""

    body = "".join(f"DA:{number},{hits}\n" for number, hits in lines.items())
    return f"SF:{path}\nFN:2,place\nFNDA:1,place\n{body}LF:{len(lines)}\nLH:0\nend_of_record\n"


def _lipo(command, archs: dict[str, str]):
    """`xcrun lipo -archs` lists each binary's slices: `archs[name]`, or one arm64 slice.

    An empty `archs[name]` is a file lipo cannot read.
    """

    if command[:2] != ["xcrun", "lipo"]:
        return None
    found = archs.get(Path(command[-1]).name, "arm64")
    if not found:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="can't figure out the architecture")
    return subprocess.CompletedProcess(command, 0, stdout=found + "\n")


def _fake_llvm_cov(
    monkeypatch, exports: dict[Path, str], calls: list | None = None, archs: dict[str, str] | None = None
):
    """`xcrun` is on the PATH and `llvm-cov` prints `exports[profile]` for each profile."""

    def run(command, **_kwargs):
        lipo = _lipo(command, archs or {})
        if lipo is not None:
            return lipo
        if calls is not None:
            calls.append(command)
        profile = Path(command[command.index("-instr-profile") + 1])
        return subprocess.CompletedProcess(command, 0, stdout=exports[profile], stderr="")

    monkeypatch.setattr("crapper.languages.swift_coverage.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("crapper.languages.swift_coverage.subprocess.run", run)


def test_only_the_executable_lines_of_the_scored_files_are_read(tmp_path, monkeypatch):
    board = _write(tmp_path, "Feature/Sources/Feature/Board.swift")
    profile = _derived_data(tmp_path)
    _fake_llvm_cov(
        monkeypatch,
        {
            profile: _record(board, {2: 3, 3: 0})
            + _record(tmp_path / "Feature/Tests/FeatureTests/BoardTests.swift", {1: 5})
            + _record("/Users/ci/checkouts/Lib/Sources/Lib/Lib.swift", {1: 5})
        },
    )
    assert read_derived_data(tmp_path / "DerivedData", [board]) == {str(board): {2: 3, 3: 0}}


def test_a_file_in_two_profiles_is_covered_where_either_covers_it(tmp_path, monkeypatch):
    board = _write(tmp_path, "Feature/Sources/Feature/Board.swift")
    first = _derived_data(tmp_path, device="IPHONE")
    second = _derived_data(tmp_path, device="IPAD")
    _fake_llvm_cov(
        monkeypatch,
        {
            first: _record(board, {2: 1, 3: 0, 4: 0}),
            # One input can list a file once per binary it is linked into.
            second: _record(board, {2: 0, 3: 2}) + _record(board, {4: 0, 5: 0}),
        },
    )
    assert read_derived_data(tmp_path / "DerivedData", [board]) == {str(board): {2: 1, 3: 2, 4: 0, 5: 0}}


def test_absolute_paths_are_kept_and_score_the_function_they_name(tmp_path, monkeypatch):
    from crapper.analyze import analyze_files
    from crapper.coverage import load_reports

    source = "func place(_ open: Bool) -> Int {\n    if open { return 1 }\n    return 0\n}\n"
    board = _write(tmp_path, "Feature/Sources/Feature/Board.swift", source)
    other = _write(tmp_path, "UI/Sources/Feature/Board.swift", source)
    profile = _derived_data(tmp_path)
    _fake_llvm_cov(monkeypatch, {profile: _record(board, {1: 2, 2: 2, 3: 0, 4: 0})})
    assert read_derived_data(tmp_path / "DerivedData", [board, other]) == {str(board): {1: 2, 2: 2, 3: 0, 4: 0}}
    entries = analyze_files([board, other], tmp_path, load_reports(tmp_path, ["DerivedData"], [board, other]))
    assert {entry.path: entry.coverage for entry in entries} == {
        "Feature/Sources/Feature/Board.swift": 50.0,
        "UI/Sources/Feature/Board.swift": 0.0,
    }


def _files_under(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def test_derived_data_outside_the_project_is_read_in_memory_and_nothing_is_written(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    board = _write(project, "Feature/Sources/Feature/Board.swift")
    elsewhere = tmp_path / "Library" / "Developer" / "Xcode" / "DerivedData"
    profile = _derived_data(tmp_path, name="App-abc", folder=elsewhere)
    _fake_llvm_cov(
        monkeypatch,
        {profile: _record(board, {2: 3, 3: 0}) + _record("/Users/ci/checkouts/Lib/Lib.swift", {1: 5})},
    )
    derived = elsewhere / "App-abc"
    before = _files_under(tmp_path)

    counts = read_derived_data(derived, [board])

    assert counts == {str(board): {2: 3, 3: 0}}
    assert read_derived_data(elsewhere, [board]) == counts
    assert _files_under(tmp_path) == before


def _llvm_cov_calls(tmp_path, monkeypatch, archs: dict[str, str]) -> tuple[list, Path, Path]:
    board = _write(tmp_path, "Board.swift")
    profile = _derived_data(tmp_path)
    calls = []
    _fake_llvm_cov(monkeypatch, {profile: _record(board, {1: 1})}, calls, archs)
    read_derived_data(tmp_path / "DerivedData", [board])
    return calls, profile, profile.parents[2] / "Products" / "Debug-iphonesimulator"


def test_llvm_cov_reads_each_profile_against_the_test_bundles_and_apps_built_beside_it(
    tmp_path, monkeypatch
):
    # llvm-cov pairs the -arch options with the binaries in order, so each
    # binary names its own; it refuses a universal binary without one.
    calls, profile, products = _llvm_cov_calls(tmp_path, monkeypatch, {})
    assert calls == [
        [
            "xcrun", "llvm-cov", "export", "-format=lcov", "-skip-functions",
            "-instr-profile", str(profile),
            str(products / "FeatureTests.xctest" / "FeatureTests"), "-arch=arm64",
            "-object", str(products / "Shop.app" / "Shop"), "-arch=arm64",
            "-object", str(products / "Shop.app" / "Shop.debug.dylib"), "-arch=arm64",
            "-object",
            str(products / "Shop.app" / "PlugIns" / "ShopTests.xctest" / "ShopTests"),
            "-arch=arm64",
        ]
    ]


def test_each_binary_names_arm64_when_it_has_that_slice_and_its_first_slice_otherwise(
    tmp_path, monkeypatch
):
    archs = {"FeatureTests": "x86_64", "Shop": "x86_64 arm64", "ShopTests": "x86_64 i386"}
    calls, _profile, _products = _llvm_cov_calls(tmp_path, monkeypatch, archs)
    assert [part for part in calls[0] if part.startswith("-arch")] == [
        "-arch=x86_64", "-arch=arm64", "-arch=arm64", "-arch=x86_64",
    ]


def test_a_universal_binary_with_one_slice_names_it(tmp_path, monkeypatch):
    # Xcode's UI test runner is a universal binary whose only slice is arm64:
    # lipo lists one architecture, as for a thin binary, and llvm-cov still
    # refuses it without -arch.
    runner = _write(tmp_path, "DerivedData/Build/Products/Debug-iphonesimulator/"
                    "ShopUITests-Runner.app/ShopUITests-Runner", "binary")
    calls, _profile, _products = _llvm_cov_calls(tmp_path, monkeypatch, {"ShopUITests-Runner": "arm64"})
    assert calls[0][calls[0].index(str(runner)) + 1] == "-arch=arm64"


def test_a_binary_lipo_cannot_read_leaves_every_binary_without_an_arch(tmp_path, monkeypatch, capsys):
    calls, _profile, products = _llvm_cov_calls(tmp_path, monkeypatch, {"Shop.debug.dylib": ""})
    assert calls and not [part for part in calls[0] if part.startswith("-arch")]
    err = capsys.readouterr().err
    assert f"architecture of {products / 'Shop.app' / 'Shop.debug.dylib'};" in err


# Segments as `swift test --enable-code-coverage` exports them for
#
#  2      public init() {}
#  4      public func label(_ score: Int) -> String {
#  5          guard score >= 0 else { return "invalid" }
#  6          if score >= 90 { return "A" } else { return "B" }
#  7      }
# 17      public func untested(_ value: Int) -> Int {
# 18          if value > 0 {
# 19              return value * 2
# 20          }
# 21          return -value
# 22      }
#
# after a test that calls `label(95)`: [line, column, count, hasCount, isRegionEntry, isGapRegion].
_GRADE_SEGMENTS = [
    [2, 19, 2, True, True, False],
    [2, 21, 0, False, False, False],
    [4, 47, 1, True, True, False],
    [5, 31, 0, True, True, False],
    [5, 51, 1, True, True, False],
    [6, 12, 1, True, True, False],
    [6, 23, 1, True, False, False],
    [6, 24, 1, True, True, False],
    [6, 38, 1, True, False, False],
    [6, 44, 0, True, True, False],
    [6, 58, 1, True, False, False],
    [7, 6, 0, False, False, False],
    [17, 47, 0, True, True, False],
    [18, 12, 0, True, True, False],
    [18, 21, 0, True, False, False],
    [18, 22, 0, True, True, False],
    [20, 10, 0, True, True, False],
    [21, 22, 0, True, False, False],
    [22, 6, 0, False, False, False],
]


def _export_file(path: Path | str, segments: list, count: int, covered: int) -> dict:
    return {
        "filename": str(path),
        "segments": segments,
        "branches": [],
        "expansions": [],
        "summary": {"lines": {"count": count, "covered": covered}},
    }


def _swift_test_export(root: Path, name: str, files: list[dict]) -> Path:
    """The file `swift test --show-codecov-path` names after `swift test --enable-code-coverage`."""

    export = {
        "type": "llvm.coverage.json.export",
        "version": "3.0.1",
        "data": [{"files": files, "functions": [], "totals": {}}],
    }
    return _write(root, name, json.dumps(export))


def test_an_export_converts_each_line_as_llvm_cov_counts_it(tmp_path, monkeypatch):
    grade = _write(tmp_path, "Sources/Scratch/Grade.swift")
    entry_point = tmp_path / ".build/out/DerivedSources/test_entry_point.swift"
    export = _swift_test_export(
        tmp_path,
        "Scratch.json",
        [
            _export_file(grade, _GRADE_SEGMENTS, count=11, covered=5),
            _export_file(entry_point, [[1, 1, 1, True, True, False], [2, 1, 0, False, False, False]], 1, 1),
        ],
    )
    monkeypatch.setattr("crapper.languages.swift_coverage.shutil.which", lambda name: None)
    counts = read_export(export, [grade])
    # Line 3 and lines 8 to 16 are not executable; lines 5 and 6 hold a covered
    # and an uncovered region and are covered; lines 17 to 22 are uncovered.
    assert list(counts) == [str(grade)]
    assert sorted(counts[str(grade)]) == [2, 4, 5, 6, 7, 17, 18, 19, 20, 21, 22]
    assert sorted(number for number, hits in counts[str(grade)].items() if hits) == [2, 4, 5, 6, 7]


@pytest.mark.parametrize("text", ['{"pins": []}', "{not json"])
def test_a_json_file_that_is_not_a_coverage_export_is_refused(tmp_path, text):
    grade = _write(tmp_path, "Sources/Scratch/Grade.swift")
    other = _write(tmp_path, "Package.resolved.json", text)
    with pytest.raises(ValueError, match="not a swift test coverage export"):
        read_export(other, [grade])


def test_an_export_of_an_unexpected_shape_is_refused_whole(tmp_path):
    grade = _write(tmp_path, "Sources/Scratch/Grade.swift")
    good = _export_file(grade, _GRADE_SEGMENTS[:2], 1, 1)
    # Each one reads a file of grade's before the part it does not expect.
    shapes = [
        _swift_test_export(tmp_path, "nameless.json", [good, {"segments": []}]),
        _swift_test_export(tmp_path, "short.json", [good, _export_file(grade, [[3, 1]], 1, 0)]),
        _write(
            tmp_path,
            "unit.json",
            json.dumps({"type": "llvm.coverage.json.export", "data": [{"files": [good]}, "not a unit"]}),
        ),
    ]
    for export in shapes:
        with pytest.raises(ValueError, match="not a swift test coverage export"):
            read_export(export, [grade])
    assert read_export(_swift_test_export(tmp_path, "Scratch.json", [good]), [grade]) == {str(grade): {2: 2}}


def test_an_export_given_by_path_is_read_in_memory_and_nothing_is_written(tmp_path, monkeypatch):
    grade = _write(tmp_path, "project/Sources/Scratch/Grade.swift")
    export = _swift_test_export(tmp_path / "elsewhere", "any-name", [_export_file(grade, _GRADE_SEGMENTS[:2], 1, 1)])
    monkeypatch.setattr("crapper.languages.swift_coverage.shutil.which", lambda name: None)
    before = _files_under(tmp_path)

    counts = read_export(export, [grade])

    assert counts == {str(grade): {2: 2}}
    assert _files_under(tmp_path) == before
