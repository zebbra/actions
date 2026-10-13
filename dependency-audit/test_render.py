import json
from pathlib import Path

import pytest

from dependency_audit import npm_backend, render, uv_backend
from dependency_audit.model import Ignore

FIXTURES = Path(__file__).parent / "fixtures"
LABELS = {"backend": "backend (uv)", "web": "web (npm)"}


@pytest.fixture(autouse=True)
def no_run_env(monkeypatch):
    for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(key, raising=False)


def uv_findings():
    return uv_backend.parse_pip_audit(json.loads((FIXTURES / "pip-audit-core.json").read_text()), "backend")


def npm_findings():
    report = json.loads((FIXTURES / "npm-audit.json").read_text())
    lock = json.loads((FIXTURES / "npm-package-lock.json").read_text())
    return npm_backend.parse_npm_audit(report, lock, "web")


def rows():
    return [
        render.Row(
            target="backend (uv)",
            package="urllib3",
            from_versions=("2.7.0",),
            to_versions=("2.8.0",),
            ids=("GHSA-8988-9cw3-xx77", "GHSA-gh4c-6fx4-qh6g", "GHSA-vxq7-64xx-v4gw"),
            scope="runtime",
        ),
        render.Row(
            target="web (npm)",
            package="@nestjs/core",
            from_versions=("10.4.1",),
            to_versions=("10.4.20",),
            ids=("GHSA-cj7v-w2c7-cp7c",),
            scope="dev",
        ),
    ]


def issue():
    findings = uv_findings() + npm_findings()
    django = [f for f in findings if f.package == "django"]
    no_fix = [f for f in findings if f.package in ("ecdsa", "ip")]
    return render.issue_body(
        LABELS,
        dry_run=True,
        fixable=rows(),
        blocked=django,
        blockers={f: "neops-core v0.0.0" for f in django},
        no_fix=no_fix,
        now_fixable=[
            Ignore(id="PYSEC-2026-4175", reason="Fixed upstream @maintainer, see #12", issue="zebbra/neops-core#2239")
        ],
        unused=[Ignore(id="GHSA-zzzz-zzzz-zzzz", package="python-jose", reason="Not reachable.")],
        report_only={"web"},
        errors=[("web", "npm audit exited 1 without valid JSON: npm ERR! code E401 @zebbra/sdk")],
    )


def golden(name, content):
    path = FIXTURES / "golden" / name
    assert content == path.read_text(), f"{path} differs; review the rendered output and update the golden file"


def test_pr_body_golden():
    golden("pr-body.md", render.pr_body(rows(), has_triage=True))


def test_issue_body_golden():
    golden("issue-body.md", issue())


def test_no_advisory_text_and_no_mentions():
    for body in (issue(), render.pr_body(rows(), has_triage=True)):
        assert "Third-party text" not in body and "Prototype pollution" not in body
        outside_code = "".join(body.split("`")[::2])
        assert "@" not in outside_code
        assert "#12" not in outside_code and "#2239" not in outside_code


def test_text_escapes_mentions_refs_and_table_breakers():
    assert render.text("hi @team see #12 | <b>") == "hi &#64;team see &#35;12 \\| &lt;b&gt;"
    assert render.code("@nestjs/common") == "`@nestjs/common`"


def test_advisory_links():
    assert (
        render.advisory("GHSA-cj7v-w2c7-cp7c")
        == "[GHSA-cj7v-w2c7-cp7c](https://github.com/advisories/GHSA-cj7v-w2c7-cp7c)"
    )
    assert render.advisory("PYSEC-2026-4175") == "[PYSEC-2026-4175](https://osv.dev/vulnerability/PYSEC-2026-4175)"
    assert render.advisory("1103412") == "1103412"
    assert render.advisory("x](https://evil)") == "x](https://evil)"


def test_truncation_at_65536_links_the_run(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "zebbra/neops-core")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    body = render.cap("| row |\n" * 20000)
    assert len(body) <= 65536
    assert body.endswith("full report: https://github.com/zebbra/neops-core/actions/runs/42_\n")
    assert render.cap("short\n") == "short\n"


def test_empty_bodies():
    assert render.pr_body([], has_triage=True) == ""
    empty = render.issue_body(
        LABELS,
        dry_run=False,
        fixable=rows(),
        blocked=[],
        blockers={},
        no_fix=[],
        now_fixable=[],
        unused=[],
        report_only=set(),
        errors=[],
    )
    assert empty == ""


def test_summaries_list_the_three_tables():
    f = uv_findings()
    summary = render.diff_summary("abc123", LABELS, new=f[:1], pre_existing=f[1:2], ignored=f[2:3])
    assert "1 new finding(s)" in summary
    assert summary.index("### New") < summary.index("### Pre-existing") < summary.index("### Ignored")
    assert "no dependency change" in render.skipped_summary("abc123")
