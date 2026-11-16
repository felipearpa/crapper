"""Load Cloverage, LCOV, Cobertura, JaCoCo, Kover, and Go coverage profiles.

Clojure prefers Cloverage's per-line form counts, then LCOV. Java uses JaCoCo
instruction counters, and Kotlin the same counters from Kover's JaCoCo-shaped
report. Go uses statement profiles. Cobertura reports join the LCOV index;
they and Kover's report are read only from paths given to `load_reports`.
LCOV scores a function by its BRDA branch records when it has any, and by
line hits otherwise. Swift's LCOV is converted in memory from Xcode profile
data or `swift test` exports given to `load_reports`, by
`crapper.languages.swift_coverage`. F#, Kotlin, and Swift are scored from
given paths only (`GIVEN_ONLY`).
`percent_for` returns None when the file is absent; analysis turns that into 0%.
"""

from __future__ import annotations

import functools
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote

from crapper.languages import swift_coverage
from crapper.model import Function
from crapper.runners import nearest_marker

_SPAN = re.compile(
    r'<span[^>]*title="(\d+) out of (\d+) forms covered"[^>]*>\s*(\d+)&nbsp;'
)
_LCOV_DA = re.compile(r"DA:(\d+),(\d+)")
_LCOV_BRDA = re.compile(r"BRDA:(\d+),([^,]*),([^,]*),(-|\d+)")
_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
_CONDITIONS = re.compile(r"\((\d+)/(\d+)\)")


class FileCoverage(dict):
    """Line hits for one file, plus branch hits keyed by line.

    The mapping itself is `line → (covered, total)` from `DA` records, so
    existing callers can keep indexing it. `branches` aggregates each `BRDA`
    record on that line as one branch.
    """

    def __init__(self):
        super().__init__()
        self.branches: dict[int, tuple[int, int]] = {}


def normalize_path(path: str) -> str:
    text = unquote(path).replace("\\", "/")
    if text.startswith("file:"):
        text = text[5:]
    while text.startswith("./"):
        text = text[2:]
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _segments(path: str) -> list[str]:
    return [part for part in normalize_path(path).split("/") if part]


def suffix_match(path: str, suffix: str) -> bool:
    path_parts = _segments(path)
    suffix_parts = _segments(suffix)
    if not suffix_parts or len(suffix_parts) > len(path_parts):
        return False
    return path_parts[-len(suffix_parts) :] == suffix_parts


def _key_items(keys: list[str]) -> list[tuple[str, str]]:
    return [(key, normalize_path(key)) for key in keys]


def _exact_key(items: list[tuple[str, str]], candidates: list[str]) -> str | None:
    for candidate in candidates:
        for original, norm in items:
            if norm == candidate:
                return original
    return None


def _nonempty_parts(candidates: list[str]) -> list[tuple[str, list[str]]]:
    found = []
    for candidate in candidates:
        parts = _segments(candidate)
        if parts:
            found.append((candidate, parts))
    return found


def _key_has_suffix(key_parts: list[str], parts: list[str]) -> bool:
    if len(key_parts) < len(parts):
        return False
    return key_parts[-len(parts) :] == parts


def _forward_matches(
    items: list[tuple[str, str]],
    parts_of: list[tuple[str, list[str]]],
    source_parts: list[tuple[str, ...]],
) -> list[str]:
    forward: list[tuple[int, str]] = []
    for _candidate, parts in parts_of:
        for original, norm in items:
            key_parts = _segments(norm)
            if not _key_has_suffix(key_parts, parts):
                continue
            if _longer_source_owns(key_parts, len(parts), source_parts):
                continue
            forward.append((len(key_parts) - len(parts), original))
    if not forward:
        return []
    smallest = min(extra for extra, _key in forward)
    return list(dict.fromkeys(key for extra, key in forward if extra == smallest))


def _key_is_proper_suffix(parts: list[str], key_parts: list[str]) -> bool:
    if not key_parts or len(key_parts) >= len(parts):
        return False
    return parts[-len(key_parts) :] == key_parts


