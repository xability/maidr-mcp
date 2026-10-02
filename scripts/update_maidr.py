"""Raise maidr-mcp's two maidr pins to the newest releases.

maidr-mcp pins maidr twice:

- ``MAIDR_JS_VERSION`` in ``src/maidr_mcp/server.py``, the maidr.js the chart view loads from
  jsDelivr: what every reader gets;
- ``maidr`` (py-maidr) in ``uv.lock``, what CI and a checkout test against. uvx and Docker
  installs resolve py-maidr afresh within pyproject's range, so it is the lock that trails.

``.github/workflows/update-maidr.yml`` runs ``update`` every hour, ``provenance`` on a new
maidr.js, then every check against what they wrote, and commits only when all of them pass;
``report`` writes the issue it opens when one does not, and ``resolved`` picks the issues a later
run settles. By hand, from the repository root:

    uv run --no-project python scripts/update_maidr.py update
    uv run --no-project python scripts/update_maidr.py update --maidr-js 4.14.0 --allow-lower

Standard library only, so it runs without the project's environment, and on Python 3.10.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SERVER = Path("src/maidr_mcp/server.py")
LOCK = Path("uv.lock")
REGISTRY = "https://registry.npmjs.org"
# maidr's release workflow, as the provenance of each maidr.js it publishes names it: its
# repository, its file, and the branch semantic-release releases from (maidr's .releaserc.json).
MAIDR_REPOSITORY = "https://github.com/xability/maidr"
MAIDR_RELEASE_WORKFLOW = ".github/workflows/release.yml"
MAIDR_RELEASE_REF = "refs/heads/main"
SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
# Every issue title starts with it.
TITLE_PREFIX = "Automatic maidr update"
# The body of an issue ends with it when its failure holds the versions (Step.holds): while that
# issue is open, the hourly run leaves them alone. update-maidr.yml looks for it.
HOLD_MARKER = "<!-- update-maidr: hold -->"

# An npm version as semver writes it: MAJOR.MINOR.PATCH, numeric identifiers without leading
# zeros, an optional -prerelease, and no +build, which npm drops from what it publishes. ASCII
# classes rather than \d, which matches any Unicode digit; fullmatch rather than $, which lets a
# trailing newline through; and the length checked first, so nothing long reaches the pattern.
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
NPM_VERSION = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE}(?:\.{_PRE})*)?")
# A PyPI release in PEP 440's normal form, without epochs or local versions.
PYPI_VERSION = re.compile(
    r"([0-9]+(?:\.[0-9]+)*)(?:(a|b|rc)([0-9]+))?(?:\.post([0-9]+))?(?:\.dev([0-9]+))?"
)
MAX_VERSION_LENGTH = 64

PIN = re.compile(r'^MAIDR_JS_VERSION = "([^"\n]*)"$', re.MULTILINE)
PACKAGE = re.compile(
    r'^\[\[package\]\]\nname = "([^"\n]+)"\n(?:version = "([^"\n]+)"\n)?', re.MULTILINE
)

Fetch = Callable[[str], Any]
Lock = Callable[[Path, str], None]


class Refused(Exception):
    """The update stopped. The message goes into an issue, so it never quotes what it refused.

    ``passing`` marks a fault that says nothing about the versions, a registry that could not
    be read, so the hourly run tries them again.
    """

    def __init__(self, message: str, *, passing: bool = False) -> None:
        super().__init__(message)
        self.passing = passing


def log(message: str) -> None:
    print(f"update_maidr: {message}", file=sys.stderr)


def valid(value: object, pattern: re.Pattern[str] = NPM_VERSION) -> str:
    """``value`` if it is a version number, "" otherwise."""
    ok = isinstance(value, str) and len(value) <= MAX_VERSION_LENGTH and pattern.fullmatch(value)
    return value if ok else ""


def checked(value: object, what: str, pattern: re.Pattern[str] = NPM_VERSION) -> str:
    """``value`` if it is a version number, refused otherwise."""
    if version := valid(value, pattern):
        return version
    # It can come from a repository_dispatch payload. The log gets its repr, which escapes every
    # line break, so it cannot start a workflow command; the message gets none of it.
    log(f"refusing {what}: {str(value)[:80]!r}")
    raise Refused(f"{what} is not a version number")


def version_key(version: str) -> tuple[Any, ...]:
    """Sorts npm versions by semver precedence: a prerelease comes before its release."""
    core, _, pre = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not pre:
        return (major, minor, patch, 1, ())
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre.split("."))
    return (major, minor, patch, 0, ids)


def pypi_key(version: str) -> tuple[Any, ...]:
    """Sorts what PYPI_VERSION takes as PEP 440 does: 1.0.dev1 < 1.0a1 < 1.0 = 1.0.0 < 1.0.post1."""
    match = PYPI_VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"not a PyPI version: {version[:80]!r}")
    release, phase, number, post, dev = match.groups()
    parts = [int(part) for part in release.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    if phase:
        pre = (("a", "b", "rc").index(phase), int(number))
    elif dev is not None and post is None:
        pre = (-1, 0)  # a .dev of the release itself comes before its first alpha
    else:
        pre = (3, 0)  # the release, and its .posts, come after every pre-release
    return (
        tuple(parts),
        pre,
        -1 if post is None else int(post),
        (1, 0) if dev is None else (0, int(dev)),
    )


def read_pin(source: str) -> str:
    pins = PIN.findall(source)
    if len(pins) != 1:
        raise Refused(f"{SERVER} sets MAIDR_JS_VERSION {len(pins)} times, not once")
    return pins[0]


def write_pin(source: str, version: str) -> str:
    """``source`` with MAIDR_JS_VERSION set to ``version``, and every other byte as it was."""
    read_pin(source)
    line = f'MAIDR_JS_VERSION = "{checked(version, "the maidr.js version")}"'
    return PIN.sub(lambda _: line, source, count=1)


def lock_versions(lock: str) -> dict[str, tuple[str, ...]]:
    """Each package in a uv.lock, with the versions it is locked at (more than one on a fork)."""
    found: dict[str, set[str]] = {}
    for name, version in PACKAGE.findall(lock):
        found.setdefault(name, set()).add(version)
    return {name: tuple(sorted(versions)) for name, versions in found.items()}


def locked_py_maidr(lock: str) -> str:
    versions = lock_versions(lock).get("maidr", ())
    if len(versions) != 1:
        raise Refused(f"{LOCK} locks maidr at {len(versions)} versions, not one")
    return checked(versions[0], "py-maidr's version in uv.lock", PYPI_VERSION)


def moved_packages(before: str, after: str) -> list[tuple[str, str, str]]:
    """The packages other than maidr whose locked versions differ, as (name, before, after)."""
    old, new = lock_versions(before), lock_versions(after)
    return [
        (name, ", ".join(old.get(name, ())) or "none", ", ".join(new.get(name, ())) or "none")
        for name in sorted(old.keys() | new.keys())
        if name != "maidr" and old.get(name) != new.get(name)
    ]


def _get(value: Any, *keys: str) -> Any:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def fetch_json(url: str) -> Any:
    """``url``'s JSON, after three tries ten seconds apart."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError) as error:  # URLError and HTTPError are OSErrors
            log(f"{url}: {error}")
            if attempt < 2:
                time.sleep(10)
    raise Refused(f"could not read {url}", passing=True)


