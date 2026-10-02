"""What a release publishes agrees with itself: server.json, pyproject.toml, README, image.

python-semantic-release stamps the version into pyproject.toml and server.json, and the MCP
Registry takes the name only from a package that names it too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "io.github.xability/maidr-mcp"
SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"


def server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def project_version() -> str:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\[project\]$.*?^version = "([^"]+)"$', pyproject, re.M | re.S)
    assert match, "pyproject.toml has no [project] version"
    return match.group(1)


def packages() -> dict[str, dict]:
    return {package["registryType"]: package for package in server_json()["packages"]}


def test_server_json_carries_the_project_version_everywhere():
    version = project_version()
    assert server_json()["version"] == version
    assert packages()["pypi"]["version"] == version
    assert packages()["oci"]["identifier"] == f"ghcr.io/xability/maidr-mcp:{version}"


def test_server_json_names_what_the_release_publishes():
    server = server_json()
    assert server["$schema"] == SCHEMA
    assert server["name"] == NAME
    assert packages()["pypi"]["identifier"] == "maidr-mcp"
    # The PyPI package runs over stdio only when asked: by default it serves HTTP.
    assert packages()["pypi"]["transport"] == {"type": "stdio"}
    assert [arg["value"] for arg in packages()["pypi"]["packageArguments"]] == ["--stdio"]
    assert packages()["oci"]["transport"]["url"] == "http://localhost:8000/mcp"


def test_the_package_readme_proves_the_name():
    # PyPI's long description is the README, where the registry looks for this line.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {NAME} -->\n" in readme


def test_the_image_label_proves_the_name():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f'LABEL io.modelcontextprotocol.server.name="{NAME}"\n' in dockerfile


def test_every_action_in_the_release_is_pinned_to_a_commit():
    # The release runs them beside tokens that push tags, publish to PyPI and write the image,
    # ci.yml's among them since the release calls it, so a tag moved upstream must not change
    # what runs.
    workflows = [ROOT / ".github/workflows" / name for name in ("release.yml", "ci.yml")]
    uses = [
        (action, comment)
        for workflow in workflows
        for action, comment in re.findall(
            r"uses: (\S+)(.*)$", workflow.read_text(encoding="utf-8"), re.MULTILINE
        )
        if not action.startswith("./")
    ]
    assert uses
    for action, comment in uses:
        assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", action), action
        assert re.fullmatch(r" # v\d+\.\d+\.\d+", comment), action


def test_the_changelog_leaves_out_chores_and_keeps_the_rest():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    patterns = json.loads(re.search(r"^exclude_commit_patterns = (\[.*\])$", pyproject, re.M)[1])
    left_out = [re.compile(pattern) for pattern in patterns]

    def listed(subject: str) -> bool:
        return not any(pattern.match(subject) for pattern in left_out)

    for subject in [
        "feat: draw pie charts",
        "fix(deps): load maidr.js 4.14.0",
        "feat!: drop the old view",
        "perf: cache the drawn svg",
        "docs: say how to set a token",
    ]:
        assert listed(subject), subject
    for subject in [
        "chore(release): 0.2.0",
        "chore(deps): load py-maidr 1.27.0",
        "chore: tidy",
        "Merge branch 'main'",
    ]:
        assert not listed(subject), subject