def _reverse_key(
    items: list[tuple[str, str]],
    parts_of: list[tuple[str, list[str]]],
    source_parts: list[tuple[str, ...]],
) -> str | None:
    own = {tuple(parts) for _candidate, parts in parts_of}
    reverse: list[str] = []
    for _candidate, parts in parts_of:
        for original, norm in items:
            key_parts = _segments(norm)
            if not _key_is_proper_suffix(parts, key_parts):
                continue
            if _another_source_ends_with(key_parts, source_parts, own):
                continue
            reverse.append(original)
    unique = list(dict.fromkeys(reverse))
    if len(unique) == 1:
        return unique[0]
    return None


def _select_key(
    keys: list[str], source_path: str, source_parts: list[tuple[str, ...]]
) -> str | None:
    """Report key for one source file.

    An exact path wins. A report path may be the source path plus a prefix
    (`proj/src/a.go` for `src/a.go`). It is not a match when another source
    file is a longer suffix of that key, so `b/main.go` does not take
    `a/b/main.go`.
    """

    items = _key_items(keys)
    candidates = _candidates(source_path)
    exact = _exact_key(items, candidates)
    if exact is not None:
        return exact
    parts_of = _nonempty_parts(candidates)
    forward = _forward_matches(items, parts_of, source_parts)
    if len(forward) == 1:
        return forward[0]
    if forward:
        return None
    return _reverse_key(items, parts_of, source_parts)


def _longer_source_owns(
    key_parts: list[str], source_len: int, source_parts: list[tuple[str, ...]]
) -> bool:
    for other in source_parts:
        if len(other) <= source_len:
            continue
        if len(other) <= len(key_parts) and key_parts[-len(other) :] == list(other):
            return True
    return False


def _another_source_ends_with(
    key_parts: list[str],
    source_parts: list[tuple[str, ...]],
    own: set[tuple[str, ...]],
) -> bool:
    for other in source_parts:
        if other in own or len(other) < len(key_parts):
            continue
        if list(other[-len(key_parts) :]) == key_parts:
            return True
    return False


def _candidates(source_path: str) -> list[str]:
    relative = normalize_path(source_path)
    absolute = normalize_path(str(Path(source_path).resolve())) if source_path else relative
    no_src = re.sub(r"^src/", "", relative)
    absolute_no_src = re.sub(r"/src/", "/", absolute)
    values = [relative, absolute, no_src, absolute_no_src]
    found: list[str] = []
    for value in values:
        if value and value not in found:
            found.append(value)
    return found