def uv_lock(root: Path, requirement: str) -> None:
    # --no-build: resolve from wheels and published metadata alone, so locking runs no package.
    command = ["uv", "lock", "--upgrade-package", requirement, "--no-build"]
    try:
        subprocess.run(command, cwd=root, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise Refused(f"`{' '.join(command)}` failed; the log has uv's message") from error


def npm_latest(fetch: Fetch) -> str:
    return checked(_get(fetch(f"{REGISTRY}/maidr/latest"), "version"), "npm's latest maidr")


def check_provenance(version: str, fetch: Fetch = fetch_json) -> None:
    """Refuses a maidr.js that npm does not say maidr's release workflow built.

    maidr publishes with --provenance, so every release carries a SLSA provenance statement:
    which workflow built it, in which repository and on which branch, and the digest of the
    tarball it built. This reads it as the registry serves it, and requires that it names maidr's
    release workflow on main and the tarball npm serves for this version. It does not check the
    statement's Sigstore signature itself: update-maidr.yml runs `npm audit signatures` for that.
    What it turns away is a release published any other way: by hand, from another repository,
    from another workflow or branch of maidr's, or with a stolen token.
    """
    version = checked(version, "the maidr.js version")
    doc = fetch(f"{REGISTRY}/maidr/{version}")
    if _get(doc, "name") != "maidr" or _get(doc, "version") != version:
        raise Refused(f"npm has no maidr {version}")
    if _get(doc, "dist", "attestations", "provenance", "predicateType") != SLSA_PROVENANCE:
        raise Refused(
            f"maidr.js {version} carries no npm provenance, so nothing says maidr's release "
            "workflow published it"
        )
    integrity = _get(doc, "dist", "integrity")
    try:
        if not (isinstance(integrity, str) and integrity.startswith("sha512-")):
            raise ValueError
        tarball = base64.b64decode(integrity.removeprefix("sha512-"), validate=True).hex()
    except (ValueError, binascii.Error):
        raise Refused(f"npm gives no sha512 digest for maidr.js {version}") from None

    # The URL is built here rather than taken from the document, so the registry is all it reads.
    bundle = fetch(f"{REGISTRY}/-/npm/v1/attestations/maidr@{version}")
    attestations = _get(bundle, "attestations")
    for attestation in attestations if isinstance(attestations, list) else []:
        if _get(attestation, "predicateType") != SLSA_PROVENANCE:
            continue
        try:
            payload = _get(attestation, "bundle", "dsseEnvelope", "payload")
            statement = json.loads(base64.b64decode(payload, validate=True))
        except (TypeError, ValueError, binascii.Error):
            continue
        subjects = _get(statement, "subject")
        built = any(
            _get(subject, "name") == f"pkg:npm/maidr@{version}"
            and _get(subject, "digest", "sha512") == tarball
            for subject in (subjects if isinstance(subjects, list) else [])
        )
        workflow = _get(statement, "predicate", "buildDefinition", "externalParameters", "workflow")
        named = tuple(_get(workflow, key) for key in ("repository", "path", "ref"))
        if built and named == (MAIDR_REPOSITORY, MAIDR_RELEASE_WORKFLOW, MAIDR_RELEASE_REF):
            return
        log(f"maidr@{version}'s provenance names {str(named)[:300]!r}")
    raise Refused(
        f"maidr.js {version}'s npm provenance does not say {MAIDR_RELEASE_WORKFLOW} on "
        f"{MAIDR_RELEASE_REF} of {MAIDR_REPOSITORY} built the tarball npm serves"
    )


def files_digest(root: Path) -> str:
    """One digest of server.py and uv.lock together, to compare two jobs' updates by."""
    digest = hashlib.sha256()
    for path in (SERVER, LOCK):
        digest.update(hashlib.sha256((root / path).read_bytes()).digest())
    return digest.hexdigest()


def issue_title(js: str, py: str) -> str:
    if not (js and py):
        return f"{TITLE_PREFIX} failed before choosing the versions"
    return f"{TITLE_PREFIX} to maidr.js {js} / py-maidr {py} failed"


# A title issue_title wrote for a pair of versions.
PAIR_TITLE = re.compile(rf"{re.escape(TITLE_PREFIX)} to maidr\.js (\S+) / py-maidr (\S+) failed")


@dataclass
class Update:
    js_old: str
    js_new: str
    py_old: str
    py_new: str
    digest: str
    moved: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.js_new != self.js_old or self.py_new != self.py_old

    def outputs(self) -> dict[str, str]:
        return {
            "changed": str(self.changed).lower(),
            "js_old": self.js_old,
            "js_new": self.js_new,
            "py_old": self.py_old,
            "py_new": self.py_new,
            "issue_title": issue_title(self.js_new, self.py_new),
            "digest": self.digest,
        }


def update(
    root: Path,
    *,
    maidr_js: str = "",
    py_maidr: str = "",
    allow_lower: bool = False,
    fetch: Fetch = fetch_json,
    lock: Lock = uv_lock,
) -> Update:
    """Writes the newest maidr.js into server.py and the newest py-maidr into uv.lock.

    ``maidr_js`` and ``py_maidr`` ask for those versions instead. A newest maidr.js below the
    pin leaves the pin, as does a requested one unless ``allow_lower``, so a registry replica
    that has not seen a release yet cannot take it back. py-maidr comes from ``uv lock``, within
    pyproject's range. Files change only where a version does: a lock that uv rewrites without
    moving maidr is put back as it was.
    """
    if maidr_js:
        maidr_js = checked(maidr_js, "the requested maidr.js version")
    if py_maidr:
        py_maidr = checked(py_maidr, "the requested py-maidr version", PYPI_VERSION)
    server_path, lock_path = root / SERVER, root / LOCK
    server = server_path.read_bytes().decode("utf-8")
    js_old = checked(read_pin(server), "MAIDR_JS_VERSION in server.py")

    js_new = maidr_js or npm_latest(fetch)
    if version_key(js_new) < version_key(js_old) and not (maidr_js and allow_lower):
        log(f"leaving maidr.js at {js_old}, above {js_new}")
        js_new = js_old

    lock_before = lock_path.read_bytes()
    py_old = locked_py_maidr(lock_before.decode("utf-8"))
    lock(root, f"maidr=={py_maidr}" if py_maidr else "maidr")
    lock_after = lock_path.read_bytes()
    try:
        py_new = locked_py_maidr(lock_after.decode("utf-8"))
    except Refused:
        lock_path.write_bytes(lock_before)
        raise
    moved: list[tuple[str, str, str]] = []
    if py_new == py_old:
        lock_path.write_bytes(lock_before)
    else:
        moved = moved_packages(lock_before.decode("utf-8"), lock_after.decode("utf-8"))

    if js_new != js_old:
        server_path.write_bytes(write_pin(server, js_new).encode("utf-8"))
    return Update(js_old, js_new, py_old, py_new, files_digest(root), moved)


def commit_message(change: Update, run_url: str = "") -> str:
    if not change.changed:
        raise ValueError("nothing changed")
    named, body = [], []
    if change.js_new != change.js_old:
        named.append(f"maidr.js {change.js_new}")
        body.append(
            f"maidr.js {change.js_old} -> {change.js_new}: MAIDR_JS_VERSION, the maidr.js "
            "the chart view loads from jsDelivr."
        )
    if change.py_new != change.py_old:
        named.append(f"py-maidr {change.py_new}")
        body.append(
            f"py-maidr {change.py_old} -> {change.py_new}: uv.lock, what CI and a checkout "
            "test against."
        )
    if change.moved:
        moves = ", ".join(f"{name} {old} -> {new}" for name, old, new in change.moved)
        body.append(f"uv.lock also moves {moves}.")
    if run_url:
        body.append(
            "Committed by update-maidr.yml once ruff, pytest on Python 3.10 and 3.13, and "
            f"e2e/run.sh passed against these versions: {run_url}"
        )
    paragraphs = [
        textwrap.fill(p, width=72, break_long_words=False, break_on_hyphens=False) for p in body
    ]
    return "\n\n".join([f"fix(deps): load {' and '.join(named)}", *paragraphs]) + "\n"


REPRODUCE = (
    "`uv run --no-project python scripts/update_maidr.py update` writes the same update in a "
    "checkout, to reproduce it."
)


class Step(NamedTuple):
    doing: str
    hint: str = ""
    # Whether trying the same versions again would only repeat a failure here: a check failing
    # on them, or a push GitHub refused. The hourly run then leaves them alone while the issue is
    # open. One that may pass next time (a download, the browser's install, a release landing
    # between the jobs) it tries again, for a day.
    holds: bool = False


# The steps of update-maidr.yml that can fail, by id: what each was doing, and what to do.
STEPS = {
    "update": Step(
        "finding the newest releases (npm's latest maidr.js and PyPI's newest py-maidr)"
    ),
    "gate": Step("looking for open issues about the update"),
    "provenance": Step(
        "checking the new maidr.js's npm provenance",
        "If maidr now publishes from another workflow or branch, `MAIDR_RELEASE_WORKFLOW` and "
        "`MAIDR_RELEASE_REF` in `scripts/update_maidr.py` name the ones it accepts.",
        holds=True,
    ),
    "fetch": Step("downloading the new maidr.js from npm, to verify its signatures"),
    "signatures": Step(
        "verifying the Sigstore signatures of the new maidr.js, and the registry's on what it "
        "installs with it, with `npm audit signatures`",
        "The log names each package whose signature did not verify. One that fails twice is a "
        "package npm serves that its publisher did not sign: do not raise the pin by hand to "
        "it, and tell maidr's maintainers if it is maidr.",
        holds=True,
    ),
    "sync": Step("`uv sync --locked`", REPRODUCE),
    "lint": Step("`uv run ruff check .`", holds=True),
    "format": Step("`uv run ruff format --check .`", holds=True),
    "pytest": Step("`pytest`, on Python 3.10 and 3.13 as ci.yml runs it", REPRODUCE, holds=True),
    "browser": Step("installing Playwright's Chromium"),
    "e2e": Step(
        "`bash e2e/run.sh`",
        "If the log has `FAIL  maidr_run_command offers the model exactly the commands maidr "
        "lists as runnable`, this maidr.js runs other commands than `RunnableCommand` in "
        "`src/maidr_mcp/server.py` lists. Bring that list, and the one `tests/test_server.py` "
        "pins, in step with it in a pull request that also raises the pin, which "
        "`uv run --no-project python scripts/update_maidr.py update` writes; the pull request "
        "can close this issue.",
        holds=True,
    ),
    "land": Step("writing the update again in the job that pushes"),
    "push": Step(
        "pushing to `main`",
        "The push was refused for some reason other than `main` moving on, which the job "
        "leaves to the next run: check that GitHub Actions may still push to `main`.",
        holds=True,
    ),
}


def issue(
    *,
    step: str,
    reason: str = "",
    js_old: str = "",
    js_new: str = "",
    py_old: str = "",
    py_new: str = "",
    passing: bool = False,
    run_url: str = "",
) -> tuple[str, str]:
    """The title and body of the issue for a failed run.

    The workflow passes these in from its jobs' outputs. Anything not a version is dropped, a
    step not in STEPS is not named, and the reason, which only this script writes, goes on one
    line; so nothing reaches the issue that this script did not write or check. ``passing``
    says the step's script refused for a passing fault, which holds nothing.
    """

    js_old, js_new = valid(js_old), valid(js_new)
    py_old, py_new = valid(py_old, PYPI_VERSION), valid(py_new, PYPI_VERSION)
    doing, hint, holds = STEPS.get(step, Step("a step that did not record its name"))
    parts = [f"The automatic maidr update did not reach `main`: it stopped at {doing}."]
    if js_new and py_new:
        parts.append(
            "| | on `main` | this update |\n| --- | --- | --- |\n"
            f"| maidr.js (`MAIDR_JS_VERSION`, what the view loads) | {js_old or '?'} | {js_new} |\n"
            f"| py-maidr (`uv.lock`, what the tests run) | {py_old or '?'} | {py_new} |"
        )
    reason = " ".join(reason.split())[:600].rstrip(".")
    if reason:
        parts.append(f"Why: {reason}.")
    if hint:
        parts.append(hint)
    parts.append("Nothing was pushed." + (f" [The run]({run_url}) has the log." if run_url else ""))
    if js_new and py_new and holds and not passing:
        parts.append(
            "While this issue is open, the hourly run leaves these versions alone, so this is "
            "reported once rather than every hour. Close it once the cause is fixed, or if it "
            "was a passing fault, and the next hourly run tries again; running **update-maidr** "
            "from the Actions tab tries at once, and takes a maidr.js version. A newer maidr.js "
            "or py-maidr is tried as usual."
        )
        if js_old and js_new != js_old:
            parts.append(
                "Every update pairs npm's latest maidr.js with PyPI's newest py-maidr, so until "
                f"maidr.js {js_new} passes, a new py-maidr is tried only with it. To raise "
                "py-maidr alone meanwhile, run **update-maidr** by hand with maidr.js "
                f"{js_old}, the version `main` loads."
            )
        parts.append(HOLD_MARKER)
    elif js_new and py_new:
        parts.append(
            "This failure says nothing about these versions, so the hourly run tries them "
            "again, without adding to this issue, and closes it once they reach `main`. If they "
            "still fail a day after this issue was opened, it leaves them alone until the issue "
            "is closed. Running **update-maidr** from the Actions tab tries at once."
        )
    else:
        parts.append(
            "The hourly run tries again every hour, without adding to this issue, and closes it "
            "once a run chooses the versions."
        )
    return issue_title(js_new, py_new), "\n\n".join(parts) + "\n"


def resolved(
    issues: object,
    *,
    landed_js: str = "",
    landed_py: str = "",
    sha: str = "",
    reported_js: str = "",
    reported_py: str = "",
    reported_issue: str = "",
    run_url: str = "",
) -> list[dict[str, Any]]:
    """The bot's open update issues that this run settles, each with the comment that closes it.

    ``issues`` are those issues as ``{"number", "title"}``; the workflow calls this only once a
    run has chosen the versions, which settles the issue about a run that could not. A run that
    also pushed ``landed_js`` and ``landed_py`` settles every pair no newer than them. One that
    failed with ``reported_js`` and ``reported_py``, reported in ``reported_issue``, settles every
    other pair no newer than those, which no automatic run tries again. A pair newer in either
    stays open: one a lower maidr.js given by hand left behind, say.
    """
    landed_js, landed_py = valid(landed_js), valid(landed_py, PYPI_VERSION)
    sha = sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""
    reported_js, reported_py = valid(reported_js), valid(reported_py, PYPI_VERSION)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", reported_issue):
        reported_js = reported_py = ""

    def no_newer(js: str, py: str, than_js: str, than_py: str) -> bool:
        return version_key(js) <= version_key(than_js) and pypi_key(py) <= pypi_key(than_py)

    later = f"[A later run]({run_url})" if run_url else "A later run"
    settled = []
    for item in issues if isinstance(issues, list) else []:
        number, title = _get(item, "number"), _get(item, "title")
        if type(number) is not int or not isinstance(title, str):
            continue
        if title == issue_title("", ""):
            comment = f"{later} chose the versions again, so this report is out of date."
            settled.append({"number": number, "comment": comment})
            continue
        pair = PAIR_TITLE.fullmatch(title)
        js, py = (valid(pair[1]), valid(pair[2], PYPI_VERSION)) if pair else ("", "")
        if not (js and py):
            continue
        if landed_js and landed_py and no_newer(js, py, landed_js, landed_py):
            commit = f" ({sha})" if sha else ""
            comment = (
                f"`main` now loads maidr.js {landed_js} and locks py-maidr {landed_py}{commit}, "
                "which passed every check, so this report is out of date. A failure with a later "
                "release opens a new issue."
            )
        elif (
            reported_js
            and reported_py
            and title != issue_title(reported_js, reported_py)
            and no_newer(js, py, reported_js, reported_py)
        ):
            comment = (
                f"{later} tried maidr.js {reported_js} with py-maidr {reported_py}, and "
                f"#{reported_issue} reports how that failed, so this report is out of date: no "
                "automatic run tries these versions again."
            )
        else:
            continue
        settled.append({"number": number, "comment": comment})
    return settled


def run_url_from_env() -> str:
    parts = [os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY")]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{parts[0]}/{parts[1]}/actions/runs/{run_id}" if all(parts) and run_id else ""


def write_outputs(values: dict[str, str]) -> None:
    """Hands ``values`` to the next steps when run in GitHub Actions, and logs them."""
    lines = [f"{key}={' '.join(value.split())}" for key, value in values.items()]
    for line in lines:
        log(line)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write("".join(f"{line}\n" for line in lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("update", help="write the newest maidr.js and py-maidr")
    up.add_argument("--maidr-js", default="", help="this maidr.js rather than npm's latest")
    up.add_argument("--py-maidr", default="", help="this py-maidr rather than PyPI's newest")
    up.add_argument("--allow-lower", action="store_true", help="let --maidr-js lower the pin")
    up.add_argument("--expect-digest", default="", help="refuse unless the files come out so")
    up.add_argument("--message-file", type=Path, help="write the commit message here")
    prov = commands.add_parser("provenance", help="check a maidr.js's npm provenance")
    prov.add_argument("--maidr-js", required=True)
    rep = commands.add_parser("report", help="write the issue for a failed run")
    for name in ("step", "reason", "passing", "js-old", "js-new", "py-old", "py-new"):
        rep.add_argument(f"--{name}", default="")
    rep.add_argument("--body-file", type=Path, required=True)
    res = commands.add_parser(
        "resolved",
        help="read the bot's open issues as JSON on stdin; print the ones this run settles",
    )
    for name in ("landed-js", "landed-py", "sha", "reported-js", "reported-py", "reported-issue"):
        res.add_argument(f"--{name}", default="")
    args = parser.parse_args(argv)

    if args.command == "resolved":
        try:
            issues = json.load(sys.stdin)
        except ValueError:
            log("the open issues are not JSON")
            return 1
        settled = resolved(
            issues,
            landed_js=args.landed_js,
            landed_py=args.landed_py,
            sha=args.sha,
            reported_js=args.reported_js,
            reported_py=args.reported_py,
            reported_issue=args.reported_issue,
            run_url=run_url_from_env(),
        )
        print(json.dumps(settled))
        return 0

    if args.command == "report":
        title, body = issue(
            step=args.step,
            reason=args.reason,
            js_old=args.js_old,
            js_new=args.js_new,
            py_old=args.py_old,
            py_new=args.py_new,
            passing=args.passing == "true",
            run_url=run_url_from_env(),
        )
        args.body_file.write_text(body, encoding="utf-8")
        write_outputs({"title": title})
        return 0

    try:
        if args.command == "provenance":
            check_provenance(args.maidr_js)
            log(f"maidr.js {args.maidr_js} was built by {MAIDR_REPOSITORY}'s release workflow")
            return 0
        change = update(
            args.root,
            maidr_js=args.maidr_js,
            py_maidr=args.py_maidr,
            allow_lower=args.allow_lower,
        )
        if args.expect_digest and change.digest != args.expect_digest:
            raise Refused(
                "the job that pushes wrote a different server.py or uv.lock from the one the "
                "checks ran on, most likely because a dependency was released in between"
            )
    except Refused as refused:
        log(str(refused))
        write_outputs({"reason": str(refused), **({"passing": "true"} if refused.passing else {})})
        return 1
    write_outputs(change.outputs())
    for name, old, new in (
        ("maidr.js", change.js_old, change.js_new),
        ("py-maidr", change.py_old, change.py_new),
    ):
        print(f"{name} {old} -> {new}" if new != old else f"{name} {old}, the newest")
    if change.changed and args.message_file:
        args.message_file.write_text(commit_message(change, run_url_from_env()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
