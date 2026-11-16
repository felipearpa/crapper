import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from crapper.cli import (
    GitError,
    _changed_files,
    _positionals,
    _take,
    main,
    parse_args,
    run,
    select_files,
)
from crapper import runners
from crapper.discover import is_test_file, iter_source_files, language_of


def test_language_detection():
    assert language_of("src/app/core.clj") == "clojure"
    assert language_of("src/app/core.cljc") == "clojure"
    assert language_of("Widget.java") == "java"
    assert language_of("board.go") == "go"
    assert language_of("ui/view.tsx") == "typescript"
    assert language_of("src/app.js") == "typescript"
    assert language_of("src/app.mjs") == "typescript"
    assert language_of("src/app.cjs") == "typescript"
    assert language_of("src/app.jsx") == "typescript"
    assert language_of("src/lib.rs") == "rust"
    assert language_of("src/crapper/cli.py") == "python"
    assert language_of("types.d.ts") is None
    assert language_of("notes.md") is None
    assert language_of("app.cts") == "typescript"
    assert language_of("app.test.cts") == "typescript"
    assert is_test_file("app.test.cts") is True
    assert is_test_file("app.spec.cts") is True
    assert is_test_file("app.cts") is False


def test_fsharp_sources_are_fsharp_and_signature_files_are_not_scored():
    assert language_of("src/Board.fs") == "fsharp"
    assert language_of("scripts/build.fsx") == "fsharp"
    assert language_of("src/Board.fsi") is None


def test_kotlin_sources_are_kotlin_and_gradle_scripts_are_not_scored():
    assert language_of("app/src/main/kotlin/Board.kt") == "kotlin"
    assert language_of("app/build.gradle.kts") is None
    assert is_test_file("app/src/androidTest/kotlin/BoardTest.kt") is True
    assert is_test_file("shared/src/commonTest/kotlin/BoardTest.kt") is True
    assert is_test_file("app/src/main/kotlin/Board.kt") is False
    assert is_test_file("app/src/androidTest/java/BoardTest.java") is False


def test_swift_sources_are_swift_and_manifests_tests_and_build_folders_are_not_scored():
    assert language_of("Feature/Sources/Feature/FeatureView.swift") == "swift"
    assert language_of("Feature/Package.swift") is None
    assert is_test_file("Feature/Tests/FeatureTests/FeatureViewTests.swift") is True
    assert is_test_file("App/AppUITests/LaunchTests.swift") is True
    assert is_test_file("Feature/Sources/Feature/FeatureView.swift") is False
    assert is_test_file("shared/CoreTests/kotlin/Board.kt") is False


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_status(root: Path, status: str):
    def fake(args, **_kwargs):
        if "rev-parse" in args:
            return _Completed(0, f"{root.resolve()}\n")
        return _Completed(0, status)

    return fake


def test_project_snapshot_is_readable_as_crap4clj_edn(tmp_path):
    _write(
        tmp_path,
        "src/demo/core.clj",
        "(ns demo.core)\n\n(defn choose [x]\n  (if x 1 0))\n",
    )
    _write(
        tmp_path,
        "src/demo/Board.java",
        "package demo;\npublic class Board {\n  public int place(int x) {\n    if (x > 0 && ready) return 1;\n    return 0;\n  }\n}\n",
    )
    _write(
        tmp_path,
        "board.go",
        "package demo\n\nfunc Place(x int) int {\n\tif x > 0 {\n\t\treturn x\n\t}\n\treturn 0\n}\n",
    )
    _write(
        tmp_path,
        "src/ui/view.ts",
        "export function view(ok: boolean): number {\n  return ok ? 1 : 0;\n}\n",
    )
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(
        tmp_path,
        "src/lib.rs",
        "pub fn open(ok: bool) -> i32 { if ok { 1 } else { 0 } }\n",
    )
    _write(tmp_path, "src/demo/core_test.clj", "(ns demo.core-test)\n(defn should-skip [] 1)\n")
    _write(tmp_path, "board_test.go", "package demo\nfunc TestPlace(t *testing.T) {}\n")

    code = run(["--root", str(tmp_path), "--no-coverage"])
    assert code == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':namespace "demo.core"' in text
    assert ':name "choose"' in text
    assert ':namespace "demo.Board"' in text
    assert ':name "place"' in text
    assert ':namespace "demo"' in text and ':name "Place"' in text
    assert ':namespace "ui.view"' in text
    assert ':namespace "demo"' in text and ':name "open"' in text
    assert "should-skip" not in text
    assert "TestPlace" not in text

    reader = subprocess.run(
        [
            "bb",
            "-e",
            (
                "(require '[clojure.edn :as edn])"
                "(let [data (edn/read-string (slurp *in*))]"
                " (doseq [entry (:entries data)]"
                "   (println (:namespace entry) (:name entry)"
                "            (:complexity entry) (pr-str (:coverage entry)))))"
            ),
        ],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    assert reader.returncode == 0, reader.stderr
    assert "demo.core choose 2 nil" in reader.stdout
    assert "demo.Board place 3 nil" in reader.stdout


def test_existing_coverage_feeds_the_snapshot(tmp_path):
    _write(
        tmp_path,
        "src/demo/core.clj",
        "(ns demo.core)\n\n(defn choose [x]\n  (if x 1 0))\n",
    )
    lcov = tmp_path / "target" / "coverage" / "lcov.info"
    lcov.parent.mkdir(parents=True)
    lcov.write_text(
        "SF:src/demo/core.clj\nDA:3,1\nDA:4,1\nend_of_record\n",
        encoding="utf-8",
    )
    code = run(
        ["--root", str(tmp_path), "--use-existing-coverage", "src/demo/core.clj"]
    )
    assert code == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ":coverage 100.0" in text
    assert ":crap 2.0" in text


def test_threshold_exits_when_the_worst_score_is_too_high(tmp_path):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [x]\n  (if x 1 0))\n")
    lcov = tmp_path / "target" / "coverage" / "lcov.info"
    lcov.parent.mkdir(parents=True)
    lcov.write_text("SF:src/demo/core.clj\nDA:3,0\nDA:4,0\nend_of_record\n", encoding="utf-8")
    code = run(
        [
            "--root",
            str(tmp_path),
            "--use-existing-coverage",
            "--threshold",
            "1",
        ]
    )
    assert code == 2


def test_help_and_unknown_option(capsys):
    assert run(["--help"]) == 0
    help_text = capsys.readouterr().out
    assert "uml-viewer" in help_text
    assert ".clj-kondo" in help_text
    assert ".test.cts" in help_text
    assert run(["--not-an-option"]) == 1
    assert run(["--root"]) == 1
    assert run(["--threshold", "nope"]) == 1
    assert run(["--no-coverage", "--coverage-command", "true"]) == 1