def coverage_percent(covered: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return 100.0 * covered / total


def percent_for_range(lines: dict[int, tuple[int, int]] | None, start: int, end: int) -> float | None:
    if lines is None:
        return None
    covered = 0
    total = 0
    for line in range(start, end + 1):
        entry = lines.get(line)
        if entry is None:
            continue
        covered += entry[0]
        total += entry[1]
    if total == 0:
        return 0.0
    return coverage_percent(covered, total)


def parse_form_coverage(html: str) -> dict[int, tuple[int, int]]:
    found: dict[int, tuple[int, int]] = {}
    for covered, total, line in _SPAN.findall(html):
        total_forms = int(total)
        if total_forms <= 0:
            continue
        found[int(line)] = (int(covered), total_forms)
    return found


def _branch_hit(taken: str) -> bool:
    if taken == "-":
        return False
    try:
        return int(taken) > 0
    except ValueError:
        return False


def _add_branch(record: FileCoverage, line: int, taken: str) -> None:
    covered, total = record.branches.get(line, (0, 0))
    record.branches[line] = (covered + (1 if _branch_hit(taken) else 0), total + 1)


def _store_lcov(out: dict[str, FileCoverage], current_file: str | None, current: FileCoverage) -> None:
    if current_file is not None:
        out[current_file] = current


def _read_lcov_line(current: FileCoverage, line: str) -> None:
    match = _LCOV_DA.match(line)
    if match:
        hits = int(match.group(2))
        current[int(match.group(1))] = (1 if hits > 0 else 0, 1)
        return
    branch = _LCOV_BRDA.match(line)
    if branch:
        _add_branch(current, int(branch.group(1)), branch.group(4))


def parse_lcov(text: str) -> dict[str, FileCoverage]:
    out: dict[str, FileCoverage] = {}
    current_file = None
    current = FileCoverage()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            _store_lcov(out, current_file, current)
            current_file = line[3:]
            current = FileCoverage()
        elif line == "end_of_record":
            _store_lcov(out, current_file, current)
            current_file = None
            current = FileCoverage()
        elif current_file is not None:
            _read_lcov_line(current, line)
    _store_lcov(out, current_file, current)
    return out


def branch_percent(record, start: int, end: int) -> float | None:
    """Branch coverage for a function, or None when that span has no branches."""

    branches = getattr(record, "branches", None)
    if not branches:
        return None
    covered = 0
    total = 0
    for line in range(start, end + 1):
        entry = branches.get(line)
        if entry is None:
            continue
        covered += entry[0]
        total += entry[1]
    if total == 0:
        return None
    return coverage_percent(covered, total)


def parse_go_profile(text: str) -> dict[str, list[tuple[int, int, int, int]]]:
    """Map file → (start_line, end_line, statements, hits)."""

    out: dict[str, list[tuple[int, int, int, int]]] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("mode:"):
            continue
        fields = line.split()
        if len(fields) != 3 or ":" not in fields[0]:
            raise ValueError(f"invalid coverage segment on line {line_number}: {raw!r}")
        file_part, range_part = fields[0].split(":", 1)
        start_text, end_text = range_part.split(",", 1)
        start_line = int(start_text.split(".", 1)[0])
        end_line = int(end_text.split(".", 1)[0])
        statements = int(fields[1])
        hits = int(fields[2])
        out.setdefault(file_part, []).append((start_line, end_line, statements, hits))
    return out


def _profile_segments(profile, path: str, source_parts: list[tuple[str, ...]] | None = None):
    if profile is None:
        return None
    key = _select_key(list(profile), path, source_parts or [])
    if key is None:
        return None
    return profile[key]


_GO_MODULE = re.compile(r'^\s*module\s+"?([^"\s]+)"?', re.MULTILINE)


def go_import_name(file: Path, stop: Path) -> str | None:
    """The name `go test -coverprofile` gives a file: its module path plus its path in the module."""

    module_dir = nearest_marker(file, "go.mod", stop)
    if module_dir is None:
        return None
    found = _GO_MODULE.search(_read(module_dir / "go.mod") or "")
    if found is None:
        return None
    return f"{found.group(1)}/{file.relative_to(module_dir).as_posix()}"


def go_percent(
    profile: dict[str, list[tuple[int, int, int, int]]] | None,
    path: str,
    start: int,
    end: int,
    source_parts: list[tuple[str, ...]] | None = None,
) -> float | None:
    return _segments_percent(_profile_segments(profile, path, source_parts), start, end)


def _segments_percent(segments, start: int, end: int) -> float | None:
    if segments is None:
        return None
    total = 0
    covered = 0
    for seg_start, seg_end, statements, hits in segments:
        if seg_end < start or seg_start > end:
            continue
        total += statements
        if hits > 0:
            covered += statements
    if total == 0:
        return 0.0
    return coverage_percent(covered, total)


def _cobertura_key(filename: str, root: Path) -> str | None:
    """Report path relative to `root`, so every report spells one file alike.

    An absolute path outside the project, such as FSharp.Core's build paths,
    is dropped, and so is a Windows path with a drive. A relative path is kept
    as written.
    """

    if PureWindowsPath(filename).drive:
        return None
    path = Path(normalize_path(filename))
    if not path.is_absolute():
        return path.as_posix()
    for candidate in (path, path.resolve()):
        try:
            return candidate.relative_to(root).as_posix()
        except ValueError:
            continue
    return None


def _condition_coverage(line) -> tuple[int, int] | None:
    if line.get("branch", "").lower() != "true":
        return None
    found = _CONDITIONS.search(line.get("condition-coverage") or "")
    if found is None:
        return None
    return int(found.group(1)), int(found.group(2))


def _merge_line(
    record: FileCoverage, number: int, covered: int, branch: tuple[int, int] | None
) -> None:
    """A line is covered when any report covered it; its branches keep the best counts."""

    record[number] = (max(record.get(number, (0, 1))[0], covered), 1)
    if branch is None:
        return
    current = record.branches.get(number, (0, 0))
    record.branches[number] = (max(current[0], branch[0]), max(current[1], branch[1]))


def read_cobertura(text: str, root: Path) -> tuple[dict[str, FileCoverage], bool]:
    """Files keyed by their path relative to `root`, classes that name one file
    merged, and whether a branch line that ran has no `condition-coverage`.

    A solution-wide `dotnet test` merges its test projects' reports and drops
    that value on lines more than one project reached.
    """

    found: dict[str, FileCoverage] = {}
    lacking = False
    for class_node in ET.fromstring(text.lstrip("\ufeff")).iter("class"):
        key = _cobertura_key(class_node.get("filename", ""), root)
        if key is None:
            continue
        record = found.setdefault(key, FileCoverage())
        for line in class_node.iter("line"):
            number = int(line.get("number") or "0")
            covered = 1 if int(line.get("hits") or "0") > 0 else 0
            branch = _condition_coverage(line)
            if branch is None and covered and line.get("branch", "").lower() == "true":
                lacking = True
            _merge_line(record, number, covered, branch)
    return found, lacking


@dataclass
class JacocoMethod:
    missed: int
    covered: int
    line: int

    @property
    def percent(self) -> float:
        return coverage_percent(self.covered, self.missed + self.covered)


def _jacoco_from_index(classes, class_names, method_name, line) -> float | None:
    pool: list[JacocoMethod] = []
    for name in class_names:
        for method in classes.get(f"{name}#{method_name}", []):
            pool.append(method)
    if not pool:
        return None
    for method in pool:
        if method.line == line:
            return method.percent
    nearest = min(pool, key=lambda method: abs(method.line - line))
    return nearest.percent


def _kotlin_from_index(
    classes: dict[str, list[tuple[str, JacocoMethod]]], class_name: str, name: str
) -> float | None:
    """Sum the methods Kotlin compiles one function into.

    `name-<hash>` is a value-class mangling; `name$<anything>` is a lambda, an
    `internal` module suffix, `$suspendImpl`, or a default-argument bridge.
    Kover writes no line numbers, so overloads share one percentage.
    """

    missed = 0
    covered = 0
    found = False
    for method_name, method in classes.get(class_name, []):
        if method_name == name or method_name.startswith((f"{name}-", f"{name}$")):
            found = True
            missed += method.missed
            covered += method.covered
    if not found:
        return None
    return coverage_percent(covered, missed + covered)


def _by_class(index: dict[str, list[JacocoMethod]]) -> dict[str, list[tuple[str, JacocoMethod]]]:
    grouped: dict[str, list[tuple[str, JacocoMethod]]] = {}
    for key, methods in index.items():
        class_name, method_name = key.split("#", 1)
        grouped.setdefault(class_name, []).extend((method_name, method) for method in methods)
    return grouped


def _instruction_counter(method):
    for child in list(method):
        if child.tag == "counter" and child.get("type") == "INSTRUCTION":
            return child
    return None


def _method_line(method) -> int:
    try:
        return int(method.get("line") or "0")
    except ValueError:
        return 0


def _record_jacoco_method(found: dict[str, list[JacocoMethod]], class_name: str, method) -> None:
    if method.tag != "method":
        return
    counter = _instruction_counter(method)
    if counter is None:
        return
    key = f"{class_name}#{method.get('name', '')}"
    found.setdefault(key, []).append(
        JacocoMethod(
            missed=int(counter.get("missed") or "0"),
            covered=int(counter.get("covered") or "0"),
            line=_method_line(method),
        )
    )


def parse_jacoco_index(text: str) -> dict[str, list[JacocoMethod]]:
    """Key methods as `binary.class.name#method`."""

    cleaned = _DOCTYPE.sub("", text)
    root = ET.fromstring(cleaned)
    found: dict[str, list[JacocoMethod]] = {}
    for class_node in root.iter("class"):
        class_name = class_node.get("name", "").replace("/", ".")
        for method in list(class_node):
            _record_jacoco_method(found, class_name, method)
    return found


# Languages that get coverage only from paths given with `--coverage-report`.
GIVEN_ONLY = frozenset({"fsharp", "kotlin", "swift"})


@dataclass
class CoverageBundle:
    lcov: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)
    go_profile: dict[str, list[tuple[int, int, int, int]]] | None = None
    jacoco: dict[str, list[JacocoMethod]] | None = None
    form_html: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)
    source_parts: list[tuple[str, ...]] = field(default_factory=list)
    root: Path | None = None
    # Each path given with `--coverage-report` and the reports it holds; None
    # when coverage came from crapper's own locations.
    given: list[tuple[Path, CoverageBundle]] | None = None

    def bind_sources(self, paths: list[str]) -> None:
        """Remember project files so a short path cannot take a longer file's report."""

        found: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for source in paths:
            for candidate in _candidates(source):
                parts = tuple(_segments(candidate))
                if parts and parts not in seen:
                    seen.add(parts)
                    found.append(parts)
        self.source_parts = found
        for _, reports in self.given or []:
            reports.source_parts = found

    def percent_for(self, function: Function) -> float | None:
        if function.language == "kotlin" and function.jacoco_class:
            return _kotlin_from_index(self._jacoco_by_class, function.jacoco_class, function.name)
        if function.language == "java":
            names = []
            if function.jacoco_class:
                names.append(function.jacoco_class)
            if function.namespace not in names:
                names.append(function.namespace)
            return _jacoco_from_index(
                self.jacoco or {}, names, function.name, function.start_line
            )
        if function.language == "go":
            segments = self._go_segments(function.path)
            return _segments_percent(segments, function.start_line, function.end_line)
        html = self._html_lines(function.path)
        if html is not None:
            return percent_for_range(html, function.start_line, function.end_line)
        record = self._lcov_lines(function.path)
        if record is None:
            return None
        branched = branch_percent(record, function.start_line, function.end_line)
        if branched is not None:
            return branched
        return percent_for_range(record, function.start_line, function.end_line)

    def knows(self, function: Function) -> bool:
        """Whether a report holds the function's file, or for Java and Kotlin one of its classes."""

        if function.language == "kotlin" and function.jacoco_class:
            return function.jacoco_class in self._jacoco_by_class
        if function.language == "java":
            return any(name in self._jacoco_by_class for name in (function.jacoco_class, function.namespace))
        if function.language == "go":
            return self._go_segments(function.path) is not None
        return self._html_lines(function.path) is not None or self._lcov_lines(function.path) is not None

    def given_languages(self, functions: list[Function]) -> set[str]:
        """Languages whose functions are looked up in the reports.

        With paths given, those with a scored file in the given reports; with
        none, every language of `functions` but the ones in `GIVEN_ONLY`.

        Warns, naming it, for each given path whose reports hold none of the
        scored files, when there are functions to score.
        """

        if self.given is None:
            return {function.language for function in functions} - GIVEN_ONLY
        for path, reports in self.given if functions else []:
            if not any(reports.knows(function) for function in functions):
                print(
                    f"--coverage-report {path}: matches none of the files being scored; "
                    "was it made in another checkout or for another project?",
                    file=sys.stderr,
                )
        found: set[str] = set()
        for function in functions:
            if function.language not in found and self.knows(function):
                found.add(function.language)
        return found

    @functools.cached_property
    def _jacoco_by_class(self) -> dict[str, list[tuple[str, JacocoMethod]]]:
        return _by_class(self.jacoco or {})

    def _go_segments(self, path: str) -> list[tuple[int, int, int, int]] | None:
        """Profile segments for one Go file: by import path, else by the longest path suffix."""

        if self.go_profile is None:
            return None
        if self.root is not None:
            name = go_import_name((self.root / path).resolve(), self.root)
            if name in self.go_profile:
                return self.go_profile[name]
        return _profile_segments(self.go_profile, path, self.source_parts)

    def _html_lines(self, path: str) -> dict[int, tuple[int, int]] | None:
        return _lookup(self.form_html, path, self.source_parts)

    def _lcov_lines(self, path: str) -> dict[int, tuple[int, int]] | None:
        return _lookup(self.lcov, path, self.source_parts)


