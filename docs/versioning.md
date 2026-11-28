# Version management

How versions are numbered, what a release commit looks like, and how an entry
gets into [`CHANGELOG.md`](../CHANGELOG.md).

The short version:

- **SemVer** for the number, **Keep a Changelog** for the file.
- The changelog is written **while** the change is made, in the `[Unreleased]`
  section, not reconstructed from `git log` at release time.
- One source of truth: `sdts/__init__.py:__version__`. `pyproject.toml` and the
  git tag follow it.
- Merging into `main` is not a release. A release is a version commit plus a tag.

---

## 1. Version numbers

`MAJOR.MINOR.PATCH`, starting at `0.1.0`.

| Bump | When | Example in this project |
|---|---|---|
| **MAJOR** | A released interface changes incompatibly: CLI flag removed or renamed, config key removed, output JSON key removed or given a different meaning, Python API signature broken. | Dropping `--granularity` or renaming `merge.min_part_share`. |
| **MINOR** | New capability, backwards compatible: a new subcommand, a new config section, a new output format, an optional algorithm improvement. | `0.2.0` — speaker identity. |
| **PATCH** | Bug fix or documentation/metadata change with no new capability and no interface change. | `0.1.1` — the Japanese README. A threshold fix that changes a number but not the interface. |

Rules of thumb:

- **Model or threshold changes that alter output** are `MINOR`, not `PATCH`,
  even though nothing breaks: transcripts produced by `0.2.x` and `0.3.0` will
  differ, and consumers who diff results need to know. Say so in the entry.
- **Config is an interface.** Adding a key is `MINOR`; renaming or removing one
  is `MAJOR` unless the old key keeps working with a deprecation warning for at
  least one minor cycle.
- **The `0.x` caveat.** While `MAJOR` is 0 the format is still settling, so a
  breaking change bumps the *minor* instead of the major (`0.2.0` → `0.3.0`).
  Anything that would have been `MAJOR` gets an explicit **Changed** or
  **Removed** entry with a migration note. Once `1.0.0` ships, the table above
  applies literally.
- **Pre-releases** use `-rc.1`, `-beta.1` suffixes when a change needs field
  testing on real audio before it becomes the default. `1.0.0-rc.1` < `1.0.0`.
- **Do not bump for refactors.** Moving code between modules with no behaviour
  change is not a version event; it belongs in the commit message, not the
  changelog.

There is no calver, no build metadata, no date in the version string. Dates live
in the changelog heading.

---

## 2. What goes in the changelog

An entry is worth writing when **a user of the package, the CLI or the output
files would notice it**. That includes:

- anything under `sdts/` that changes behaviour, including a default value;
- config keys added, renamed or removed;
- CLI flags and subcommands;
- the shape of `out/*.json`, `*.srt`, `*.txt`, `*.md`;
- dependency bumps with a functional effect (a new pyannote, a new model);
- minimum Python version, model licence changes;
- security-relevant behaviour, however small.

Not worth an entry: internal refactors, test-only changes, README typos, CI
tweaks. If it is ambiguous, ask whether someone upgrading needs to know — if
yes, write it.

### Entry format

One bullet, past-tense or imperative-free, starting with the *thing* that
changed rather than with "we" or the module name. Add the reason when the reason
is not obvious; this repo's entries are read by whoever debugs an old
transcript six months later.

```markdown
- **Local voice registry** (`sdts/registry.py`): SQLite store with one
  L2-normalised anchor vector per speaker and up to `max_centroids` adapted
  centroids.
- Review page rounded sample duration down (`8.2` → `8 秒`); it now shows one
  decimal.
```

Group bullets under the section that matches the change type:

| Section | Use for |
|---|---|
| **Added** | new capability |
| **Changed** | existing behaviour, defaults, output that differs |
| **Fixed** | bugs |
| **Removed** | features/interfaces taken out |
| **Deprecated** | still works, scheduled for removal — always state the replacement and the removal version |
| **Security** | vulnerabilities, privacy or consent behaviour |
| **Known limitations** | optional; honest list of what is still wrong at this version (used in `0.2.0`) |

Never rewrite a released section. Corrections go in the next version's entry
("Corrected the `0.2.0` note about …"). The file is an append-only record; that
is the point of it.

---

## 3. The day-to-day loop

```text
branch ──► change + test ──► CHANGELOG [Unreleased] ──► PR ──► squash to main
                                                              │
                            release: version bump + tag ◄─────┘
```

1. **Branch.** `feature/<topic>`, `fix/<topic>`, `docs/<topic>`.
2. **Write the entry as part of the change.** Add the bullet to
   `## [Unreleased]` in the right section. One bullet per user-visible change;
   several bullets is fine for a feature, as in `0.2.0`.
3. **In the PR**, the reviewer checks three things in the diff: does the code
   match the entry, is the entry in the right section, does anything in the diff
   deserve an entry that is missing.