def test_selects_directories_filters_and_changed_files(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [x]\n  (if x 1 0))\n")
    _write(tmp_path, "notes.md", "not source\n")
    assert run(["--root", str(tmp_path), "--no-coverage", "--source-root", "src"]) == 0
    assert run(["--root", str(tmp_path), "--no-coverage", "src"]) == 0
    assert run(["--root", str(tmp_path), "--no-coverage", "src/demo/core.clj", "core"]) == 0
    assert run(["--root", str(tmp_path), "--no-coverage", "notes.md"]) == 0
    assert "No source files" in capsys.readouterr().out

    monkeypatch.setattr(
        "crapper.cli.subprocess.run",
        _git_status(tmp_path, " M src/demo/core.clj\0"),
    )
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    assert ':name "choose"' in (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")

    monkeypatch.setattr(
        "crapper.cli.subprocess.run",
        lambda *_args, **_kwargs: _Completed(128, "", "fatal: not a git repository"),
    )
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 128
    captured = capsys.readouterr()
    assert "not a git repository" in captured.err
    assert "No source files" not in captured.out


def test_run_asks_for_coverage_when_it_is_enabled(tmp_path, monkeypatch):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [x]\n  x)\n")
    called = []
    monkeypatch.setattr("crapper.cli.run_coverage", lambda *args: called.append(args))
    assert run(["--root", str(tmp_path), "--coverage-command", "true"]) == 0
    assert called


def test_empty_option_value_is_rejected():
    with pytest.raises(ValueError, match="requires a value"):
        _take(["ok", "--opt", ""], 1, "--opt")
    with pytest.raises(ValueError, match="requires a value"):
        _take(["--opt", "-1"], 0, "--opt")


def test_parse_args_reads_the_process_arguments_after_the_program(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["crapper", "--threshold", "5"])
    options = parse_args()
    assert options.threshold == 5.0
    assert options.positionals == []
    assert options.action == "analyze"


def test_flags_that_only_store_a_boolean():
    assert parse_args(["--use-existing-coverage"]).use_existing_coverage is True
    assert parse_args(["--changed"]).changed is True
    assert parse_args(["--changed-functions"]).changed_functions is True
    assert parse_args(["--no-coverage"]).no_coverage is True
    conflict = parse_args(["--no-coverage", "--coverage-command", "true"])
    assert conflict.exit_code == 1


