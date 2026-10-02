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
