"""Read Swift coverage from Xcode derived data or `swift test` exports, in memory.

Given by path with `--coverage-report`, derived data anywhere, or one folder
above it, is read by `read_derived_data`: each profile it holds
(`Build/ProfileData/<device>/*.profdata`) is read with
`xcrun llvm-cov export -format=lcov` against the test bundles and apps built
next to it, which also holds the packages linked into them; `xccov` reports
those packages as empty. Only the lines `llvm-cov` lists, which are the
executable ones, are kept, because the LCOV loader counts every line it is
given. Only the Swift files being scored are kept, under their absolute
paths, so a test or third-party file cannot match a source file by suffix. A
file in several profiles, or in several binaries, is merged line by line, so
a line is covered when any of them covered it. A profile that cannot be
converted, or no `xcrun`, as on Linux, raises `ConversionError`.

A `swift test --enable-code-coverage` export, under any name, is read by
`read_export` without `xcrun`. Its per-line counts are derived from each
file's segments the way `llvm-cov` derives its own LCOV lines; a JSON file
that is not a coverage export, or an export of an unexpected shape, raises
ValueError.

Both return each file's line counts and write nothing; the loader adds every
given input together with `add_counts` and reads `lcov_text`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from crapper.discover import language_of

_PROFILES = ("Build/ProfileData/*/*.profdata", "*/Build/ProfileData/*/*.profdata")
_BINARIES = ("*/*.xctest", "*/*.app", "*/*.app/PlugIns/*.xctest")
_EXPORT_TYPE = "llvm.coverage.json.export"


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _scored_sources(files: list[Path]) -> dict[str, str]:
    """Each scored Swift file by its real path, mapped to the path written."""

    return {
        os.path.realpath(file): str(Path(file).resolve())
        for file in files
        if language_of(Path(file)) == "swift"
    }


def _profiles(folder: Path) -> list[Path]:
    return sorted({path for pattern in _PROFILES for path in folder.glob(pattern)})


def _bundle_binaries(bundle: Path) -> list[Path]:
    """The bundle's executable, and the `.debug.dylib` that holds an app's code in a debug build."""

    found = []
    for folder in (bundle, bundle / "Contents" / "MacOS"):
        for name in (bundle.stem, f"{bundle.stem}.debug.dylib"):
            if (folder / name).is_file():
                found.append(folder / name)
    return found


def _binaries(profile: Path) -> list[Path]:
    """Test bundles, hosted test bundles, and apps in the `Build/Products` next to the profile."""

    products = profile.parents[2] / "Products"
    bundles = sorted({path for pattern in _BINARIES for path in products.glob(pattern)})
    return [binary for bundle in bundles for binary in _bundle_binaries(bundle)]


def _archs(binary: Path) -> list[str]:
    try:
        command = ["xcrun", "lipo", "-archs", str(binary)]
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return []
    return completed.stdout.split() if completed.returncode == 0 else []


def _arch_options(binaries: list[Path]) -> list[list[str]]:
    """The `-arch` to follow each binary with, or none at all.

    `llvm-cov` refuses a universal binary, even one with a single slice such
    as Xcode's UI test runner, without `-arch`, and pairs the `-arch` options
    with the binaries in order, so every binary gets its own: `arm64` if it
    has that slice, its first slice otherwise. When `lipo` cannot read one
    binary, none gets an `-arch`.
    """

    archs = [_archs(binary) for binary in binaries]
    unread = [str(binary) for binary, found in zip(binaries, archs) if not found]
    if unread:
        _warn(f"lipo could not read the architecture of {', '.join(unread)}; llvm-cov gets no -arch.")
        return [[] for _ in binaries]
    return [[f"-arch={'arm64' if 'arm64' in found else found[0]}"] for found in archs]


class ConversionError(Exception):
    """Xcode derived data that cannot be converted, with the reason."""


def _export(profile: Path, binaries: list[Path]) -> str:
    options = _arch_options(binaries)
    command = [
        "xcrun", "llvm-cov", "export", "-format=lcov", "-skip-functions",
        "-instr-profile", str(profile), str(binaries[0]), *options[0],
    ]
    for binary, arch in zip(binaries[1:], options[1:]):
        command.extend(["-object", str(binary), *arch])
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise ConversionError(f"{profile}: llvm-cov failed to start ({exc})") from exc
    if completed.returncode != 0:
        raise ConversionError(f"{profile}: llvm-cov exited {completed.returncode} ({completed.stderr.strip()})")
    return completed.stdout


def _counts_for(merged: dict[str, dict[int, int]], path: str, sources: dict[str, str]) -> dict[int, int] | None:
    source = sources.get(os.path.realpath(path))
    return None if source is None else merged.setdefault(source, {})


def _merge_lcov(merged: dict[str, dict[int, int]], text: str, sources: dict[str, str]) -> None:
    counts: dict[int, int] | None = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            counts = _counts_for(merged, line[3:], sources)
        elif line.startswith("DA:") and counts is not None:
            number, hits = line[3:].split(",")[:2]
            counts[int(number)] = counts.get(int(number), 0) + int(hits)
        elif line == "end_of_record":
            counts = None


def _line_counts(segments: list) -> dict[int, int]:
    """Each executable line's count, as `llvm-cov`'s line coverage iterator gives it.

    A segment is `[line, column, count, hasCount, isRegionEntry, isGapRegion]`.
    A line is executable when a counted region starts on it, or when the
    segment carried over from an earlier line has a count; its count is the
    highest of those. A line that starts a skipped region is not executable.
    """

    counts: dict[int, int] = {}
    if not segments:
        return counts
    by_line: dict[int, list] = {}
    for segment in segments:
        by_line.setdefault(segment[0], []).append(segment)
    wrapped = None
    for number in range(segments[0][0], segments[-1][0] + 1):
        starting = by_line.get(number, [])
        starts = [s for s in starting if s[3] and s[4] and not s[5]]
        skipped = bool(starting) and not starting[0][3] and starting[0][4]
        mapped = not skipped and (bool(wrapped and wrapped[3]) or bool(starts))
        mapped = mapped or any(s[3] and s[4] for s in starting)
        if mapped:
            counts[number] = max([wrapped[2] if wrapped else 0, *(s[2] for s in starts)])
        if starting:
            wrapped = starting[-1]
    return counts


def _export_counts(export: Path, sources: dict[str, str]) -> dict[str, dict[int, int]]:
    """The scored files' line counts in a `swift test` export; ValueError when it is not one."""

    try:
        data = json.loads(export.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"not a swift test coverage export ({exc})") from exc
    if not isinstance(data, dict) or data.get("type") != _EXPORT_TYPE:
        raise ValueError(f"not a swift test coverage export (no type {_EXPORT_TYPE})")
    read: dict[str, dict[int, int]] = {}
    try:
        for unit in data.get("data", []):
            for file in unit.get("files", []):
                counts = _counts_for(read, file["filename"], sources)
                if counts is None:
                    continue
                for number, hits in _line_counts(file.get("segments", [])).items():
                    counts[number] = counts.get(number, 0) + hits
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise ValueError(f"not a swift test coverage export ({exc!r})") from exc
    return read


