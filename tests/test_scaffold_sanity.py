"""Sanity test: confirms pytest collects + runs before any supervisor code exists.
Delete or replace once OLB-01 lands its first real component suite."""


def test_pytest_scaffold_is_live():
    assert True


def test_supervisor_dir_resolves(supervisor_dir):
    # supervisor/ exists (created at scaffold time); it may be empty pre-OLB-01.
    assert supervisor_dir.name == "supervisor"
