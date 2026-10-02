"""scripts/release_state.py, which release.yml's release job runs to find where its release stands.

Each test builds what the job's checkout sees: a bare origin, a clone of it with the history and
the tags, and the commit the tests passed on as GITHUB_SHA. PyPI and the pause between its tries
are fakes, so nothing here reaches the network or waits.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_state", ROOT / "scripts/release_state.py")
assert _spec and _spec.loader
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

WORKFLOW = ROOT / ".github/workflows/release.yml"

# Who makes the commits and tags, and no git configuration but the repositories' own, so the
# tests run the same on any machine.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "maidr-mcp tests",
    "GIT_AUTHOR_EMAIL": "tests@example.com",
    "GIT_COMMITTER_NAME": "maidr-mcp tests",
    "GIT_COMMITTER_EMAIL": "tests@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


class Repo:
    """origin, a bare repository, and the release job's checkout of it.

    main starts at an earlier release, v0.1.0, which no test's commit is in.
    """

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.path = root / "checkout"
        git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        git(root, "init", "--quiet", "--initial-branch=main", str(self.path))
        self.git("remote", "add", "origin", str(self.origin))
        self.release("0.1.0")
        self.push()

    def git(self, *args: str) -> str:
        return git(self.path, *args)

    def commit(self, subject: str) -> str:
        """A commit on top of the checkout's last one; its hash."""
        self.git("commit", "--quiet", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def release(self, version: str) -> str:
        """A release commit and its tag, as python-semantic-release makes them; its hash."""
        sha = self.commit(f"chore(release): {version}")
        self.git("tag", "--annotate", f"v{version}", "-m", f"v{version}")
        return sha

    def push(self) -> None:
        """Pushes main and the tags to origin, as the merge or an earlier attempt did."""
        self.git("push", "--quiet", "--tags", "origin", "main")


class PyPI:
    """A fake lookup: gives ``answers`` in turn, then the last one for good; records each ask."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, version: str) -> int:
        self.asked.append(version)
        assert self.answers, f"PyPI was asked for {version}, which this test does not expect"
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class Job:
    code: int
    # What the step handed the later steps, as $GITHUB_OUTPUT holds it.
    outputs: str
    # What the step printed, where GitHub reads its workflow commands.
    stdout: str
    asked: list[str]
    pauses: list[float]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    made = Repo(tmp_path)
    monkeypatch.chdir(made.path)
    return made


@pytest.fixture
def job(repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """Runs the step on ``sha`` from the checkout, with PyPI giving ``answers``."""

    def run(sha: str, *answers: int | Exception) -> Job:
        output = tmp_path / "github_output"
        output.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_SHA", sha)
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        pypi = PyPI(*answers)
        pauses: list[float] = []
        code = rs.main(lookup=pypi, sleep=pauses.append)
        stdout = capsys.readouterr().out
        return Job(code, output.read_text(encoding="utf-8"), stdout, pypi.asked, pauses)

    return run


def unknown(answer: str) -> str:
    return (
        f"::error::PyPI {answer} for maidr-mcp 0.2.0 on the last of 3 tries, 10 seconds apart, "
        "so whether v0.2.0 is published is unknown. Re-run this job.\n"
    )


def test_a_commit_main_is_still_on_is_versioned_as_usual(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.push()
    ran = job(tested)
    assert (ran.code, ran.outputs, ran.stdout, ran.asked) == (0, "", "", [])


def test_main_moving_on_during_the_tests_hands_the_release_on(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.commit("fix: label the axes")
    repo.push()
    ran = job(tested)
    assert (ran.code, ran.outputs, ran.asked) == (0, "moved=true\n", [])


def test_a_release_tagged_from_this_commit_and_not_on_pypi_is_finished(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.release("0.2.0")
    repo.push()
    ran = job(tested, 404)
    # origin's main is the release commit, which is no reason to hand the release on.
    assert (ran.code, ran.outputs, ran.pauses) == (0, "resume=v0.2.0\n", [])
    # PyPI is asked for the version, without the tag's v.
    assert ran.asked == ["0.2.0"]


def test_a_release_tagged_from_this_commit_and_on_pypi_is_done(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.release("0.2.0")
    repo.push()
    ran = job(tested, 200)
    assert (ran.code, ran.outputs, ran.asked) == (0, "done=true\n", ["0.2.0"])
    assert ran.stdout == (
        "::notice::v0.2.0, released from this commit, is already on PyPI. Nothing to do.\n"
    )


def test_a_later_release_is_not_this_runs(repo, job):
    # v0.2.0 contains the tested commit, but was made from the one after it.
    tested = repo.commit("feat: draw pie charts")
    repo.commit("fix: label the axes")
    repo.release("0.2.0")
    repo.push()
    ran = job(tested)
    assert (ran.code, ran.outputs, ran.asked) == (0, "moved=true\n", [])


def test_a_release_commits_own_tag_is_not_a_release_made_from_it(repo, job):
    # A run on a release commit, main still on it: its tag is the release it is, not one made
    # from it, so the job versions it as usual (and python-semantic-release finds nothing new).
    repo.commit("feat: draw pie charts")
    tested = repo.release("0.2.0")
    repo.push()
    ran = job(tested)
    assert (ran.code, ran.outputs, ran.asked) == (0, "", [])


def test_a_tag_on_a_commit_without_a_parent_is_passed_over(repo, job):
    repo.git("checkout", "--quiet", "--orphan", "elsewhere")
    tested = repo.commit("chore: start again")
    repo.git("tag", "v9.0.0")
    ran = job(tested)
    assert (ran.code, ran.outputs, ran.asked) == (0, "moved=true\n", [])


@pytest.mark.parametrize(
    ("answers", "outputs"),
    [
        ((503, 404), "resume=v0.2.0\n"),
        ((urllib.error.URLError("timed out"), 200), "done=true\n"),
        ((429, 502, 404), "resume=v0.2.0\n"),
    ],
)
def test_pypi_is_asked_again_after_an_answer_the_job_cannot_act_on(repo, job, answers, outputs):
    tested = repo.commit("feat: draw pie charts")
    repo.release("0.2.0")
    repo.push()
    ran = job(tested, *answers)
    assert (ran.code, ran.outputs) == (0, outputs)
    assert ran.asked == ["0.2.0"] * len(answers)
    assert ran.pauses == [10] * (len(answers) - 1)


def test_pypi_never_answering_200_or_404_fails_the_job(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.release("0.2.0")
    repo.push()
    ran = job(tested, 503)
    assert (ran.code, ran.outputs, ran.stdout) == (1, "", unknown("answered 503"))
    assert ran.asked == ["0.2.0"] * 3
    assert ran.pauses == [10, 10]


def test_pypi_out_of_reach_fails_the_job_after_the_tries(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.release("0.2.0")
    repo.push()
    ran = job(tested, urllib.error.URLError("Connection refused"))
    assert (ran.code, ran.outputs, ran.stdout) == (1, "", unknown("did not answer"))
    assert ran.asked == ["0.2.0"] * 3
    assert ran.pauses == [10, 10]


def test_a_failing_git_command_fails_the_job_rather_than_reading_as_main_moved(repo, job):
    tested = repo.commit("feat: draw pie charts")
    repo.push()
    shutil.rmtree(repo.origin)
    ran = job(tested)
    assert (ran.code, ran.outputs) == (1, "")
    assert ran.stdout == (
        "::error::`git ls-remote origin refs/heads/main` failed, so where the release stands "
        "is unknown. Re-run this job.\n"
    )


def test_pypi_status_reads_pypis_json_for_the_version(monkeypatch):
    asked: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url: str, timeout: float):
        asked.append(url)
        if "/0.2.0/" in url:
            return Response()
        code = 404 if "/0.3.0/" in url else 503
        raise urllib.error.HTTPError(url, code, "", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert rs.pypi_status("0.2.0") == 200
    assert rs.pypi_status("0.3.0") == 404
    assert rs.pypi_status("0.4.0") == 503
    assert asked[0] == "https://pypi.org/pypi/maidr-mcp/0.2.0/json"


def test_the_release_job_runs_this_script():
    # The step was shell, untested, once; it stays this one line.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = re.search(
        r"^ +- name: Find where the release stands\n +id: state\n +run: (.*)\n", workflow, re.M
    )
    assert step, "release.yml has no state step"
    assert step[1] == "uv run --no-project python scripts/release_state.py"
    # The later steps read only what the script writes.
    assert set(re.findall(r"steps\.state\.outputs\.(\w+)", workflow)) == {
        "resume",
        "done",
        "moved",
    }