def add_counts(merged: dict[str, dict[int, int]], read: dict[str, dict[int, int]]) -> None:
    """Add one input's line counts to `merged`, so a line is covered when any input covered it."""

    for source, counts in read.items():
        target = merged.setdefault(source, {})
        for number, hits in counts.items():
            target[number] = target.get(number, 0) + hits


def lcov_text(merged: dict[str, dict[int, int]]) -> str:
    records = []
    for source in sorted(merged):
        counts = merged[source]
        lines = [f"DA:{number},{counts[number]}" for number in sorted(counts)]
        records.append("\n".join([f"SF:{source}", *lines, "end_of_record"]))
    return "\n".join(records) + "\n"


def _profile_lcov(profile: Path) -> str:
    binaries = _binaries(profile)
    if not binaries:
        raise ConversionError(f"{profile}: no test bundle or app next to it")
    return _export(profile, binaries)


def _merge_profiles(merged: dict[str, dict[int, int]], profiles: list[Path], sources: dict[str, str]) -> None:
    """Merge each profile's lines; raises ConversionError when one cannot be converted."""

    if shutil.which("xcrun") is None:
        names = ", ".join(str(profile) for profile in profiles)
        raise ConversionError(f"xcrun is not installed, so the Xcode coverage profiles {names} cannot be read")
    for profile in profiles:
        _merge_lcov(merged, _profile_lcov(profile), sources)


def is_derived_data(folder: Path) -> bool:
    """Whether `folder`, or a folder directly inside it, is Xcode derived data with a profile."""

    return folder.is_dir() and bool(_profiles(folder))


def read_derived_data(folder: Path, files: list[Path]) -> dict[str, dict[int, int]]:
    """Each scored file's line counts in the derived data at `folder`, or one folder down.

    Nothing is written. Raises ConversionError when a profile cannot be converted.
    """

    merged: dict[str, dict[int, int]] = {}
    _merge_profiles(merged, _profiles(folder), _scored_sources(files))
    return merged


def read_export(export: Path, files: list[Path]) -> dict[str, dict[int, int]]:
    """Each scored file's line counts in a `swift test` export; ValueError when it is not one."""

    return _export_counts(export, _scored_sources(files))
