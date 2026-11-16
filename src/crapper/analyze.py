"""Turn source files and coverage into CRAP entries."""

from pathlib import Path

from crapper.coverage import CoverageBundle
from crapper.crap import make_entry, sort_entries
from crapper.discover import language_of
from crapper.languages import functions_in_file
from crapper.model import Entry, Function


def _coverage(
    bundle: CoverageBundle | None, function: Function, given: set[str] | None
) -> float | None:
    """Percentage for one function.

    None means coverage was not requested (`--no-coverage`), or the function's
    language is not looked up (`given`): reports were given and none holds a
    file of that language, or none were given and the language gets coverage
    only from given reports. A function the reports do not mention scores 0%,
    so it sorts with the other scores instead of sinking to the bottom as N/A.
    """

    if bundle is None or function.language not in given:
        return None
    found = bundle.percent_for(function)
    if found is None:
        return 0.0
    return found


def _source_path(file: Path, root: Path) -> str:
    try:
        return file.relative_to(root).as_posix()
    except ValueError:
        return file.as_posix()


def analyze_files(
    files: list[Path],
    project_root: Path,
    bundle: CoverageBundle | None,
) -> list[Entry]:
    root = project_root.resolve()
    if bundle is not None:
        bundle.bind_sources([_source_path(file.resolve(), root) for file in files])
    functions: list[Function] = []
    for file in files:
        file = file.resolve()
        language = language_of(file)
        if language is None:
            continue
        source = file.read_text(encoding="utf-8", errors="replace")
        try:
            relative = file.relative_to(root).as_posix()
        except ValueError:
            relative = file.as_posix()
        functions.extend(functions_in_file(language, source, relative, str(root)))
    given = None if bundle is None else bundle.given_languages(functions)
    return sort_entries([make_entry(function, _coverage(bundle, function, given)) for function in functions])
