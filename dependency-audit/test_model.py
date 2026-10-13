from dependency_audit.model import (
    Finding,
    Ignore,
    common_ignores,
    fix_target,
    new_findings,
    split_ignored,
    unused_ignores,
    version_key,
)


def finding(
    package="urllib3",
    version="2.7.0",
    advisory="PYSEC-2026-4175",
    aliases=(),
    fixes=("2.8.0",),
    target="backend",
    ecosystem="uv",
):
    return Finding(
        target=target,
        ecosystem=ecosystem,
        package=package,
        version=version,
        advisory=advisory,
        ids=frozenset({advisory, *aliases}),
        fix_versions=tuple(fixes),
    )


def test_ignore_by_ghsa_hides_pysec_finding():
    f = finding(aliases=("GHSA-8988-9cw3-xx77", "CVE-2026-97687"))
    active, ignored = split_ignored([f], [Ignore(id="GHSA-8988-9cw3-xx77", reason="r")])
    assert active == [] and ignored == [f]


def test_ignore_package_narrows_match_with_pep503_names():
    f = finding(package="python-jose", advisory="CVE-2026-85394")
    assert Ignore(id="CVE-2026-85394", reason="r", package="Python_Jose").matches(f)
    assert not Ignore(id="CVE-2026-85394", reason="r", package="ecdsa").matches(f)


def test_npm_package_names_are_not_normalized():
    f = finding(package="@nestjs/common", advisory="GHSA-aaaa-bbbb-cccc", ecosystem="npm")
    assert Ignore(id="GHSA-aaaa-bbbb-cccc", reason="r", package="@nestjs/common").matches(f)
    assert not Ignore(id="GHSA-aaaa-bbbb-cccc", reason="r", package="@NestJS/common").matches(f)


def test_version_bump_is_not_new():
    base = [finding(version="2.6.0")]
    head = [finding(version="2.7.0")]
    assert new_findings(head, base) == []


def test_rekeyed_id_is_not_new():
    base = [finding(advisory="PYSEC-2026-4175", aliases=("GHSA-8988-9cw3-xx77",))]
    head = [finding(advisory="GHSA-8988-9cw3-xx77", aliases=("CVE-2026-97687",))]
    assert new_findings(head, base) == []


def test_added_package_is_new():
    base = [finding()]
    added = finding(package="multidict", version="6.7.1", advisory="CVE-2026-104874", fixes=("6.9.1",))
    assert new_findings([finding(), added], base) == [added]


def test_same_advisory_in_another_target_is_new():
    other = finding(target="python-graphql")
    assert new_findings([other], [finding()]) == [other]


def test_common_ignores_need_id_and_package_on_both_sides():
    base = [Ignore(id="A", reason="old"), Ignore(id="B", reason="r", package="x")]
    head = [Ignore(id="A", reason="new"), Ignore(id="B", reason="r", package="y"), Ignore(id="C", reason="r")]
    assert [i.key for i in common_ignores(base, head)] == [("A", None)]


def test_unused_ignores():
    used = Ignore(id="PYSEC-2026-4175", reason="r")
    unused = Ignore(id="GHSA-zzzz-zzzz-zzzz", reason="r")
    assert unused_ignores([used, unused], [finding()]) == [unused]


def test_fix_target_is_max_over_advisories_of_min_fix_above_installed():
    findings = [
        finding(package="django", version="4.2.30", advisory="A", fixes=("5.2.15", "6.0.6")),
        finding(package="django", version="4.2.30", advisory="B", fixes=("6.0.8", "5.2.17", "4.2.1")),
        finding(package="django", version="4.2.30", advisory="C", fixes=()),
    ]
    assert fix_target(findings) == "5.2.17"
    assert fix_target([finding(fixes=())]) is None


def test_version_key_orders_pep440_versions():
    ordered = ["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.0.1", "1.10"]
    assert sorted(reversed(ordered), key=version_key) == ordered
    assert version_key("2.0") == version_key("2.0.0")


def test_display_id_prefers_ghsa():
    assert finding(aliases=("GHSA-8988-9cw3-xx77", "CVE-1")).display_id == "GHSA-8988-9cw3-xx77"
    assert finding(advisory="PYSEC-1").display_id == "PYSEC-1"
