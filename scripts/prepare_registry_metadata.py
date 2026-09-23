"""Use the PyPI package version for MCP Registry release metadata."""

import json
import tomllib
from pathlib import Path


def prepare_registry_metadata(root: Path) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    path = root / "server.json"
    server = json.loads(path.read_text(encoding="utf-8"))
    package = server["packages"][0]
    if package["identifier"] != project["name"]:
        raise ValueError("Registry package name must match PyPI")
    readme = (root / project["readme"]).read_text(encoding="utf-8")
    if "mcp-name: " + server["name"] not in readme:
        raise ValueError("PyPI README must contain the ownership marker")
    server["version"] = package["version"] = project["version"]
    path.write_text(json.dumps(server, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Prepared MCP Registry metadata: {server['name']} {project['version']}")


if __name__ == "__main__":
    prepare_registry_metadata(Path(__file__).resolve().parents[1])