def _lookup(index: dict[str, dict], source_path: str, source_parts: list[tuple[str, ...]]):
    if not index:
        return None
    key = _select_key(list(index), source_path, source_parts)
    if key is None:
        return None
    return index[key]


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _lcov_paths(root: Path) -> list[Path]:
    paths = list(root.glob("target/coverage/**/lcov.info"))
    paths.extend(root.glob("coverage/**/lcov.info"))
    return paths


def _merge_texts(paths: list[Path]) -> list[str]:
    texts: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        text = _read(path)
        if text:
            texts.append(text)
    return texts


def _merge_lcov(root: Path) -> dict[str, dict[int, tuple[int, int]]]:
    merged: dict[str, dict[int, tuple[int, int]]] = {}
    for text in _merge_texts(_lcov_paths(root)):
        merged.update(parse_lcov(text))
    return merged


def _merge_go(root: Path) -> dict[str, list[tuple[int, int, int, int]]]:
    paths = [
        root / "target" / "coverage" / "coverage.out",
        root / "target" / "coverage" / "go" / "coverage.out",
        root / "coverage.out",
    ]
    paths.extend(root.glob("*/target/coverage/go/coverage.out"))
    paths.extend(root.glob("*/*/target/coverage/go/coverage.out"))
    merged: dict[str, list[tuple[int, int, int, int]]] = {}
    for text in _merge_texts(paths):
        for key, segments in parse_go_profile(text).items():
            merged.setdefault(key, []).extend(segments)
    return merged