def test_changed_files_follow_git_status(tmp_path, monkeypatch, capsys):
    seen = {}
    named = tmp_path / "x"
    named.write_text("x", encoding="utf-8")
    keep = tmp_path / "sources" / "keep.py"
    keep.parent.mkdir()
    keep.write_text("x = 1\n", encoding="utf-8")

    def fake(args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        if "rev-parse" in args:
            return _Completed(0, f"{tmp_path.resolve()}\n")
        return _Completed(0, " M x\0?? sources/keep.py\0")

    monkeypatch.setattr("crapper.cli.subprocess.run", fake)
    found = _changed_files(tmp_path)
    assert seen["check"] is False
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["encoding"] == "utf-8"
    assert "-z" in seen["args"]
    assert "--no-renames" in seen["args"]
    assert "--untracked-files=all" in seen["args"]
    assert found == [named.resolve(), keep.resolve()]

    monkeypatch.setattr(
        "crapper.cli.subprocess.run",
        lambda *_a, **_k: _Completed(1, "", "git failed"),
    )
    with pytest.raises(GitError, match="git failed") as failed:
        _changed_files(tmp_path)
    assert failed.value.code == 1
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(
        "crapper.cli.subprocess.run",
        lambda *_a, **_k: _Completed(128, "", ""),
    )
    with pytest.raises(GitError, match="git status failed") as silent:
        _changed_files(tmp_path)
    assert silent.value.code == 128


def test_relative_paths_are_resolved_under_the_project_root(tmp_path):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [x]\n  (if x 1 0))\n")
    existing, filters = _positionals(tmp_path, ["src/demo/core.clj"])
    assert existing == [(tmp_path / "src/demo/core.clj").resolve()]
    assert filters == []
    assert run(["--root", str(tmp_path), "--no-coverage", "src/demo/core.clj"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "choose"' in text
    assert "parse_args" not in text


def test_changed_selection_ignores_files_git_did_not_name(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "src/kept.clj", "(ns demo.kept)\n\n(defn kept [] 1)\n")
    _write(tmp_path, "src/other.clj", "(ns demo.other)\n\n(defn other [] 1)\n")

    monkeypatch.setattr(
        "crapper.cli.subprocess.run",
        _git_status(tmp_path, "?? src/kept.clj\0"),
    )
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert "other" not in text
    assert "No source files" not in capsys.readouterr().out


def test_changed_selection_skips_swift_manifests_tests_and_build_folders(tmp_path, monkeypatch):
    changed = {
        "Feature/Sources/Feature/FeatureView.swift": "struct FeatureView {\n    func kept() {}\n}\n",
        "Feature/Package.swift": "func manifest() {}\n",
        "Feature/Tests/FeatureTests/FeatureViewTests.swift": "func fromTests() {}\n",
        "App/AppTests/AppTests.swift": "func fromAppTests() {}\n",
        "App/Pods/Lib/Lib.swift": "func fromPods() {}\n",
        "App/.build/checkouts/Dep.swift": "func fromBuild() {}\n",
        "App/Carthage/Checkouts/Lib.swift": "func fromCarthage() {}\n",
        "App/DerivedData/Gen.swift": "func fromDerivedData() {}\n",
    }
    for relative, text in changed.items():
        _write(tmp_path, relative, text)
    status = "".join(f"?? {relative}\0" for relative in changed)
    monkeypatch.setattr("crapper.cli.subprocess.run", _git_status(tmp_path, status))
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert text.count(":name ") == 1


@pytest.mark.parametrize("outer", ["Pods", "DerivedData"])
def test_changed_selection_keeps_swift_files_of_a_project_inside_a_build_folder_name(
    tmp_path, monkeypatch, outer
):
    root = tmp_path / outer / "MyApp"
    changed = {
        "Sources/App/App.swift": "struct App {\n    func kept() {}\n}\n",
        "Pods/Lib/Lib.swift": "func fromPods() {}\n",
        ".build/checkouts/Dep.swift": "func fromBuild() {}\n",
    }
    for relative, text in changed.items():
        _write(root, relative, text)
    status = "".join(f"?? {relative}\0" for relative in changed)
    monkeypatch.setattr("crapper.cli.subprocess.run", _git_status(root, status))
    assert run(["--root", str(root), "--no-coverage", "--changed"]) == 0
    text = (root / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert text.count(":name ") == 1


def _commit_then_modify(repo: Path, files: dict[str, str]) -> None:
    """Commit `files` (relative path to contents), then modify each one."""

    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init"], check=True, capture_output=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    for relative, text in files.items():
        _write(repo, relative, text)
    subprocess.run([*git, "add", "-A", "-f"], check=True)
    subprocess.run([*git, "commit", "-m", "init"], check=True, capture_output=True)
    for relative, text in files.items():
        _write(repo, relative, text + "\n")


def test_changed_selection_skips_fsharp_build_folders_in_a_real_repository(tmp_path):
    _commit_then_modify(
        tmp_path,
        {
            "Demo/Board.fs": "module Demo.Board\n\nlet kept x = x\n",
            "Demo/bin/Debug/Gen.fs": "module Demo.Gen\n\nlet fromBin x = x\n",
            "Demo/obj/Debug/Gen.fs": "module Demo.Gen\n\nlet fromObj x = x\n",
        },
    )
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert text.count(":name ") == 1


_PROJECT_WITH_VENDOR_AND_BUILD = {
    "src/app.py": "def kept():\n    return 1\n",
    "vendor/lib/dep.py": "def from_vendor():\n    return 1\n",
    "build/generated/gen.py": "def from_build():\n    return 1\n",
    "Pods/Lib/Lib.swift": "func fromPods() {}\n",
}


def test_changed_selection_skips_committed_vendor_and_build_folders(tmp_path):
    _commit_then_modify(tmp_path, _PROJECT_WITH_VENDOR_AND_BUILD)
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert text.count(":name ") == 1


@pytest.mark.parametrize(
    ("args", "name"),
    [
        (["vendor"], "from_vendor"),
        (["--source-root", "build/generated"], "from_build"),
        (["Pods/Lib"], "fromPods"),
        (["--source-root", "Pods/Lib"], "fromPods"),
    ],
)
def test_a_skipped_folder_named_on_the_command_line_is_scored(tmp_path, args, name):
    for relative, text in _PROJECT_WITH_VENDOR_AND_BUILD.items():
        _write(tmp_path, relative, text)
    assert run(["--root", str(tmp_path), "--no-coverage", *args]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert f':name "{name}"' in text
    assert text.count(":name ") == 1


# A Swift package next to dependency checkouts and Xcode output in every language.
_IOS_PROJECT_WITH_BUILD_FOLDERS = {
    "Feature/Sources/Feature/FeatureView.swift": "struct FeatureView {\n    func kept() {}\n}\n",
    "Feature/.build/checkouts/dep/scripts/lint.py": "def from_checkouts():\n    return 1\n",
    "Feature/.build/index-build/checkouts/dep/gen.js": "function fromIndexBuild() {\n  return 1;\n}\n",
    "App/Pods/P/z.py": "def from_pods():\n    return 1\n",
    "App/Carthage/Checkouts/c/c.go": "package c\n\nfunc FromCarthage() int { return 1 }\n",
    "App/DerivedData/App/Build/d.py": "def from_derived_data():\n    return 1\n",
}


def test_ios_build_folders_are_skipped_for_every_language(tmp_path):
    _commit_then_modify(tmp_path, _IOS_PROJECT_WITH_BUILD_FOLDERS)
    kept = (tmp_path / "Feature/Sources/Feature/FeatureView.swift").resolve()
    assert iter_source_files([tmp_path], tmp_path) == [kept]
    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert text.count(":name ") == 1


@pytest.mark.parametrize("outer", ["build", "vendor", ".build", "Pods", "Carthage", "DerivedData"])
def test_a_project_inside_a_skipped_folder_name_keeps_its_sources(tmp_path, outer):
    root = tmp_path / outer / "app"
    _commit_then_modify(
        tmp_path,
        {
            f"{outer}/app/src/app.py": "def kept():\n    return 1\n",
            f"{outer}/app/{outer}/dep.py": "def skipped():\n    return 1\n",
        },
    )
    assert iter_source_files([root], root) == [(root / "src/app.py").resolve()]
    assert run(["--root", str(root), "--no-coverage", "--changed"]) == 0
    text = (root / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert text.count(":name ") == 1


# A project whose own path runs through a folder named like a test folder:
# (folder the project lives in, its source file, a test file in a test folder inside it).
_PROJECTS_IN_TEST_FOLDER_NAMES = {
    "python": (
        "tests/app",
        ("src/app/core.py", "def kept():\n    return 1\n"),
        ("tests/helpers.py", "def helper():\n    return 1\n"),
    ),
    "typescript": (
        "spec/app",
        ("src/core.ts", "export function kept() {\n  return 1;\n}\n"),
        ("src/__tests__/helpers.ts", "export function helper() {\n  return 1;\n}\n"),
    ),
    "go": (
        "test/app",
        ("core.go", "package app\n\nfunc kept() int {\n\treturn 1\n}\n"),
        ("test/helpers.go", "package test\n\nfunc helper() int {\n\treturn 1\n}\n"),
    ),
    "swift": (
        "MyTests/App",
        ("Sources/App/App.swift", "func kept() {}\n"),
        ("AppTests/Helpers.swift", "func helper() {}\n"),
    ),
    "kotlin": (
        "src/androidTest/App",
        ("app/src/main/kotlin/Board.kt", "fun kept(): Int {\n    return 1\n}\n"),
        ("app/src/androidTest/kotlin/Helper.kt", "fun helper(): Int {\n    return 1\n}\n"),
    ),
    "fsharp": (
        "Demo.Tests/App",
        ("Demo/Board.fs", "module Demo.Board\n\nlet kept x = x + 1\n"),
        ("Demo.Tests/Helper.fs", "module Demo.Helper\n\nlet helper x = x + 1\n"),
    ),
}


def _project_in_test_folder_name(tmp_path, language):
    outer, source, test = _PROJECTS_IN_TEST_FOLDER_NAMES[language]
    root = tmp_path / outer
    for relative, text in (source, test):
        _write(root, relative, text)
    return root, source[0], test[0]


@pytest.mark.parametrize("language", sorted(_PROJECTS_IN_TEST_FOLDER_NAMES))
def test_a_project_inside_a_test_folder_name_keeps_its_sources_but_not_its_tests(
    tmp_path, language
):
    root, source, _test = _project_in_test_folder_name(tmp_path, language)
    assert iter_source_files([root]) == [(root / source).resolve()]


def test_changed_selection_keeps_sources_of_a_project_inside_a_test_folder_name(
    tmp_path, monkeypatch
):
    root, source, test = _project_in_test_folder_name(tmp_path, "python")
    status = f"?? {source}\0?? {test}\0"
    monkeypatch.setattr("crapper.cli.subprocess.run", _git_status(root, status))
    assert run(["--root", str(root), "--no-coverage", "--changed"]) == 0
    text = (root / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "kept"' in text
    assert "helper" not in text


@pytest.mark.parametrize(
    "args",
    [
        ["tests"],
        ["app/src/androidTest"],
        ["-s", "app/src/androidTest"],
        ["-s", "app/src/androidTest/kotlin"],
    ],
)
def test_a_path_inside_a_test_folder_of_the_project_selects_nothing(
    tmp_path, monkeypatch, args
):
    for relative in (
        "tests/helpers.py",
        "app/src/androidTest/kotlin/Helper.kt",
    ):
        _write(tmp_path, relative, "x\n")
    monkeypatch.chdir(tmp_path)
    assert select_files(parse_args(["--root", str(tmp_path), *args])) == []


def test_a_source_root_named_src_still_skips_its_kotlin_test_source_sets(tmp_path, monkeypatch):
    _write(tmp_path, "app/src/main/kotlin/Board.kt", "fun kept() = 1\n")
    _write(tmp_path, "app/src/androidTest/kotlin/Helper.kt", "fun helper() = 1\n")
    monkeypatch.chdir(tmp_path)
    selected = select_files(parse_args(["--root", str(tmp_path), "-s", "app/src"]))
    assert selected == [(tmp_path / "app/src/main/kotlin/Board.kt").resolve()]


def test_threshold_equal_to_the_score_is_allowed(tmp_path):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [] 1)\n")
    lcov = tmp_path / "target" / "coverage" / "lcov.info"
    lcov.parent.mkdir(parents=True)
    lcov.write_text("SF:src/demo/core.clj\nDA:1,1\nDA:3,1\nend_of_record\n", encoding="utf-8")
    code = run(
        ["--root", str(tmp_path), "--use-existing-coverage", "--threshold", "1", "src/demo/core.clj"]
    )
    assert code == 0


def test_threshold_with_no_scores_does_not_call_max_on_an_empty_list(tmp_path):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [] 1)\n")
    assert run(["--root", str(tmp_path), "--no-coverage", "--threshold", "1"]) == 0


def test_a_failed_coverage_command_ignores_a_stale_report(tmp_path, monkeypatch):
    _write(tmp_path, "src/demo/core.clj", "(ns demo.core)\n\n(defn choose [x]\n  (if x 1 0))\n")
    lcov = tmp_path / "target" / "coverage" / "lcov.info"
    lcov.parent.mkdir(parents=True)
    lcov.write_text("SF:src/demo/core.clj\nDA:3,1\nDA:4,1\nend_of_record\n", encoding="utf-8")
    monkeypatch.setattr("crapper.cli.run_coverage", lambda *_args, **_kwargs: 3)
    code = run(
        ["--root", str(tmp_path), "--coverage-command", "exit 3", "src/demo/core.clj"]
    )
    assert code == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ":coverage 0.0" in text
    assert ":crap 6.0" in text

    monkeypatch.setattr("crapper.cli.run_coverage", lambda *_args, **_kwargs: 0)
    assert run(
        ["--root", str(tmp_path), "--coverage-command", "true", "src/demo/core.clj"]
    ) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ":coverage 100.0" in text


def test_changed_files_from_a_real_git_status(tmp_path):
    _git = ["git", "-C", str(tmp_path)]
    subprocess.run([*_git, "init"], check=True, capture_output=True)
    subprocess.run([*_git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*_git, "config", "user.name", "t"], check=True)
    keep = tmp_path / "keep.go"
    keep.write_text("package demo\nfunc Keep() int { return 1 }\n", encoding="utf-8")
    subprocess.run([*_git, "add", "keep.go"], check=True)
    subprocess.run([*_git, "commit", "-m", "init"], check=True, capture_output=True)
    keep.unlink()
    cafe = tmp_path / "café.go"
    cafe.write_text("package demo\nfunc Cafe() int { return 1 }\n", encoding="utf-8")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "new.go").write_text("package demo\nfunc New() int { return 1 }\n", encoding="utf-8")

    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    text = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "Cafe"' in text
    assert ':name "New"' in text
    assert "Keep" not in text

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "inside.go").write_text("package pkg\nfunc Inside() int { return 1 }\n", encoding="utf-8")
    (tmp_path / "outside.go").write_text(
        "package demo\nfunc Outside() int { return 1 }\n",
        encoding="utf-8",
    )
    assert run(["--root", str(pkg), "--no-coverage", "--changed"]) == 0
    nested = (pkg / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "Inside"' in nested
    assert "Outside" not in nested
    assert "Cafe" not in nested
    assert "New" not in nested


def test_changed_outside_a_repository_returns_gits_status(tmp_path, capsys):
    _write(tmp_path, "src/app.py", "def run():\n    return 1\n")
    code = run(["--root", str(tmp_path), "--no-coverage", "--changed"])
    assert code != 0
    captured = capsys.readouterr()
    assert "No source files" not in captured.out
    assert captured.err.strip()


def test_uml_loader_rejects_a_missing_viewer(tmp_path):
    script = Path(__file__).resolve().parents[1] / "uml"
    completed = subprocess.run(
        [str(script)],
        env={**os.environ, "UML_VIEWER_ROOT": str(tmp_path / "missing")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "uml-viewer checkout not found" in completed.stderr


def test_main_exits_with_the_status(monkeypatch):
    monkeypatch.setattr("crapper.cli.run", lambda _argv=None: 4)
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 4
    else:
        raise AssertionError("main did not exit")


def _repo(root: Path) -> list[str]:
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init"], check=True, capture_output=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    return git


def _repo_with_one_edited_function(root: Path, relative: str = "app.py") -> list[str]:
    git = _repo(root)
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "def edited(x):\n    return x\n\n\ndef untouched(x):\n    return x\n",
        encoding="utf-8",
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-m", "init"], check=True, capture_output=True)
    source.write_text(
        "def edited(x):\n    if x:\n        return 1\n    return x\n\n\ndef untouched(x):\n    return x\n",
        encoding="utf-8",
    )
    return git


def test_changed_functions_reports_only_the_functions_an_edit_touched(tmp_path, capsys):
    _repo_with_one_edited_function(tmp_path)

    assert run(["--root", str(tmp_path), "--no-coverage", "--changed-functions"]) == 0
    table = capsys.readouterr().out
    assert "edited" in table
    assert "untouched" not in table
    snapshot = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':name "edited"' in snapshot
    assert ':name "untouched"' in snapshot


def test_changed_still_reports_every_function_of_an_edited_file(tmp_path, capsys):
    _repo_with_one_edited_function(tmp_path)

    assert run(["--root", str(tmp_path), "--no-coverage", "--changed"]) == 0
    out = capsys.readouterr().out
    assert "edited" in out
    assert "untouched" in out


def test_changed_functions_reports_an_edited_function_in_a_file_whose_name_has_a_space(tmp_path, capsys):
    _repo_with_one_edited_function(tmp_path, "my app.py")

    assert run(["--root", str(tmp_path), "--no-coverage", "--changed-functions"]) == 0
    assert "edited" in capsys.readouterr().out


@pytest.mark.parametrize("setting", ["diff.mnemonicPrefix", "diff.relative"])
def test_changed_functions_ignores_diff_settings_that_rewrite_paths(tmp_path, capsys, setting):
    git = _repo_with_one_edited_function(tmp_path, "sub/app.py")
    subprocess.run([*git, "config", setting, "true"], check=True)

    assert run(["--root", str(tmp_path / "sub"), "--no-coverage", "--changed-functions"]) == 0
    assert "edited" in capsys.readouterr().out


def test_changed_functions_reports_nothing_for_an_edit_outside_every_function(tmp_path, capsys):
    git = _repo(tmp_path)
    source = tmp_path / "app.py"
    source.write_text("def kept(x):\n    return x\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-m", "init"], check=True, capture_output=True)
    source.write_text("def kept(x):\n    return x\n\n\n# note\n", encoding="utf-8")

    assert run(["--root", str(tmp_path), "--no-coverage", "--changed-functions"]) == 0
    assert "kept" not in capsys.readouterr().out
    assert ':name "kept"' in (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")


def test_a_directory_outside_the_project_root_is_judged_from_itself(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "tests" / "app"
    _write(other, "src/core.py", "def kept():\n    return 1\n")
    _write(other, "tests/helpers.py", "def helper():\n    return 1\n")
    selected = select_files(parse_args(["--root", str(project), str(other)]))
    assert selected == [(other / "src/core.py").resolve()]


def _one_line_cobertura(filename: str, line: int, hits: int) -> str:
    """A Cobertura report as `dotnet test` writes it, byte-order mark first."""

    return (
        '﻿<?xml version="1.0" encoding="utf-8"?>\n<coverage><packages><package name="Demo">'
        f'<classes><class name="Demo" filename="{filename}"><methods/><lines>'
        f'<line number="{line}" hits="{hits}" branch="False" /></lines></class></classes>'
        "</package></packages></coverage>"
    )


def _snapshot_coverage(root: Path, name: str) -> str:
    text = (root / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    return re.search(rf':name "{name}", [^}}]*:coverage ([^,]+),', text).group(1)


def test_coverage_report_paths_score_from_their_reports_and_run_nothing(tmp_path, monkeypatch):
    root = tmp_path / "project"
    for module in ("Board", "Other", "Stale"):
        _write(root, f"Demo/{module}.fs", f"module Demo.{module}\n\nlet {module.lower()} x = x + 1\n")
    _write(root, "reports/unit.xml", _one_line_cobertura(str(root / "Demo/Board.fs"), 3, 1))
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere, "any-name.txt", _one_line_cobertura(str(root / "Demo/Other.fs"), 3, 1))
    _write(elsewhere, "notes.txt", "not a report\n")
    _write(
        root,
        "target/coverage/fsharp/old/me.cobertura.xml",
        _one_line_cobertura(str(root / "Demo/Stale.fs"), 3, 1),
    )
    called = []
    monkeypatch.setattr("crapper.cli.run_coverage", lambda *args, **_kwargs: called.append(args))
    monkeypatch.chdir(tmp_path)

    code = run(
        [
            "--root",
            str(root),
            "--coverage-report",
            "reports/unit.xml",
            "--coverage-report",
            str(elsewhere),
        ]
    )

    assert code == 0
    assert called == []
    assert _snapshot_coverage(root, "board") == "100.0"
    assert _snapshot_coverage(root, "other") == "100.0"
    assert _snapshot_coverage(root, "stale") == "0.0"


@pytest.mark.parametrize("given", ["missing", "empty"])
def test_a_coverage_report_path_with_no_report_stops_before_scoring(tmp_path, capsys, given):
    _write(tmp_path, "Demo/Board.fs", "module Demo.Board\n\nlet board x = x + 1\n")
    (tmp_path / "empty").mkdir()
    assert run(["--root", str(tmp_path), "--no-coverage"]) == 0
    snapshot = tmp_path / ".metrics" / "crap.edn"
    before = snapshot.read_text(encoding="utf-8")
    capsys.readouterr()

    code = run(["--root", str(tmp_path), "--coverage-report", given])

    captured = capsys.readouterr()
    assert code == 1
    assert str(tmp_path / given) in captured.err
    assert "--coverage-report" in captured.err
    assert "CRAP Report" not in captured.out
    assert snapshot.read_text(encoding="utf-8") == before


_JACOCO_WITH_DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
    '<report name="demo"><package name="demo"><class name="demo/Board">'
    '<method name="board" desc="(I)I" line="4">'
    '<counter type="INSTRUCTION" missed="0" covered="4"/></method>'
    "</class></package></report>"
)


def test_jacoco_lcov_and_go_reports_are_told_by_content_whatever_their_name(tmp_path):
    _write(
        tmp_path,
        "src/main/java/demo/Board.java",
        "package demo;\n\npublic class Board {\n    public int board(int x) {\n        return x + 1;\n    }\n}\n",
    )
    _write(tmp_path, "app.py", "def app(x):\n    return x + 1\n")
    _write(tmp_path, "walk.go", "package demo\n\nfunc walk(x int) int {\n\treturn x + 1\n}\n")
    _write(tmp_path, "given/one.data", _JACOCO_WITH_DOCTYPE)
    _write(tmp_path, "given/two.data", "TN:\nSF:app.py\nDA:2,1\nend_of_record\n")
    _write(tmp_path, "elsewhere/three.data", "mode: set\nwalk.go:3.27,5.2 1 1\n")

    code = run(
        ["--root", str(tmp_path), "--coverage-report", "given", "--coverage-report", "elsewhere/three.data"]
    )

    assert code == 0
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert _snapshot_coverage(tmp_path, "app") == "100.0"
    assert _snapshot_coverage(tmp_path, "walk") == "100.0"


def test_given_reports_merge_as_the_fixed_locations_do_and_each_file_is_read_once(tmp_path, capsys):
    _write(tmp_path, "Demo/Board.fs", "module Demo.Board\n\nlet board x =\n    x + 1\n")
    _write(tmp_path, "reports/a.xml", _one_line_cobertura(str(tmp_path / "Demo/Board.fs"), 4, 1))
    _write(tmp_path, "reports/b.xml", _one_line_cobertura(str(tmp_path / "Demo/Board.fs"), 4, 0))
    _write(tmp_path, "reports/z.info", "SF:Demo/Board.fs\nDA:4,0\nend_of_record\n")
    _write(tmp_path, "reports/nested/half.xml", "<coverage><packages><pack")
    _write(tmp_path, "reports/nested/other.info", "SF:Demo/Other.fs\nDA:1,1\nend_of_record\n")

    code = run(
        [
            "--root",
            str(tmp_path),
            "--coverage-report",
            "reports/a.xml",
            "--coverage-report",
            "reports",
            "--coverage-report",
            "reports/nested",
        ]
    )

    assert code == 0
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert capsys.readouterr().err.count("half.xml") == 1


_DAMAGED = {
    "cobertura": ("half.xml", "<coverage><packages><pack"),
    "jacoco": ("half.xml", '<?xml version="1.0"?><!DOCTYPE report><report name="demo"><pack'),
    "go": ("half.out", "mode: set\nwalk.go:3.27"),
}


@pytest.mark.parametrize("kind", sorted(_DAMAGED))
def test_a_damaged_report_alone_stops_crapper_and_in_a_folder_is_skipped_with_a_warning(
    tmp_path, capsys, kind
):
    name, text = _DAMAGED[kind]
    _write(tmp_path, "Demo/Board.fs", "module Demo.Board\n\nlet board x = x + 1\n")
    _write(tmp_path, f"reports/{name}", text)
    _write(tmp_path, "reports/good.xml", _one_line_cobertura(str(tmp_path / "Demo/Board.fs"), 3, 1))
    _write(tmp_path, f"broken/{name}", text)

    alone = run(["--root", str(tmp_path), "--coverage-report", f"reports/{name}"])
    alone_err = capsys.readouterr()
    folder_then_file = run(
        ["--root", str(tmp_path), "--coverage-report", "reports", "--coverage-report", f"reports/{name}"]
    )
    folder_then_file_err = capsys.readouterr()
    only_damaged = run(["--root", str(tmp_path), "--coverage-report", "broken"])
    only_damaged_err = capsys.readouterr().err
    in_folder = run(["--root", str(tmp_path), "--coverage-report", "reports"])
    in_folder_err = capsys.readouterr().err

    assert alone == 1
    assert str(tmp_path / "reports" / name) in alone_err.err
    assert "CRAP Report" not in alone_err.out
    assert f"{name}: unreadable" in alone_err.err and "skipped" not in alone_err.err
    assert folder_then_file == 1
    assert f"--coverage-report {tmp_path / 'reports' / name}: unreadable" in folder_then_file_err.err
    assert "CRAP Report" not in folder_then_file_err.out
    assert only_damaged == 1
    assert str(tmp_path / "broken") in only_damaged_err
    assert in_folder == 0
    assert f"{name}: unreadable" in in_folder_err and "skipped" in in_folder_err
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert "Traceback" not in alone_err.err + folder_then_file_err.err + only_damaged_err + in_folder_err


@pytest.mark.parametrize("other", [["--no-coverage"], ["--coverage-command", "true"]])
def test_a_coverage_report_cannot_be_combined_with_no_coverage_or_a_command(other):
    options = parse_args(["--coverage-report", "reports", *other])

    assert options.action == "help"
    assert options.exit_code == 1
    assert options.message.startswith(f"--coverage-report cannot be combined with {other[0]}")


def test_a_coverage_report_with_use_existing_coverage_reads_only_the_given_report(tmp_path, monkeypatch):
    for module in ("Board", "Stale"):
        _write(tmp_path, f"Demo/{module}.fs", f"module Demo.{module}\n\nlet {module.lower()} x = x + 1\n")
    _write(tmp_path, "reports/unit.xml", _one_line_cobertura(str(tmp_path / "Demo/Board.fs"), 3, 1))
    _write(
        tmp_path,
        "target/coverage/fsharp/old/me.cobertura.xml",
        _one_line_cobertura(str(tmp_path / "Demo/Stale.fs"), 3, 1),
    )
    called = []
    monkeypatch.setattr("crapper.cli.run_coverage", lambda *args, **_kwargs: called.append(args))

    code = run(
        ["--root", str(tmp_path), "--use-existing-coverage", "--coverage-report", "reports/unit.xml"]
    )

    assert code == 0
    assert called == []
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert _snapshot_coverage(tmp_path, "stale") == "0.0"


_SWIFT_BOARD = "func board(_ x: Int) -> Int {\n    return x + 1\n}\n"


def _xcode_derived_data(folder: Path, bundle: bool = True) -> Path:
    """What `xcodebuild test -enableCodeCoverage YES -derivedDataPath <folder>` leaves."""

    profile = _write(folder, "Build/ProfileData/B2F2EDFE/Coverage.profdata", "profile")
    if bundle:
        _write(folder, "Build/Products/Debug-iphonesimulator/BoardTests.xctest/BoardTests", "binary")
    return profile


def _fake_xcrun(monkeypatch, llvm_cov, xcrun: bool = True) -> None:
    """`xcrun` on the PATH, `lipo` naming one arm64 slice, and `llvm-cov` answering `llvm_cov(command)`."""

    def run(command, **_kwargs):
        if command[:2] == ["xcrun", "lipo"]:
            return subprocess.CompletedProcess(command, 0, stdout="arm64\n", stderr="")
        return llvm_cov(command)

    which = (lambda name: f"/usr/bin/{name}") if xcrun else (lambda name: None)
    monkeypatch.setattr("crapper.languages.swift_coverage.shutil.which", which)
    monkeypatch.setattr("crapper.languages.swift_coverage.subprocess.run", run)


def _files_under(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file() and ".metrics" not in path.parts)


@pytest.mark.parametrize("given", ["coverage/DerivedData", "coverage"])
def test_xcode_derived_data_converts_directly_or_one_folder_down_and_is_not_searched(
    tmp_path, monkeypatch, given
):
    board = _write(tmp_path, "Board.swift", _SWIFT_BOARD)
    other = _write(tmp_path, "Other.swift", _SWIFT_BOARD.replace("board", "other"))
    derived = tmp_path / "coverage" / "DerivedData"
    _xcode_derived_data(derived)
    # Report-like files beside the derived data and in a package checkout inside it.
    lcov = f"SF:{other}\nDA:1,1\nDA:2,1\nDA:3,1\nend_of_record\n"
    _write(tmp_path, "coverage/lcov.info", lcov)
    _write(derived, "SourcePackages/checkouts/Lib/Tests/Fixtures/lcov.info", lcov)
    llvm_cov = f"SF:{board}\nDA:1,2\nDA:2,2\nDA:3,2\nend_of_record\n"
    _fake_xcrun(monkeypatch, lambda command: subprocess.CompletedProcess(command, 0, stdout=llvm_cov, stderr=""))
    before = _files_under(tmp_path)

    code = run(["--root", str(tmp_path), "--coverage-report", given])

    assert code == 0
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert _snapshot_coverage(tmp_path, "other") == "0.0"
    assert _files_under(tmp_path) == before


def _swift_test_export_text(board: Path) -> str:
    """A `swift test --enable-code-coverage` export, its type after more than 4 KB of data, as llvm-cov orders it."""

    covered = {"filename": str(board), "segments": [[1, 29, 1, True, True, False], [3, 2, 0, False, False, False]]}
    padding = [{"filename": f"/elsewhere/Generated{index}.swift", "segments": []} for index in range(200)]
    return json.dumps(
        {"data": [{"files": [covered, *padding]}], "type": "llvm.coverage.json.export", "version": "3.0.1"}
    )


def test_a_swift_test_export_is_told_by_content_whatever_its_name_and_needs_no_xcrun(tmp_path, monkeypatch):
    board = _write(tmp_path, "Sources/Demo/Board.swift", _SWIFT_BOARD)
    text = _swift_test_export_text(board)
    assert len(text) > 4096 and "llvm.coverage.json.export" not in text[:4096]
    _write(tmp_path, "reports/any-name.data", text)
    _write(tmp_path, "reports/Package.resolved", '{"pins": [], "version": 2}')
    _fake_xcrun(monkeypatch, lambda command: pytest.fail(f"ran {command}"), xcrun=False)
    before = _files_under(tmp_path)

    code = run(["--root", str(tmp_path), "--coverage-report", "reports"])

    assert code == 0
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert _files_under(tmp_path) == before


@pytest.mark.parametrize("cause", ["no xcrun", "llvm-cov fails", "no test bundle"])
def test_xcode_derived_data_that_cannot_be_converted_stops_crapper_with_the_reason(
    tmp_path, monkeypatch, capsys, cause
):
    _write(tmp_path, "Board.swift", _SWIFT_BOARD)
    profile = _xcode_derived_data(tmp_path / "DerivedData", bundle=cause != "no test bundle")
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="malformed instrumentation profile data")
    _fake_xcrun(monkeypatch, lambda command: failed, xcrun=cause != "no xcrun")

    code = run(["--root", str(tmp_path), "--coverage-report", "DerivedData"])

    captured = capsys.readouterr()
    reason = {
        "no xcrun": "xcrun is not installed",
        "llvm-cov fails": "llvm-cov exited 1 (malformed instrumentation profile data)",
        "no test bundle": "no test bundle or app next to it",
    }[cause]
    assert code == 1
    assert f"--coverage-report {tmp_path / 'DerivedData'}: cannot convert Xcode derived data" in captured.err
    assert reason in captured.err and str(profile) in captured.err
    assert "CRAP Report" not in captured.out
    assert "Traceback" not in captured.err


def test_a_truncated_swift_test_export_alone_stops_crapper_and_in_a_folder_is_skipped_with_a_warning(
    tmp_path, monkeypatch, capsys
):
    board = _write(tmp_path, "Sources/Demo/Board.swift", _SWIFT_BOARD)
    text = _swift_test_export_text(board)
    _write(tmp_path, "reports/good.json", text)
    # A half-written export has lost the type llvm-cov writes at its end.
    _write(tmp_path, "reports/half.json", text[:300])
    _fake_xcrun(monkeypatch, lambda command: pytest.fail(f"ran {command}"), xcrun=False)

    in_folder = run(["--root", str(tmp_path), "--coverage-report", "reports"])
    in_folder_err = capsys.readouterr().err
    alone = run(["--root", str(tmp_path), "--coverage-report", "reports/half.json"])
    alone_err = capsys.readouterr()

    assert in_folder == 0
    assert f"{tmp_path / 'reports' / 'half.json'}: unreadable" in in_folder_err and "skipped" in in_folder_err
    assert _snapshot_coverage(tmp_path, "board") == "100.0"
    assert alone == 1
    assert f"--coverage-report {tmp_path / 'reports' / 'half.json'}: unreadable" in alone_err.err
    assert "skipped" not in alone_err.err
    assert "CRAP Report" not in alone_err.out
    assert "Traceback" not in in_folder_err + alone_err.err


@pytest.mark.parametrize("export_first", [False, True])
def test_swift_inputs_given_separately_merge_line_by_line_whatever_their_order(
    tmp_path, monkeypatch, export_first
):
    board = _write(tmp_path, "Board.swift", _SWIFT_BOARD)
    # Derived data covers lines 1 and 2; the export covers lines 2 and 3.
    _xcode_derived_data(tmp_path / "DerivedData")
    llvm_cov = f"SF:{board}\nDA:1,1\nDA:2,1\nDA:3,0\nend_of_record\n"
    _fake_xcrun(monkeypatch, lambda command: subprocess.CompletedProcess(command, 0, stdout=llvm_cov, stderr=""))
    segments = [[1, 29, 0, True, True, False], [2, 5, 1, True, True, False], [3, 2, 0, False, False, False]]
    export = {"data": [{"files": [{"filename": str(board), "segments": segments}]}], "type": "llvm.coverage.json.export"}
    _write(tmp_path, "swift-test.json", json.dumps(export))
    given = ["DerivedData", "swift-test.json"]
    if export_first:
        given.reverse()

    code = run(["--root", str(tmp_path), "--coverage-report", given[0], "--coverage-report", given[1]])

    assert code == 0
    assert _snapshot_coverage(tmp_path, "board") == "100.0"


_KOVER_BOARD = (
    '<?xml version="1.0" ?><report name="Kover"><package name="demo">'
    '<class name="demo/Board" sourcefilename="Board.kt">'
    '<method name="tick" desc="()I"><counter type="INSTRUCTION" missed="0" covered="2"/></method>'
    "</class></package></report>"
)
_BOARD_KT = (
    "package demo\n\nclass Board {\n    fun tick(): Int {\n        return 1\n    }\n\n"
    "    fun clear() {\n    }\n}\n"
)


def _table_row(table: str, name: str) -> str:
    return next(line for line in table.splitlines() if line.startswith(f"{name} "))


def test_a_language_no_given_report_matches_shows_na_with_one_hint(tmp_path, capsys):
    _write(tmp_path, "shared/src/commonMain/kotlin/demo/Board.kt", _BOARD_KT)
    _write(tmp_path, "iosApp/iosApp/Board.swift", _SWIFT_BOARD)
    _write(tmp_path, "iosApp/iosApp/Other.swift", _SWIFT_BOARD.replace("board", "other"))
    _write(tmp_path, "build/reports/kover/report.xml", _KOVER_BOARD)

    code = run(["--root", str(tmp_path), "--coverage-report", "build/reports/kover/report.xml"])

    captured = capsys.readouterr()
    assert code == 0
    assert _snapshot_coverage(tmp_path, "tick") == "100.0"
    assert _snapshot_coverage(tmp_path, "clear") == "0.0"
    assert _snapshot_coverage(tmp_path, "board") == "nil"
    assert _snapshot_coverage(tmp_path, "other") == "nil"
    assert re.search(r':name "board", [^}]*:crap nil', (tmp_path / ".metrics" / "crap.edn").read_text("utf-8"))
    assert _table_row(captured.out, "board").split()[-2:] == ["N/A", "N/A"]
    assert _table_row(captured.out, "clear").split()[-2] == "0.0%"
    assert captured.err.count("--coverage-report <") == 1
    assert "Swift: coverage and CRAP show N/A; no coverage report given matches its files." in captured.err
    assert "Kotlin:" not in captured.err


def test_changed_functions_score_from_a_report_path_relative_to_the_folder_crapper_runs_in(
    tmp_path, monkeypatch, capsys
):
    _repo_with_one_edited_function(tmp_path)
    _write(tmp_path, "reports/lcov.info", "SF:app.py\nDA:2,1\nDA:3,1\nDA:4,0\nend_of_record\n")
    monkeypatch.chdir(tmp_path)

    code = run(["--changed-functions", "--coverage-report", "reports/lcov.info"])

    table = capsys.readouterr().out
    assert code == 0
    assert _table_row(table, "edited").split()[-2] == "66.7%"
    assert "untouched" not in table


def test_a_given_report_that_matches_no_scored_file_warns_naming_it_and_the_table_prints(
    tmp_path, capsys
):
    _write(tmp_path, "project/Demo/Board.fs", "module Demo.Board\n\nlet board x = x + 1\n")
    _write(
        tmp_path,
        "project/reports/other.xml",
        _one_line_cobertura(str(tmp_path / "checkout/Demo/Board.fs"), 3, 1),
    )

    code = run(["--root", str(tmp_path / "project"), "--coverage-report", "reports/other.xml"])

    captured = capsys.readouterr()
    assert code == 0
    assert "CRAP Report" in captured.out
    assert _table_row(captured.out, "board").split()[-2:] == ["N/A", "N/A"]
    assert (
        f"--coverage-report {tmp_path / 'project/reports/other.xml'}: matches none of the files being scored"
        in captured.err
    )
    assert "F#: coverage and CRAP show N/A; no coverage report given matches its files." in captured.err


def test_a_given_report_does_not_warn_when_the_scored_files_hold_no_function(tmp_path, capsys):
    _write(tmp_path, "project/Demo/Empty.fs", "module Demo.Empty\n")
    _write(
        tmp_path,
        "project/reports/other.xml",
        _one_line_cobertura(str(tmp_path / "checkout/Demo/Board.fs"), 3, 1),
    )

    code = run(["--root", str(tmp_path / "project"), "--coverage-report", "reports/other.xml"])

    assert code == 0
    assert "matches none of the files being scored" not in capsys.readouterr().err


def _cobertura_branch_line(filename: str, condition: str) -> str:
    return (
        f'<?xml version="1.0"?><coverage><packages><package name="Demo"><classes>'
        f'<class name="Demo" filename="{filename}"><methods/><lines>'
        f'<line number="3" hits="2" branch="True" {condition}/>'
        f'<line number="4" hits="1" branch="True" {condition}/>'
        f'<line number="5" hits="0" branch="True" />'
        "</lines></class></classes></package></packages></coverage>"
    )


def test_a_cobertura_report_with_branch_lines_that_ran_and_no_condition_coverage_warns_once(
    tmp_path, capsys
):
    board = tmp_path / "Demo/Board.fs"
    _write(tmp_path, "Demo/Board.fs", "module Demo.Board\n\nlet board x =\n    if x then 1\n    else 0\n")
    _write(tmp_path, "merged/merged.xml", _cobertura_branch_line(str(board), ""))
    _write(tmp_path, "split/one.xml", _cobertura_branch_line(str(board), 'condition-coverage="50% (1/2)"'))

    merged = run(
        ["--root", str(tmp_path), "--coverage-report", "merged", "--coverage-report", "merged/merged.xml"]
    )
    merged_err = capsys.readouterr().err
    split = run(["--root", str(tmp_path), "--coverage-report", "split"])
    split_err = capsys.readouterr().err

    assert merged == 0 and split == 0
    assert merged_err.count("VSTEST_DISABLE_ARTIFACTS_POSTPROCESSING=1") == 1
    assert f"{tmp_path / 'merged/merged.xml'}:" in merged_err
    assert "VSTEST_DISABLE_ARTIFACTS_POSTPROCESSING" not in split_err


_FSHARP_BOARD = "module Demo.Board\n\nlet board x = x + 1\n"


@pytest.mark.parametrize(
    ("mode", "hints"),
    [([], 3), (["--use-existing-coverage"], 3), (["--coverage-command", "make coverage"], 3), (["--no-coverage"], 0)],
)
def test_without_a_report_path_fsharp_kotlin_and_swift_show_na_whatever_reports_lie_on_disk(
    tmp_path, monkeypatch, capsys, mode, hints
):
    board = _write(tmp_path, "Demo/Board.fs", _FSHARP_BOARD)
    _write(tmp_path, "shared/src/commonMain/kotlin/demo/Board.kt", _BOARD_KT)
    place = _write(tmp_path, "Feature/Sources/Feature/Place.swift", "func place() -> Int { 1 }\n")
    swift = "target/coverage/swift"
    reports = [
        _write(tmp_path, "target/coverage/lcov.info", f"SF:{board}\nDA:3,1\nend_of_record\n"),
        _write(tmp_path, "target/coverage/fsharp/Demo.Tests/r.cobertura.xml", _one_line_cobertura(str(board), 3, 1)),
        _write(tmp_path, "build/reports/kover/report.xml", _KOVER_BOARD),
        _write(tmp_path, "target/site/jacoco/jacoco.xml", _KOVER_BOARD),
        _write(tmp_path, f"{swift}/lcov.info", f"SF:{place}\nDA:1,1\nend_of_record\n"),
        _write(tmp_path, f"{swift}/DerivedData/Build/ProfileData/SIM/Coverage.profdata", "profile"),
        _write(tmp_path, f"{swift}/DerivedData/Build/Products/Debug-iphonesimulator/Shop.app/Shop", "binary"),
        _write(tmp_path, f"{swift}/Place.json", '{"type": "llvm.coverage.json.export", "data": []}'),
    ]
    started = []
    monkeypatch.setattr("crapper.cli.run_coverage", runners.run_coverage)
    monkeypatch.setattr("crapper.runners.run_shell", lambda command, cwd: started.append(command) or 0)
    monkeypatch.setattr(
        "crapper.languages.swift_coverage.subprocess.run", lambda command, **_kw: started.append(command)
    )

    code = run(["--root", str(tmp_path), *mode])

    captured = capsys.readouterr()
    assert code == 0
    assert started == (mode[1:] if mode[0:1] == ["--coverage-command"] else [])
    for name in ("board", "tick", "clear", "place"):
        assert _snapshot_coverage(tmp_path, name) == "nil"
        assert _table_row(captured.out, name).split()[-2:] == ["N/A", "N/A"]
    assert captured.err.count("--coverage-report <") == hints
    assert captured.err.count(
        "F#: coverage and CRAP show N/A; crapper reads F# coverage only from --coverage-report. "
        "Pass --coverage-report <the Cobertura reports of dotnet test>."
    ) == (hints > 0)
    assert captured.err.count(
        "Kotlin: coverage and CRAP show N/A; crapper reads Kotlin coverage only from --coverage-report. "
        "Pass --coverage-report <Kover's XML report>."
    ) == (hints > 0)
    assert captured.err.count(
        "Swift: coverage and CRAP show N/A; crapper reads Swift coverage only from --coverage-report. "
        "Pass --coverage-report <Xcode derived data or a swift test export>."
    ) == (hints > 0)
    assert all(report.is_file() for report in reports)


def _fake_python_coverage(root: Path, started: list):
    """Stand in for coverage.py: `coverage run` leaves its data file, `coverage lcov` an LCOV report of app.py."""

    def shell(command, cwd):
        started.append(command)
        if command[2:4] == ["coverage", "run"]:
            Path(command[4].split("=", 1)[1]).write_text("data", encoding="utf-8")
        if command[2:4] == ["coverage", "lcov"]:
            report = Path(command[command.index("-o") + 1])
            report.write_text(f"SF:{root / 'app.py'}\nDA:2,1\nend_of_record\n", encoding="utf-8")
        return 0

    return shell


def test_beside_fsharp_pythons_tests_still_run_and_score_while_fsharp_shows_na(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'demo'\n")
    _write(tmp_path, "app.py", "def edited(x):\n    return x\n")
    _write(tmp_path, "Demo/Board.fs", _FSHARP_BOARD)
    started = []
    monkeypatch.setattr("crapper.cli.run_coverage", runners.run_coverage)
    monkeypatch.setattr("crapper.runners.run_shell", _fake_python_coverage(tmp_path.resolve(), started))

    code = run(["--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 0
    assert any(command[2:4] == ["coverage", "run"] for command in started)
    assert not any("dotnet" in str(command) for command in started)
    assert _snapshot_coverage(tmp_path, "edited") == "100.0"
    assert _snapshot_coverage(tmp_path, "board") == "nil"
    assert captured.err.count("--coverage-report <") == 1
    assert "F#: coverage and CRAP show N/A; crapper reads F# coverage only from --coverage-report." in captured.err
