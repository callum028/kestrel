import pytest

from kestrel.observations import (
    MAX_FIXES_IN_PLACE,
    ObservationState,
    ObservationStore,
    ProposedFix,
    may_fix_in_place,
)

TASK_FILES = {"src/auth/token.ts", "src/auth/refresh.ts"}


@pytest.fixture
def observations(conn, log):
    return ObservationStore(conn, log)


def typo_fix(**kw) -> ProposedFix:
    base = {"category": "typo", "files": ["src/auth/token.ts"], "lines_changed": 1}
    base.update(kw)
    return ProposedFix(**base)


def test_raising_the_same_thing_twice_is_silent(observations):
    first = observations.raise_("Dead import of legacySync", "src/auth/token.ts")
    second = observations.raise_("Dead import of legacySync", "src/auth/token.ts")
    assert first is not None
    assert second is None


def test_dismissal_is_durable(observations):
    obs = observations.raise_("Migration script fails on clean checkout", "scripts/migrate.sh")
    observations.resolve(obs.id, ObservationState.DISMISSED)

    # Raised again by a later task - must stay quiet, or it becomes nagging.
    assert (
        observations.raise_("Migration script fails on clean checkout", "scripts/migrate.sh")
        is None
    )
    assert observations.open() == []


def test_fingerprint_ignores_which_task_noticed_it(observations):
    assert observations.raise_("Dead code in helpers", "src/util.ts", task_id="t1") is not None
    assert observations.raise_("Dead code in helpers", "src/util.ts", task_id="t2") is None


def test_a_typo_in_a_touched_file_may_be_fixed():
    assert may_fix_in_place(typo_fix(), TASK_FILES, 0).status == "ok"


def test_behavioural_categories_are_never_trivial():
    result = may_fix_in_place(typo_fix(category="null_guard"), TASK_FILES, 0)
    assert result.status == "refused"
    assert "change behaviour" in result.reason


def test_files_outside_the_task_are_refused():
    result = may_fix_in_place(typo_fix(files=["src/billing/invoice.ts"]), TASK_FILES, 0)
    assert result.status == "refused"


def test_big_changes_are_refused_however_dull():
    assert may_fix_in_place(typo_fix(lines_changed=40), TASK_FILES, 0).status == "refused"


def test_dependencies_config_and_schema_are_off_limits():
    assert may_fix_in_place(typo_fix(adds_dependency=True), TASK_FILES, 0).status == "refused"
    assert (
        may_fix_in_place(
            typo_fix(files=["pyproject.toml"]), TASK_FILES | {"pyproject.toml"}, 0
        ).status
        == "refused"
    )


def test_test_expectations_are_off_limits():
    assert (
        may_fix_in_place(typo_fix(changes_test_expectations=True), TASK_FILES, 0).status
        == "refused"
    )


def test_three_small_fixes_is_scope_creep_however_small_each_one_is():
    assert may_fix_in_place(typo_fix(), TASK_FILES, MAX_FIXES_IN_PLACE - 1).status == "ok"
    result = may_fix_in_place(typo_fix(), TASK_FILES, MAX_FIXES_IN_PLACE)
    assert result.status == "refused"
    assert "already made" in result.reason