def _merge_jacoco(root: Path) -> dict[str, list[JacocoMethod]]:
    paths = [root / "target" / "site" / "jacoco" / "jacoco.xml"]
    paths.extend(root.glob("*/target/site/jacoco/jacoco.xml"))
    paths.extend(root.glob("*/*/target/site/jacoco/jacoco.xml"))
    merged: dict[str, list[JacocoMethod]] = {}
    for text in _merge_texts(paths):
        for key, methods in parse_jacoco_index(text).items():
            merged.setdefault(key, []).extend(methods)
    return merged


def _add_cobertura(merged: dict[str, FileCoverage], parsed: dict[str, FileCoverage]) -> None:
    for key, record in parsed.items():
        into = merged.setdefault(key, FileCoverage())
        for number, (covered, _total) in record.items():
            _merge_line(into, number, covered, record.branches.get(number))


def _merge_forms(root: Path) -> dict[str, dict[int, tuple[int, int]]]:
    coverage_dir = root / "target" / "coverage"
    found: dict[str, dict[int, tuple[int, int]]] = {}
    if not coverage_dir.is_dir():
        return found
    for html_path in coverage_dir.rglob("*.html"):
        text = _read(html_path)
        if not text or "forms covered" not in text:
            continue
        relative = html_path.relative_to(coverage_dir).as_posix()
        relative = relative[: -len(".html")]
        found[relative] = parse_form_coverage(text)
    return found


