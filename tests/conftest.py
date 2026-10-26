"""Keep a mutant from launching coverage against the project under test.

`run()` falls through into `run_coverage` when a mutant skips the help return.
That would delete `target/coverage` and recurse into this suite.
"""

import pytest


@pytest.fixture(autouse=True)
def do_not_launch_project_coverage(monkeypatch):
    monkeypatch.setattr("crapper.cli.run_coverage", lambda *_args, **_kwargs: None)


def cobertura(filename, lines, name="Demo.Board"):
    """One Cobertura report with one class; `lines` are (number, hits, "covered/total" or None)."""

    rows = "".join(
        f'<line number="{number}" hits="{hits}" branch="{"True" if cond else "False"}"'
        + (f' condition-coverage="50% ({cond})"' if cond else "")
        + " />"
        for number, hits, cond in lines
    )
    return (
        '﻿<?xml version="1.0" encoding="utf-8"?>\n<coverage><packages><package name="Demo">'
        f'<classes><class name="{name}" filename="{filename}"><methods/><lines>{rows}</lines>'
        "</class></classes></package></packages></coverage>"
    )