4. **Merge as a squash** (this repo's convention) so `main` gains one commit per
   feature. The `PR #` number lands in the commit subject and is how the entry
   is traced back to the discussion.
5. **Release.** When `main` is in a state you are willing to stand behind:

   ```bash
   # 1. promote Unreleased
   #    ## [Unreleased]            ->  ## [Unreleased]\n\nNothing yet.
   #    ## [0.3.0] - 2026-12-05    (today's date, ISO 8601, no leading zero padding)
   #  2. keep the compare links at the bottom of CHANGELOG.md in sync
   #  3. bump the version in both places
   ```

   ```bash
   $EDITOR sdts/__init__.py          # __version__ = "0.3.0"
   $EDITOR CHANGELOG.md              # heading, date, compare links
   python -m pytest -q               # 48 passed, including test_version.py
   git add CHANGELOG.md pyproject.toml sdts/__init__.py
   git commit -m "Release 0.3.0"
   git tag -a v0.3.0 -m "0.3.0"
   git push && git push --tags
   git archive --format=zip -o /tmp/sdts-0.3.0.zip v0.3.0   # the shipped snapshot
   ```

6. **After the release**, open the next `[Unreleased]` section if step 1 closed
   it, and keep building.

Small repositories may skip formal releases for a while. That is fine: the
`[Unreleased]` section then simply accumulates, and the first release tags
everything at once. What is **not** fine is a release tag whose changelog
section does not match what was actually on `main` at that commit.

### What 0.1.0, 0.1.1 and 0.2.0 did *not* do

All three were released from `main` **without a git tag** — the repository has
no tags at all, and the three sections in `CHANGELOG.md` are dated, attributed
sections rather than tag checkouts. That is the accumulated-`[Unreleased]` case
above, resolved retroactively: the dates come from the commits on `main`, not
from tags.

The consequence is that `0.1.0` and `0.1.1` cannot be checked out again, and
`0.2.0` will only be taggable if it is done right now, at this commit. Do that
if you want the first clean tag, and note in the next release that the earlier
sections predate tagging. Do **not** retro-tag a later commit as `v0.2.0` — a
tag that points at a tree it does not describe is worse than a missing tag.

---

## 4. The two places a version lives

| Place | Purpose | Changed by |
|---|---|---|
| `sdts/__init__.py` → `__version__` | the only editable copy; runtime truth for `import sdts` | release commit |
| `pyproject.toml` → `dynamic = ["version"]` | instructs setuptools to read the line above; never holds a literal | never (set once) |
| git tag `vX.Y.Z` | immutable pointer to the released tree | `git tag -a` at the release commit |

Previously `pyproject.toml` carried its own `version = "..."` literal, which is
how a released wheel could claim a different version from the code it
contained. It now reads `sdts.__version__` via
`[tool.setuptools.dynamic]`, so a bump is one edit plus the changelog heading.
`tests/test_version.py` asserts that the current `__version__` has a dated
heading in `CHANGELOG.md` and that `[Unreleased]` still sits on top — a release
that forgets the changelog fails the suite.

There is deliberately **no** version derived from git metadata. A wheel
installed from a tarball, or a source checkout without `.git`, must still report
the right version.

---

## 5. Commit and merge conventions

Commit subject: `<Area>: <what changed>`, imperative, under 72 characters.

```text
P3: labelling CLI, local review HTML and registry commands
Fix labels init argument mismatch and relative source resolution
Release 0.2.0
```

The `P1..P5` prefixes were used while implementing a planned feature phase by
phase; they are useful in a branch that will be squashed, not on `main`.
Free-form subjects after a squash are fine as long as they say what changed.

Merge policy:

- Feature branch → `main`: **squash merge**, one commit, PR number in the
  subject. Keeps `main`'s history aligned with the changelog sections.
- Release: a plain commit (not squashed, not amended) plus an annotated tag.
- Hotfix on a released tag: branch `fix/<topic>` from the tag, same loop, then
  merge to `main` and release `PATCH`. Never move a published tag; if the
  release itself was wrong, supersede it with a new patch version.

---

## 6. Bottom of `CHANGELOG.md`

Keep the compare links current so old versions are one click apart on GitHub.
Until the first tag exists, the links are written but **do not resolve**: they
are the shape to fill in, and the `v0.1.0` line only starts working once such a
tag is pushed.

```markdown
[Unreleased]: https://github.com/fukaf/whodunit-asr/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/fukaf/whodunit-asr/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/fukaf/whodunit-asr/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fukaf/whodunit-asr/releases/tag/v0.1.0
```

Link rules: `[Unreleased]` compares the newest tag to `HEAD`; every released
version compares the previous tag to its own; the earliest uses the release page
URL until it too becomes a comparison.

---

## 7. Checklist

**Every PR**

- [ ] `[Unreleased]` has a bullet in the right section, or the PR argues why
      none is needed (pure refactor, test-only).
- [ ] The bullet says what changed for a user, not which file was edited.
- [ ] Breaking-looking changes are flagged as such even while `0.x`.

**Every release**

- [ ] `[Unreleased]` renamed to `[X.Y.Z] - YYYY-MM-DD`, a fresh empty
      `[Unreleased]` left above it.
- [ ] `sdts/__init__.py` bumped; `pyproject.toml` needs no edit (it reads the
      attribute) but the installed metadata should be eyeballed once.
- [ ] `pytest -q` green on the release commit — `tests/test_version.py` fails
      if the heading is missing.
- [ ] Compare links at the bottom updated.
- [ ] `git tag -a vX.Y.Z` pushed, plus an archive of the tree.
- [ ] The tag points at the commit whose tree the section describes. If it does
      not, do not tag — see §3, "What 0.1.0, 0.1.1 and 0.2.0 did *not* do".