def load_bundle(root: Path) -> CoverageBundle:
    """Coverage from crapper's own locations, which hold no F# or Kotlin report."""

    go_profile = _merge_go(root)
    jacoco = _merge_jacoco(root)
    lcov = _merge_lcov(root)
    return CoverageBundle(
        lcov=lcov,
        go_profile=go_profile or None,
        jacoco=jacoco or None,
        form_html=_merge_forms(root),
        root=root,
    )


class ReportPathError(Exception):
    """A path given with `--coverage-report` holds no report crapper reads."""


_XML_PROLOG = re.compile(r"\s*(?:<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>]*>|\s)*<([\w.:-]+)", re.DOTALL)
_LCOV_SOURCE = re.compile(r"^\s*SF:", re.MULTILINE)
_HEAD_BYTES = 4096
_SWIFT_EXPORT = re.compile(r'"type"\s*:\s*"llvm\.coverage\.json\.export"')
_SWIFT_EXPORT_HEAD = re.compile(r'\s*\{\s*"data"\s*:\s*\[\s*\{\s*"files"\s*:\s*\[')
_XML_ROOTS = {"coverage": "cobertura", "report": "jacoco"}
_LABELS = {
    "cobertura": "Cobertura",
    "jacoco": "JaCoCo or Kover",
    "lcov": "LCOV",
    "go": "Go",
    "swift": "swift test coverage",
}
_KNOWN = "Xcode derived data, Cobertura, JaCoCo or Kover XML, LCOV, Go profile, swift test export"


