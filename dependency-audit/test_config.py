from pathlib import Path

import pytest

from dependency_audit.config import Exclude, Target, load, parse
from dependency_audit.model import ConfigError, Ignore

FIXTURES = Path(__file__).parent / "fixtures"


def test_valid_config_with_defaults():
    config = load(FIXTURES / "config-valid.toml")
    assert config.targets == (
        Target(name="backend", ecosystem="uv", path="backend", scope="all"),
        Target(name="sdk", ecosystem="uv", path=".", groups=("fb",), fail_on="none"),
        Target(name="client", ecosystem="npm", path="client", fail_on="none"),
    )
    assert config.targets[1].lockfile == "uv.lock"
    assert config.targets[2].lockfile == "client/package-lock.json"
    assert config.excludes == (Exclude(glob="examples/**", reason="Example projects are not shipped."),)
    assert config.ignores[0] == Ignore(
        id="GHSA-3qf3-8w2g-rqmx",
        package="python-jose",
        reason="Not reachable: only the realm JWKS dict is passed as key.",
        issue="zebbra/neops-core#2239",
    )


@pytest.mark.parametrize(
    "name, message",
    [("config-unknown-key.toml", "unknown key(s): failon"), ("config-missing-reason.toml", "`reason` is required")],
)
def test_invalid_config_fixtures(name, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load(FIXTURES / name)


def target(**overrides):
    return {"name": "backend", "ecosystem": "uv", "path": "backend", **overrides}


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "at least one"),
        ({"target": [target(), target()]}, "duplicate target name"),
        ({"target": [target(ecosystem="pip")]}, "`ecosystem` must be one of"),
        ({"target": [target(scope="dev")]}, "`scope` must be one of"),
        ({"target": [target(**{"fail-on": "high"})]}, "`fail-on` must be one of"),
        ({"target": [target(ecosystem="npm", groups=["fb"])]}, "uv targets only"),
        ({"target": [target(path="../other")]}, "relative to the repository root"),
        ({"target": [target()], "ignores": []}, "unknown key"),
        ({"target": [target()], "exclude": [{"glob": "x/**"}]}, "`reason` is required"),
        ({"target": [target()], "ignore": [{"id": "X", "reason": " "}]}, "`reason` is required"),
        ({"target": [target()], "ignore": [{"id": "X", "reason": "r", "expires": "2027-01-01"}]}, "unknown key"),
        ({"target": {"name": "x"}}, "array of tables"),
    ],
)
def test_invalid_configs(data, message):
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "dependency-audit.toml")
