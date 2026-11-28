from pathlib import Path

import sdts
from sdts import __main__ as cli


def _changelog() -> str:
    return (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")


def test_changelog_documents_current_version():
    """A released version must have a heading with a date in CHANGELOG.md.

    The expected version is read from the single source of truth, not
    duplicated here: otherwise every release commit would have to touch a test
    file as well, which breaks the rule that a release commit contains only the
    changelog and the version bump.
    """
    heading = f"## [{sdts.__version__}] - "
    assert heading in _changelog(), f"CHANGELOG.md has no `{heading}` section"


def test_changelog_has_unreleased_section_first():
    """Entries accumulate under [Unreleased] before a release promotes them."""
    text = _changelog()
    assert text.index("## [Unreleased]") < text.index(
        f"## [{sdts.__version__}]")


def test_version_is_reported_by_the_cli_package():
    """`python -m sdts` runs from the package, so it must see the same string."""
    assert cli.__name__ == "sdts.__main__"
    assert sdts.__version__.count(".") == 2