def _tail(path: Path) -> str:
    """The last bytes of a file: a `swift test` export names its type at its end."""

    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - _HEAD_BYTES))
        return handle.read().decode("utf-8", errors="replace")


def _report_format(path: Path) -> str | None:
    """The report format of a file, told from its first bytes, or a JSON file's last, not its name."""

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(_HEAD_BYTES).lstrip("\ufeff")
        tail = _tail(path) if head.lstrip().startswith("{") else ""
    except OSError:
        return None
    root_tag = _XML_PROLOG.match(head)
    if root_tag is not None:
        return _XML_ROOTS.get(root_tag.group(1))
    if _LCOV_SOURCE.search(head):
        return "lcov"
    if head.lstrip().startswith("mode:"):
        return "go"
    if _SWIFT_EXPORT_HEAD.match(head) or (tail and _SWIFT_EXPORT.search(head + tail)):
        return "swift"
    return None


def _given_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(found for found in path.rglob("*") if found.is_file())
    return [path]


def _parse_report(kind: str, path: Path, root: Path, files: list[Path]):
    if kind == "swift":
        return swift_coverage.read_export(path, files)
    text = path.read_text(encoding="utf-8", errors="replace")
    if kind == "cobertura":
        parsed, lacking = read_cobertura(text, root)
        if lacking:
            print(
                f"{path}: Cobertura report with branch lines that ran and carry no condition-coverage, "
                "as a solution-wide dotnet test leaves when it merges its test projects' reports; "
                "their functions may score wrongly. Run dotnet test with "
                "VSTEST_DISABLE_ARTIFACTS_POSTPROCESSING=1 to keep one report per test project.",
                file=sys.stderr,
            )
        return parsed
    if kind == "jacoco":
        return parse_jacoco_index(text)
    if kind == "lcov":
        return parse_lcov(text)
    return parse_go_profile(text)


def _read_derived_data(path: Path, read: dict[Path, tuple[str, object] | str], files: list[Path]) -> None:
    """Convert Xcode derived data without searching it: its package checkouts hold report-like fixtures."""

    real = path.resolve()
    if real in read:
        return
    try:
        read[real] = ("swift", swift_coverage.read_derived_data(path, files))
    except swift_coverage.ConversionError as exc:
        raise ReportPathError(f"--coverage-report {path}: cannot convert Xcode derived data: {exc}.") from exc


