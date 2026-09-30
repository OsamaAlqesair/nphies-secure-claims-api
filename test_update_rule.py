"""Utility tests use only disposable SQLite, never configured databases."""

import pytest
from sqlalchemy import create_engine, select, event
from sqlalchemy.orm import sessionmaker
from models import (
    Base,
    DiagnosisCode,
    ServiceCode,
    InsuranceCompany,
    DiagnosisServiceRule,
)
from update_rule import main


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as db:
        db.add_all(
            [
                DiagnosisCode(id=1, code="TEST-D", description="Synthetic"),
                ServiceCode(id=1, code="TEST-S", description="Synthetic"),
                ServiceCode(id=2, code="OTHER", description="Synthetic"),
                InsuranceCompany(id=1, name="Synthetic"),
                InsuranceCompany(id=2, name="Other"),
            ]
        )
        db.flush()
        db.add_all(
            [
                DiagnosisServiceRule(
                    id=1, diagnosis_id=1, service_id=1, insurer_id=1, is_covered=False
                ),
                DiagnosisServiceRule(
                    id=2,
                    diagnosis_id=1,
                    service_id=1,
                    insurer_id=None,
                    is_covered=False,
                ),
                DiagnosisServiceRule(
                    id=3, diagnosis_id=1, service_id=2, insurer_id=1, is_covered=False
                ),
                DiagnosisServiceRule(
                    id=4, diagnosis_id=1, service_id=1, insurer_id=2, is_covered=False
                ),
            ]
        )
    yield factory
    engine.dispose()


def args(scope=None):
    return [
        "--diagnosis-code",
        "TEST-D",
        "--service-code",
        "TEST-S",
        "--covered",
        "true",
        *(scope or ["--insurer-id", "1"]),
    ]


def states(sessions):
    with sessions() as db:
        return dict(
            db.execute(
                select(
                    DiagnosisServiceRule.id, DiagnosisServiceRule.is_covered
                ).execution_options(include_deleted=True)
            ).all()
        )


@pytest.mark.parametrize(
    "scope,target", [(["--insurer-id", "1"], 1), (["--global"], 2)]
)
def test_exact_apply_only_changes_target(sessions, capsys, scope, target):
    assert main(args(scope) + ["--apply"], session_factory=sessions) == 0
    assert states(sessions) == {i: i == target for i in range(1, 5)}
    output = capsys.readouterr().out
    assert '"current_is_covered": false' in output
    assert '"intended_is_covered": true' in output
    assert output.index("Target:") < output.index("Success:")
    reverse = args(scope) + ["--apply"]
    reverse[reverse.index("true")] = "false"
    assert main(reverse, session_factory=sessions) == 0
    assert not any(states(sessions).values())


@pytest.mark.parametrize("flag", [[], ["--dry-run"]])
def test_dry_run_no_changes(sessions, capsys, flag):
    assert main(args() + flag, session_factory=sessions) == 0
    assert not any(states(sessions).values())
    assert "Success:" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "kind", ["diagnosis", "service", "insurer", "zero", "duplicate", "deleted"]
)
def test_invalid_target_never_updates(sessions, capsys, kind):
    command = args() + ["--apply"]
    with sessions.begin() as db:
        if kind in {"diagnosis", "service", "insurer"}:
            flag = {
                "diagnosis": "--diagnosis-code",
                "service": "--service-code",
                "insurer": "--insurer-id",
            }[kind]
            command[command.index(flag) + 1] = "999"
        elif kind == "zero":
            command[command.index("TEST-S")] = "OTHER"
            command[command.index("--insurer-id") + 1] = "2"
        elif kind == "duplicate":
            db.add(
                DiagnosisServiceRule(
                    id=5, diagnosis_id=1, service_id=1, insurer_id=1, is_covered=True
                )
            )
        else:
            db.get(DiagnosisServiceRule, 1).soft_delete()
    before = states(sessions)
    assert main(command, session_factory=sessions) == 1
    assert states(sessions) == before
    output = capsys.readouterr().out
    assert "Failure:" in output and "Success:" not in output


def test_deleted_duplicate_is_not_selected(sessions):
    with sessions.begin() as db:
        db.add(
            DiagnosisServiceRule(
                id=5,
                diagnosis_id=1,
                service_id=1,
                insurer_id=1,
                is_covered=False,
                is_deleted=True,
            )
        )
    assert main(args() + ["--apply"], session_factory=sessions) == 0
    assert states(sessions) == {1: True, 2: False, 3: False, 4: False, 5: False}


@pytest.mark.parametrize("model", [DiagnosisCode, ServiceCode, InsuranceCompany])
def test_deleted_mapping_rejected(sessions, model):
    with sessions.begin() as db:
        db.get(model, 1).soft_delete()
    assert main(args() + ["--apply"], session_factory=sessions) == 1
    assert not any(states(sessions).values())


def test_commit_failure_rolls_back_and_redacts(sessions, capsys):
    with sessions() as db:
        engine = db.get_bind()

    def fail(connection):
        raise RuntimeError("PRIVATE_SENTINEL")

    event.listen(engine, "commit", fail)
    try:
        assert main(args() + ["--apply"], session_factory=sessions) == 1
    finally:
        event.remove(engine, "commit", fail)
    assert not any(states(sessions).values())
    output = capsys.readouterr().out
    assert "PRIVATE_SENTINEL" not in output
    assert "Success:" not in output
    assert "Failure:" in output


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["--diagnosis-code", "TEST-D", "--service-code", "TEST-S", "--covered", "true"],
        args() + ["--global"],
        args() + ["--apply", "--dry-run"],
    ],
)
def test_invalid_cli_never_connects(command):
    def forbidden():
        pytest.fail("Invalid CLI must not connect.")

    with pytest.raises(SystemExit) as exc:
        main(command, session_factory=forbidden)
    assert exc.value.code == 2
