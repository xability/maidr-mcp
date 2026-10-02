"""scripts/update_maidr.py, which update-maidr.yml runs to raise the two maidr pins."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("update_maidr", ROOT / "scripts/update_maidr.py")
assert _spec and _spec.loader
um = importlib.util.module_from_spec(_spec)
sys.modules["update_maidr"] = um  # dataclasses look their module up here
_spec.loader.exec_module(um)

WORKFLOW = ROOT / ".github/workflows/update-maidr.yml"
SERVER_SOURCE = (ROOT / um.SERVER).read_text(encoding="utf-8")
LOCK_SOURCE = (ROOT / um.LOCK).read_text(encoding="utf-8")
JS_PIN = um.read_pin(SERVER_SOURCE)
PY_LOCKED = um.locked_py_maidr(LOCK_SOURCE)


def bumped(version: str) -> str:
    major, minor, _ = version.split(".")
    return f"{major}.{int(minor) + 1}.0"


JS_NEXT = bumped(JS_PIN)
PY_NEXT = bumped(PY_LOCKED)


def npm(
    version: str,
    *,
    provenance=True,
    repository=um.MAIDR_REPOSITORY,
    path=um.MAIDR_RELEASE_WORKFLOW,
    ref=um.MAIDR_RELEASE_REF,
    built=b"tarball",
):
    """What the registry serves for a maidr release: its document and its attestations."""
    workflow = {"repository": repository, "path": path, "ref": ref}
    served = hashlib.sha512(b"tarball").digest()
    statement = {
        "subject": [
            {
                "name": f"pkg:npm/maidr@{version}",
                "digest": {"sha512": hashlib.sha512(built).hexdigest()},
            }
        ],
        "predicate": {"buildDefinition": {"externalParameters": {"workflow": workflow}}},
    }
    dist = {"integrity": "sha512-" + base64.b64encode(served).decode()}
    if provenance:
        dist["attestations"] = {"provenance": {"predicateType": um.SLSA_PROVENANCE}}
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return {
        f"{um.REGISTRY}/maidr/{version}": {"name": "maidr", "version": version, "dist": dist},
        f"{um.REGISTRY}/-/npm/v1/attestations/maidr@{version}": {
            "attestations": [
                {
                    "predicateType": um.SLSA_PROVENANCE,
                    "bundle": {"dsseEnvelope": {"payload": payload}},
                }
            ]
        },
    }


class Registry:
    """A fake fetch: serves `docs`, and remembers what it was asked for."""

    def __init__(self, latest: str = JS_PIN, docs: dict | None = None):
        self.docs = {f"{um.REGISTRY}/maidr/latest": {"version": latest}, **(docs or {})}
        self.asked: list[str] = []

    def __call__(self, url: str):
        self.asked.append(url)
        if url not in self.docs:
            raise um.Refused(f"could not read {url}")
        return self.docs[url]


class Locker:
    """A fake `uv lock`: moves maidr to `to`, and can rewrite the lock without moving it."""

    def __init__(self, to: str = PY_LOCKED, reformat: bool = False):
        self.to, self.reformat = to, reformat
        self.asked: list[str] = []

    def __call__(self, root: Path, requirement: str):
        self.asked.append(requirement)
        lock = (root / um.LOCK).read_text(encoding="utf-8")
        lock = lock.replace(
            f'name = "maidr"\nversion = "{PY_LOCKED}"', f'name = "maidr"\nversion = "{self.to}"'
        )
        if self.reformat:
            lock = lock.replace("revision = 3", "revision = 4")
        (root / um.LOCK).write_text(lock, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for path in (um.SERVER, um.LOCK):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    return tmp_path


def read(repo: Path, path: Path) -> bytes:
    return (repo / path).read_bytes()


# Versions


@pytest.mark.parametrize(
    "version",
    ["4.13.0", "0.0.0", "10.20.30", "1.0.0-rc.1", "1.0.0-alpha-1", "1.0.0-0.3.7", "1.0.0-x.7.z.92"],
)
def test_npm_versions_are_accepted(version):
    assert um.checked(version, "v") == version


@pytest.mark.parametrize(
    "value",
    [
        "",
        "4.13",
        "4.13.0.1",
        "v4.13.0",
        "04.13.0",
        "4.013.0",
        "1.0.0-01",
        "1.0.0-",
        "1.0.0-a..b",
        "1.0.0+build.1",  # npm drops build metadata from what it publishes
        "latest",
        "4.13.0\n",
        "4.13.0 ",
        " 4.13.0",
        "4.13.0;id",
        '4.13.0"; import os #',
        "$(curl evil.example)",
        "`id`",
        "4.13.0/../../etc/passwd",
        "4.13.0?x=1",
        "４.13.0",  # fullwidth digit
        "4.1٣.0",  # Arabic-Indic digit, which \d would take
        "1." + "0" * 100 + ".0",
        "1.0.0-" + "a" * 70,
        None,
        4.13,
        ["4.13.0"],
        {"version": "4.13.0"},
    ],
)
def test_anything_but_an_npm_version_is_refused(value):
    with pytest.raises(um.Refused):
        um.checked(value, "the requested maidr.js version")


def test_a_refusal_never_quotes_the_value_and_logs_it_on_one_line(capsys):
    hostile = "4.14.0\n::error::forged\r\n::set-output name=x::y"
    with pytest.raises(um.Refused) as refused:
        um.checked(hostile, "the requested maidr.js version")
    assert "forged" not in str(refused.value) and "4.14.0" not in str(refused.value)
    err = capsys.readouterr().err
    assert "forged" in err  # in the log, for whoever debugs it
    assert not any(line.startswith("::") for line in err.splitlines())


@pytest.mark.parametrize("version", ["1.26.0", "1.27", "2.0.0rc1", "1.26.0.post1", "1.27.0.dev3"])
def test_pypi_versions_are_accepted(version):
    assert um.checked(version, "v", um.PYPI_VERSION) == version


@pytest.mark.parametrize("value", ["", "1.26.0; rm -rf /", "1.26.0\n", ">=1.26", "1.26.*", "1!2.0"])
def test_anything_but_a_pypi_version_is_refused(value):
    with pytest.raises(um.Refused):
        um.checked(value, "v", um.PYPI_VERSION)


def test_pypi_versions_sort_by_pep_440():
    ordered = [
        "1.0.dev1",
        "1.0a1.dev1",
        "1.0a1",
        "1.0a2",
        "1.0b1",
        "1.0rc1",
        "1.0rc1.post1",
        "1.0",
        "1.0.post1.dev1",
        "1.0.post1",
        "1.0.1",
        "1.2",
        "1.10",
    ]
    assert sorted(reversed(ordered), key=um.pypi_key) == ordered
    assert um.pypi_key("1.26") == um.pypi_key("1.26.0")
    with pytest.raises(ValueError):
        um.pypi_key("1!2.0")


def test_versions_sort_by_semver_precedence():
    # semver's own example of precedence, and the one plain string order gets wrong.
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    assert sorted(reversed(ordered), key=um.version_key) == ordered
    assert um.version_key("4.9.0") < um.version_key("4.13.0")


# server.py


def test_writing_the_pin_changes_that_line_and_nothing_else():
    rewritten = um.write_pin(SERVER_SOURCE, "4.99.0")
    before, after = SERVER_SOURCE.splitlines(keepends=True), rewritten.splitlines(keepends=True)
    assert len(before) == len(after)
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert changed == [(f'MAIDR_JS_VERSION = "{JS_PIN}"\n', 'MAIDR_JS_VERSION = "4.99.0"\n')]
    assert um.read_pin(rewritten) == "4.99.0"


def test_the_pin_must_be_set_exactly_once():
    with pytest.raises(um.Refused):
        um.read_pin(SERVER_SOURCE.replace("MAIDR_JS_VERSION = ", "MAIDR_JS = "))
    with pytest.raises(um.Refused):
        um.write_pin(SERVER_SOURCE + f'MAIDR_JS_VERSION = "{JS_PIN}"\n', "4.99.0")


def test_writing_the_pin_refuses_what_is_not_a_version():
    with pytest.raises(um.Refused):
        um.write_pin(SERVER_SOURCE, '4.99.0"\nimport os\nX = "')


# uv.lock


def test_the_lock_is_read_per_package():
    versions = um.lock_versions(LOCK_SOURCE)
    assert versions["maidr"] == (PY_LOCKED,)
    assert "maidr-mcp" in versions and "matplotlib" in versions
    forked = '[[package]]\nname = "a"\nversion = "1"\n\n[[package]]\nname = "a"\nversion = "2"\n'
    assert um.lock_versions(forked) == {"a": ("1", "2")}


def test_moved_packages_leave_out_maidr():
    before = (
        '[[package]]\nname = "maidr"\nversion = "1.26.0"\n\n'
        '[[package]]\nname = "lxml"\nversion = "5.3.0"\n'
    )
    after = before.replace("1.26.0", "1.27.0").replace("5.3.0", "5.4.0")
    assert um.moved_packages(before, after) == [("lxml", "5.3.0", "5.4.0")]


# update


def test_nothing_new_changes_nothing(repo):
    registry, locker = Registry(), Locker()
    change = um.update(repo, fetch=registry, lock=locker)
    assert not change.changed
    assert read(repo, um.SERVER) == read(ROOT, um.SERVER)
    assert read(repo, um.LOCK) == read(ROOT, um.LOCK)
    assert registry.asked == [f"{um.REGISTRY}/maidr/latest"]
    assert locker.asked == ["maidr"]
    assert change.outputs()["changed"] == "false"


def test_a_lock_uv_only_rewrote_is_put_back(repo):
    change = um.update(repo, fetch=Registry(), lock=Locker(reformat=True))
    assert not change.changed
    assert read(repo, um.LOCK) == read(ROOT, um.LOCK)


def test_a_new_maidr_js_is_written_into_server_py(repo):
    change = um.update(repo, fetch=Registry(JS_NEXT, npm(JS_NEXT)), lock=Locker())
    assert (change.js_old, change.js_new, change.py_new) == (JS_PIN, JS_NEXT, PY_LOCKED)
    assert um.read_pin(read(repo, um.SERVER).decode()) == JS_NEXT
    assert read(repo, um.LOCK) == read(ROOT, um.LOCK)
    assert (
        change.outputs()["issue_title"]
        == f"Automatic maidr update to maidr.js {JS_NEXT} / py-maidr {PY_LOCKED} failed"
    )


def test_a_new_py_maidr_is_locked(repo):
    change = um.update(repo, fetch=Registry(), lock=Locker(to=PY_NEXT))
    assert (change.py_old, change.py_new, change.js_new) == (PY_LOCKED, PY_NEXT, JS_PIN)
    assert um.locked_py_maidr(read(repo, um.LOCK).decode()) == PY_NEXT
    assert read(repo, um.SERVER) == read(ROOT, um.SERVER)


def test_a_requested_py_maidr_is_locked_exactly(repo):
    locker = Locker(to=PY_NEXT)
    um.update(repo, py_maidr=PY_NEXT, fetch=Registry(), lock=locker)
    assert locker.asked == [f"maidr=={PY_NEXT}"]


def test_a_requested_maidr_js_is_used_rather_than_npm_latest(repo):
    registry = Registry(latest="9.9.9")
    change = um.update(repo, maidr_js=JS_NEXT, fetch=registry, lock=Locker())
    assert change.js_new == JS_NEXT
    assert f"{um.REGISTRY}/maidr/latest" not in registry.asked


def test_an_invalid_request_is_refused_before_anything_is_fetched_or_locked(repo):
    registry, locker = Registry(), Locker()
    with pytest.raises(um.Refused):
        um.update(repo, maidr_js="4.14.0; curl evil.example", fetch=registry, lock=locker)
    with pytest.raises(um.Refused):
        um.update(
            repo, py_maidr="1.27.0 --index-url=https://evil.example", fetch=registry, lock=locker
        )
    assert registry.asked == [] and locker.asked == []
    assert read(repo, um.SERVER) == read(ROOT, um.SERVER)


def test_the_pin_is_never_lowered_unless_asked_by_hand(repo):
    lower = "4.0.0"
    # A registry replica behind on a release, or a dispatch naming an older one.
    assert not um.update(repo, fetch=Registry(latest=lower), lock=Locker()).changed
    assert not um.update(repo, maidr_js=lower, fetch=Registry(), lock=Locker()).changed
    # Only an explicit version, with allow_lower, from a hand-run.
    unasked = um.update(repo, allow_lower=True, fetch=Registry(latest=lower), lock=Locker())
    assert not unasked.changed
    change = um.update(repo, maidr_js=lower, allow_lower=True, fetch=Registry(), lock=Locker())
    assert change.js_new == lower
    assert um.read_pin(read(repo, um.SERVER).decode()) == lower


def test_the_digest_follows_both_files(repo):
    same = um.update(repo, fetch=Registry(), lock=Locker()).digest
    assert same == um.files_digest(ROOT)
    moved = um.update(repo, fetch=Registry(JS_NEXT, npm(JS_NEXT)), lock=Locker(to=PY_NEXT))
    assert moved.digest not in (same, "")


# Provenance


def test_a_release_with_maidr_provenance_passes():
    um.check_provenance(JS_NEXT, Registry(docs=npm(JS_NEXT)))


@pytest.mark.parametrize(
    "served",
    [
        npm(JS_NEXT, provenance=False),  # published without --provenance
        npm(JS_NEXT, repository="https://github.com/someone/maidr"),  # from another repository
        npm(JS_NEXT, path=".github/workflows/someone.yml"),  # by another of maidr's workflows
        npm(JS_NEXT, ref="refs/heads/someone"),  # from another branch of maidr's
        npm(JS_NEXT, path=None),  # by a workflow the statement does not name
        npm(JS_NEXT, built=b"another tarball"),  # for a tarball npm does not serve
    ],
)
def test_a_release_without_maidr_provenance_is_refused(served):
    with pytest.raises(um.Refused) as refused:
        um.check_provenance(JS_NEXT, Registry(docs=served))
    assert JS_NEXT in str(refused.value)
    assert "someone" not in str(refused.value)


def test_provenance_for_another_version_is_refused():
    served = npm(JS_NEXT)
    served[f"{um.REGISTRY}/-/npm/v1/attestations/maidr@{JS_NEXT}"] = npm("4.0.0")[
        f"{um.REGISTRY}/-/npm/v1/attestations/maidr@4.0.0"
    ]
    with pytest.raises(um.Refused):
        um.check_provenance(JS_NEXT, Registry(docs=served))


# The commit and the issue


def test_the_commit_message_names_what_changed():
    both = um.Update(JS_PIN, JS_NEXT, PY_LOCKED, PY_NEXT, "d", [("lxml", "5.3.0", "5.4.0")])
    message = um.commit_message(both, "https://github.com/xability/maidr-mcp/actions/runs/1")
    subject, *body = message.splitlines()
    assert subject == f"fix(deps): load maidr.js {JS_NEXT} and py-maidr {PY_NEXT}"
    text = " ".join(body)
    assert f"maidr.js {JS_PIN} -> {JS_NEXT}" in text
    assert f"py-maidr {PY_LOCKED} -> {PY_NEXT}" in text
    assert "lxml 5.3.0 -> 5.4.0" in text
    assert "actions/runs/1" in text
    assert all(len(line) <= 72 or "https://" in line for line in body)

    only_js = um.Update(JS_PIN, JS_NEXT, PY_LOCKED, PY_LOCKED, "d")
    assert um.commit_message(only_js).splitlines()[0] == f"fix(deps): load maidr.js {JS_NEXT}"
    only_py = um.Update(JS_PIN, JS_PIN, PY_LOCKED, PY_NEXT, "d")
    assert um.commit_message(only_py).splitlines()[0] == f"fix(deps): load py-maidr {PY_NEXT}"
    with pytest.raises(ValueError):
        um.commit_message(um.Update(JS_PIN, JS_PIN, PY_LOCKED, PY_LOCKED, "d"))


def test_issue_titles_are_stable_and_read_back():
    title = um.issue_title("4.14.0", "1.27.0")
    assert title == "Automatic maidr update to maidr.js 4.14.0 / py-maidr 1.27.0 failed"
    assert um.PAIR_TITLE.fullmatch(title).groups() == ("4.14.0", "1.27.0")
    unchosen = um.issue_title("", "")
    assert unchosen == "Automatic maidr update failed before choosing the versions"
    assert not um.PAIR_TITLE.fullmatch(unchosen)
    # The workflow looks for that issue by its title, to have `report` close it.
    assert f"UNCHOSEN: {unchosen}\n" in WORKFLOW.read_text(encoding="utf-8")


def test_the_issue_names_the_versions_the_step_and_the_run():
    title, body = um.issue(
        step="e2e",
        js_old=JS_PIN,
        js_new=JS_NEXT,
        py_old=PY_LOCKED,
        py_new=PY_LOCKED,
        run_url="https://github.com/xability/maidr-mcp/actions/runs/7",
    )
    assert title == um.issue_title(JS_NEXT, PY_LOCKED)
    assert f"| {JS_PIN} | {JS_NEXT} |" in body
    assert "`bash e2e/run.sh`" in body and "RunnableCommand" in body
    assert "(https://github.com/xability/maidr-mcp/actions/runs/7)" in body


@pytest.mark.parametrize(
    "step", ["provenance", "signatures", "lint", "format", "pytest", "e2e", "push"]
)
def test_a_failure_about_the_versions_holds_them(step):
    _, body = um.issue(step=step, js_old=JS_PIN, js_new=JS_NEXT, py_old=PY_LOCKED, py_new=PY_LOCKED)
    assert body.rstrip().endswith(um.HOLD_MARKER)
    assert "leaves these versions alone" in body


@pytest.mark.parametrize("step", ["gate", "fetch", "sync", "browser", "land", "", "cancelled"])
def test_a_failure_that_says_nothing_about_the_versions_holds_nothing(step):
    _, body = um.issue(step=step, js_old=JS_PIN, js_new=JS_NEXT, py_old=PY_LOCKED, py_new=PY_LOCKED)
    assert um.HOLD_MARKER not in body
    assert "tries them again" in body and "a day after this issue was opened" in body


def test_a_registry_that_could_not_be_read_holds_nothing(monkeypatch, tmp_path):
    def unreachable(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(um.urllib.request, "urlopen", unreachable)
    monkeypatch.setattr(um.time, "sleep", lambda _: None)
    with pytest.raises(um.Refused) as refused:
        um.fetch_json(f"{um.REGISTRY}/maidr/latest")
    assert refused.value.passing

    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert um.main(["provenance", "--maidr-js", JS_NEXT]) == 1
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs["passing"] == "true"
    _, body = um.issue(
        step="provenance",
        reason=outputs["reason"],
        passing=True,
        js_old=JS_PIN,
        js_new=JS_NEXT,
        py_old=PY_LOCKED,
        py_new=PY_LOCKED,
    )
    assert um.HOLD_MARKER not in body


def test_a_refused_provenance_is_not_a_passing_fault():
    with pytest.raises(um.Refused) as refused:
        um.check_provenance(JS_NEXT, Registry(docs=npm(JS_NEXT, provenance=False)))
    assert not refused.value.passing


def test_the_issue_about_a_run_that_could_not_choose_holds_nothing():
    title, body = um.issue(step="update", reason="could not read x", passing=True)
    assert title == um.issue_title("", "")
    assert um.HOLD_MARKER not in body and "once a run chooses the versions" in body


def test_every_step_that_can_fail_is_named():
    ids = set(
        re.findall(r"^\s+(?:- )?id: (\w+)$", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    )
    # Every step of `check` and `land` but the one that names the failed step; `report`'s own
    # steps (issue, open) report nothing.
    assert ids - {"failed", "issue", "open"} == set(um.STEPS)


def test_the_workflow_holds_by_the_marker_the_issue_ends_with():
    assert f'HOLD: "{um.HOLD_MARKER}"\n' in WORKFLOW.read_text(encoding="utf-8")


def test_the_issue_gives_the_reason_on_one_line():
    _, body = um.issue(step="provenance", reason="maidr.js 4.14.0 carries no npm provenance\n\n#")
    assert "Why: maidr.js 4.14.0 carries no npm provenance #." in body


def test_the_issue_drops_whatever_it_cannot_check():
    title, body = um.issue(
        step="<img src=x onerror=alert(1)>",
        js_old="4.13.0",
        js_new="4.14.0](https://evil.example)",
        py_old="1.26.0",
        py_new="1.27.0",
    )
    assert title == um.issue_title("", "")
    assert "evil" not in body and "<img" not in body
    assert "a step that did not record its name" in body


# Closing the issues a run settles


def open_issues(*titles: str) -> list[dict]:
    return [{"number": n, "title": title} for n, title in enumerate(titles, start=1)]


SHA = "0123456789abcdef0123456789abcdef01234567"


def test_a_run_that_chose_the_versions_settles_the_issue_about_one_that_could_not():
    issues = open_issues(um.issue_title("", ""), um.issue_title(JS_NEXT, PY_LOCKED))
    # Nothing landed: only the first issue is settled.
    settled = um.resolved(issues, run_url="https://github.com/o/r/actions/runs/3")
    assert [s["number"] for s in settled] == [1]
    assert settled[0]["comment"] == (
        "[A later run](https://github.com/o/r/actions/runs/3) chose the versions again, so this "
        "report is out of date."
    )


def test_a_landed_update_settles_the_pairs_no_newer_than_it():
    issues = open_issues(
        um.issue_title(JS_PIN, PY_NEXT),  # 1: what landed
        um.issue_title(JS_PIN, PY_LOCKED),  # 2: older py-maidr
        um.issue_title("4.0.0", PY_LOCKED),  # 3: older in both
        um.issue_title(JS_NEXT, PY_LOCKED),  # 4: a newer maidr.js, left by a lower one by hand
        um.issue_title(JS_PIN, bumped(PY_NEXT)),  # 5: a newer py-maidr
        um.issue_title("", ""),  # 6: a run that could not choose
        "Automatic maidr update to maidr.js 4.x / py-maidr 1.27.0 failed",  # 7: not versions
        "Something else",  # 8
    )
    settled = um.resolved(issues, landed_js=JS_PIN, landed_py=PY_NEXT, sha=SHA)
    assert [s["number"] for s in settled] == [1, 2, 3, 6]
    assert f"maidr.js {JS_PIN} and locks py-maidr {PY_NEXT} ({SHA})" in settled[0]["comment"]


def test_a_reported_failure_settles_the_older_pairs_no_run_tries_again():
    issues = open_issues(
        um.issue_title(JS_NEXT, PY_LOCKED),  # 1: the same maidr.js with an older py-maidr
        um.issue_title(JS_NEXT, PY_NEXT),  # 2: the pair just reported
        um.issue_title(JS_PIN, PY_LOCKED),  # 3: older in both
        um.issue_title(bumped(JS_NEXT), PY_LOCKED),  # 4: a newer maidr.js, given by hand
    )
    settled = um.resolved(issues, reported_js=JS_NEXT, reported_py=PY_NEXT, reported_issue="2")
    assert [s["number"] for s in settled] == [1, 3]
    assert "#2 reports how that failed" in settled[0]["comment"]
    # Without the number of the issue that reports it, a failure settles nothing.
    for number in ("", "0", "2; x", "#2"):
        assert (
            um.resolved(issues, reported_js=JS_NEXT, reported_py=PY_NEXT, reported_issue=number)
            == []
        )


def test_a_held_maidr_js_says_how_to_raise_py_maidr_alone():
    _, body = um.issue(step="e2e", js_old=JS_PIN, js_new=JS_NEXT, py_old=PY_LOCKED, py_new=PY_NEXT)
    assert f"run **update-maidr** by hand with maidr.js {JS_PIN}" in body
    _, only_py = um.issue(
        step="e2e", js_old=JS_PIN, js_new=JS_PIN, py_old=PY_LOCKED, py_new=PY_NEXT
    )
    assert "py-maidr alone" not in only_py


def test_only_what_it_can_check_settles_an_issue():
    pair = um.issue_title(JS_PIN, PY_LOCKED)
    hostile = [
        {"number": "1", "title": pair},
        {"number": True, "title": pair},
        {"number": 3, "title": None},
        "4",
        None,
    ]
    assert um.resolved(hostile, landed_js=JS_NEXT, landed_py=PY_NEXT) == []
    assert um.resolved({"number": 1, "title": pair}, landed_js=JS_NEXT, landed_py=PY_NEXT) == []
    # A landed version that is not one settles no pair, and a bad sha is left out.
    assert um.resolved(open_issues(pair), landed_js="latest", landed_py=PY_NEXT) == []
    settled = um.resolved(open_issues(pair), landed_js=JS_NEXT, landed_py=PY_NEXT, sha="x`y")
    assert "x`y" not in settled[0]["comment"]


def test_the_resolved_command_reads_issues_on_stdin_and_prints_json(monkeypatch, capsys):
    issues = open_issues(um.issue_title(JS_PIN, PY_LOCKED), "Something else")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(issues)))
    argv = ["resolved", "--landed-js", JS_NEXT, "--landed-py", PY_LOCKED, "--sha", SHA]
    assert um.main(argv) == 0
    assert [s["number"] for s in json.loads(capsys.readouterr().out)] == [1]
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert um.main(["resolved"]) == 1


# The command line, as the workflow runs it


def test_an_invalid_dispatch_fails_with_a_reason_for_the_issue(repo, tmp_path, monkeypatch):
    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    code = um.main(["--root", str(repo), "update", "--maidr-js", "4.14.0\n::stop-commands::x"])
    assert code == 1
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs == {"reason": "the requested maidr.js version is not a version number"}


def test_the_report_writes_the_body_and_hands_on_the_title(tmp_path, monkeypatch):
    out, body = tmp_path / "output", tmp_path / "issue.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    for key, value in [
        ("GITHUB_SERVER_URL", "https://github.com"),
        ("GITHUB_REPOSITORY", "o/r"),
        ("GITHUB_RUN_ID", "9"),
    ]:
        monkeypatch.setenv(key, value)
    argv = ["report", "--step", "pytest", "--js-old", "4.13.0", "--js-new", "4.13.0"]
    argv += ["--py-old", "1.26.0", "--py-new", "1.27.0", "--body-file", str(body)]
    assert um.main(argv) == 0
    assert out.read_text() == f"title={um.issue_title('4.13.0', '1.27.0')}\n"
    assert "(https://github.com/o/r/actions/runs/9)" in body.read_text()


# The workflow


def test_the_workflow_passes_untrusted_input_through_env_only():
    # inputs and client_payload are written by whoever starts the run. Spliced into a run:
    # script as ${{ ... }} they would be shell; as the value of an env: entry they are data.
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    untrusted = [
        line
        for line in lines
        if "${{" in line and re.search(r"\binputs\.|client_payload|github\.event\.", line)
    ]
    assert untrusted
    env_entry = re.compile(r"^\s+[A-Z][A-Z0-9_]*: \$\{\{ .* \}\}$")
    assert [line for line in untrusted if not env_entry.match(line)] == []


def test_no_job_shares_a_uv_cache():
    # setup-uv caches on GitHub's runners unless told not to, and `check` would then save a cache
    # its untrusted code had written under the very key `land` restores before it runs uv lock.
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = re.findall(r"uses: astral-sh/setup-uv@\S+(?: #.*)?\n((?: {8,}.*\n)*)", text)
    assert len(steps) == 3
    assert all(re.search(r"^\s+enable-cache: false$", step, re.MULTILINE) for step in steps)


def test_every_action_is_pinned_to_a_commit():
    # They run beside a token that can push to main or write issues, so a moved tag must not
    # change what runs.
    uses = re.findall(r"uses: (\S+)(.*)$", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(uses) == 10
    for action, comment in uses:
        assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", action), action
        assert re.fullmatch(r" # v\d+\.\d+\.\d+", comment), action
