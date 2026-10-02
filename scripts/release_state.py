"""Find where this run's release stands, for release.yml's release job.

The release job runs on the commit ci.yml's checks passed on, ``$GITHUB_SHA``. Before it versions
anything, it reads the tags, PyPI and origin's main, and hands the later steps one of:

- ``resume=<tag>``: an earlier attempt of this job pushed ``<tag>`` and then failed, and PyPI
  does not have its version yet, so this run finishes that release. The tag is this run's only
  if it is on the release commit python-semantic-release made from ``$GITHUB_SHA``, that
  commit's child; ``git tag --contains`` also lists the tags of later releases.
- ``done=true``: PyPI has that tag's version already, so this is a re-run of a release that went
  out. origin's main is its release commit now, which is no reason to start another run.
- ``moved=true``: there is no such tag, and origin's main has moved on from ``$GITHUB_SHA``
  during the tests, so the job hands the release to a new run on the new main.
- nothing: there is no such tag and main is still ``$GITHUB_SHA``, which the job versions.

PyPI's 200 and 404 are the only answers it acts on. On anything else, or no answer, it asks again,
three tries in all, ten seconds apart, and then fails the job: a guess would either leave a tagged
release off PyPI or publish it again, and "Re-run failed jobs" asks once more. A git command that
fails fails the job too, rather than reading as "main moved".

In the job, from the checkout (all of its history and tags, and credentials for origin):

    uv run --no-project python scripts/release_state.py

Standard library only, so it runs without the project's environment, and on Python 3.10.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

PACKAGE = "maidr-mcp"
PYPI = "https://pypi.org/pypi"
TRIES = 3
PAUSE = 10  # seconds between tries

# PyPI's HTTP status for a version of PACKAGE; raises OSError or HTTPException when PyPI does not
# answer.
Lookup = Callable[[str], int]
Sleep = Callable[[float], None]


class Unknown(Exception):
    """PyPI gave no answer the job can act on, so whether the tag is published is unknown."""


def log(message: str) -> None:
    print(f"release_state: {message}", file=sys.stderr)


def git(*args: str) -> str:
    """``git args``'s output, from the current directory. A failure raises CalledProcessError."""
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, text=True).stdout


def parent(tag: str) -> str:
    """The commit ``tag``'s commit was made from, or "" when it has none."""
    found = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}^"],
        stdout=subprocess.PIPE,
        text=True,
    )
    return found.stdout.strip()


def release_tag(sha: str) -> str:
    """The tag on the release commit made from ``sha``, or "" when there is none."""
    for tag in git("tag", "--list", "v*", "--contains", sha).split():
        # --contains also lists tags on later commits; only the release commit made from this
        # one, its child, is this run's.
        if parent(tag) == sha:
            return tag
    return ""


def pypi_status(version: str) -> int:
    """PyPI's HTTP status for maidr-mcp ``version``: 200 when it has it, 404 when not."""
    try:
        with urllib.request.urlopen(f"{PYPI}/{PACKAGE}/{version}/json", timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def published(tag: str, lookup: Lookup = pypi_status, sleep: Sleep = time.sleep) -> bool:
    """Whether PyPI has ``tag``'s version, asking up to TRIES times; Unknown when it never says."""
    version = tag.removeprefix("v")
    for attempt in range(1, TRIES + 1):
        try:
            status = lookup(version)
        except (OSError, http.client.HTTPException) as error:
            # The repr escapes any line break, so nothing in it can start a workflow command.
            answer = "did not answer"
            log(f"try {attempt} of {TRIES}: PyPI {answer} for {PACKAGE} {version}: {error!r}")
        else:
            if status == 200:
                return True
            if status == 404:
                return False
            answer = f"answered {status}"
            log(f"try {attempt} of {TRIES}: PyPI {answer} for {PACKAGE} {version}")
        if attempt < TRIES:
            sleep(PAUSE)
    raise Unknown(
        f"PyPI {answer} for {PACKAGE} {version} on the last of {TRIES} tries, "
        f"{PAUSE} seconds apart, so whether {tag} is published is unknown."
    )


def origin_main() -> str:
    """The commit origin's main is at now, or "" when origin has no main."""
    heads = git("ls-remote", "origin", "refs/heads/main").split()
    return heads[0] if heads else ""


def where(sha: str, lookup: Lookup = pypi_status, sleep: Sleep = time.sleep) -> dict[str, str]:
    """The outputs that tell release.yml's later steps where the release from ``sha`` stands."""
    if tag := release_tag(sha):
        if not published(tag, lookup, sleep):
            return {"resume": tag}
        # A re-run of a release that went out: main is its release commit now, which is no
        # reason to start another run.
        print(f"::notice::{tag}, released from this commit, is already on PyPI. Nothing to do.")
        return {"done": "true"}
    if origin_main() != sha:
        return {"moved": "true"}
    return {}


def write_outputs(path: str, values: dict[str, str]) -> None:
    """Hands ``values`` to the next steps through the file at ``path``, and logs them."""
    lines = [f"{key}={value}\n" for key, value in values.items()]
    for line in lines:
        log(line.rstrip("\n"))
    with open(path, "a", encoding="utf-8") as out:
        out.write("".join(lines))


def main(lookup: Lookup = pypi_status, sleep: Sleep = time.sleep) -> int:
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        print("::error::GITHUB_SHA is not set, so there is no commit to find the release of.")
        return 1
    # Without it the later steps would read no output at all, which is the "version as usual"
    # case, so a lost resume would leave a tagged release off PyPI.
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        print(
            "::error::GITHUB_OUTPUT is not set, so the next steps could not read where the "
            "release stands."
        )
        return 1
    try:
        outputs = where(sha, lookup, sleep)
    except Unknown as unknown:
        print(f"::error::{unknown} Re-run this job.")
        return 1
    except subprocess.CalledProcessError as error:
        # git has printed why, above it.
        print(
            f"::error::`{' '.join(error.cmd)}` failed, so where the release stands is unknown. "
            "Re-run this job."
        )
        return 1
    write_outputs(output, outputs)
    return 0


if __name__ == "__main__":
    # A runner's stdout is a pipe, which Python buffers; line by line, the notices land in the
    # log in order with the logged tries.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