def _read_given(
    path: Path, root: Path, read: dict[Path, tuple[str, object] | str], files: list[Path]
) -> list[Path]:
    """Read the reports at one given path into `read`, keyed by real path, each once.

    A damaged report is kept as its message. Named directly it stops crapper,
    also when a folder reached it first; inside a folder it is skipped with a
    warning. Returns the real paths of the reports the path holds that could
    be read.
    """

    if swift_coverage.is_derived_data(path):
        _read_derived_data(path, read, files)
        return [path.resolve()]
    reached: list[Path] = []
    for file in _given_files(path):
        real = file.resolve()
        if real not in read:
            kind = _report_format(file)
            if kind is None:
                continue
            try:
                read[real] = (kind, _parse_report(kind, file, root, files))
            except (ET.ParseError, OSError, ValueError) as exc:
                read[real] = f"{file}: unreadable {_LABELS[kind]} report ({exc})"
                if file != path:
                    print(f"{read[real]}; skipped.", file=sys.stderr)
        if isinstance(read[real], str) and file == path:
            raise ReportPathError(f"--coverage-report {read[real]}.")
        if not isinstance(read[real], str):
            reached.append(real)
    return reached


def _given_reports(
    root: Path, given: list[str], files: list[Path]
) -> tuple[dict[Path, tuple[str, object]], list[tuple[Path, list[Path]]]]:
    """Parsed reports by real path, in the order they were reached, and each given path
    with the real paths of its reports. A relative path is read inside `root`."""

    read: dict[Path, tuple[str, object] | str] = {}
    reached: list[tuple[Path, list[Path]]] = []
    for raw in given:
        path = Path(raw) if Path(raw).is_absolute() else root / raw
        if not path.exists():
            raise ReportPathError(f"--coverage-report {path}: no such file or folder.")
        found = _read_given(path, root.resolve(), read, files)
        if not found:
            raise ReportPathError(
                f"--coverage-report {path}: holds no coverage report crapper reads ({_KNOWN})."
            )
        reached.append((path, found))
    reports = {real: report for real, report in read.items() if not isinstance(report, str)}
    return reports, reached


def load_reports(root: Path, given: list[str], files: list[Path]) -> CoverageBundle:
    """Coverage from the given report paths only, never from crapper's own locations.

    The bundle also keeps each given path with its own reports, so a path
    that matches no scored file can be named.

    Xcode derived data and `swift test` exports are converted in memory for the
    Swift files among `files` and added together line by line, whatever their
    order, as the conversion after a coverage command adds them; the sum is read
    as one LCOV report.

    Reports combine Cobertura line by line, the last LCOV record of a file wins,
    JaCoCo methods and Go segments add up, and a Cobertura record of a file wins
    over an LCOV record of it.
    """

    reports, reached = _given_reports(root, given, files)
    bundle = _combine(list(reports.values()), root)
    bundle.given = [(path, _combine([reports[real] for real in found], root)) for path, found in reached]
    return bundle


def _combine(reports: list[tuple[str, object]], root: Path) -> CoverageBundle:
    lcov: dict[str, FileCoverage] = {}
    cobertura: dict[str, FileCoverage] = {}
    jacoco: dict[str, list[JacocoMethod]] = {}
    go_profile: dict[str, list[tuple[int, int, int, int]]] = {}
    swift: dict[str, dict[int, int]] = {}
    for kind, parsed in reports:
        if kind == "cobertura":
            _add_cobertura(cobertura, parsed)
        elif kind == "swift":
            swift_coverage.add_counts(swift, parsed)
        elif kind == "lcov":
            lcov.update(parsed)
        else:
            into = jacoco if kind == "jacoco" else go_profile
            for key, values in parsed.items():
                into.setdefault(key, []).extend(values)
    if swift:
        lcov.update(parse_lcov(swift_coverage.lcov_text(swift)))
    lcov.update(cobertura)
    return CoverageBundle(lcov=lcov, go_profile=go_profile or None, jacoco=jacoco or None, root=root)
