from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    ForeignKey,
    Date,
    DateTime,
    Numeric,
    JSON,
    CheckConstraint,
    Index,
    UniqueConstraint,
    ForeignKeyConstraint,
    event,
    DDL,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    declarative_base,
    relationship,
    Session,
    with_loader_criteria,
    validates,
)
from services.coverage_write_guards import (
    CoverageMutationError,
    _before_flush,
    _orm_execute,
    _before_rule_insert,
    _before_rule_update,
    _before_rule_delete,
    _after_rule_write,
    _after_history_insert,
    _before_history_insert,
)


def utcnow():
    return datetime.now(timezone.utc)


class AuditMixin:
    is_deleted = Column(
        Boolean, nullable=False, default=False, server_default=false(), index=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )

    def soft_delete(self):
        self.is_deleted = True

    def restore(self):
        self.is_deleted = False


Base = declarative_base(cls=AuditMixin)


@event.listens_for(Session, "do_orm_execute")
def filter_deleted(state):
    _orm_execute(state)
    if (
        state.is_update
        and state.bind_mapper is not None
        and issubclass(state.bind_mapper.class_, ClaimHistoryMixin)
    ):
        raise ValueError("Claim intake history is append-only.")
    if (
        state.is_update
        and state.bind_mapper is not None
        and state.bind_mapper.class_ in (AuditLog, CoverageRuleHistory)
    ):
        raise ValueError("Audit records are append-only.")
    if state.is_delete:
        raise ValueError("Physical ORM bulk deletes are disabled; use soft_delete().")
    if state.is_select and not state.execution_options.get("include_deleted", False):
        state.statement = state.statement.options(
            with_loader_criteria(
                AuditMixin,
                lambda cls: cls.is_deleted.is_(False),
                include_aliases=True,
            )
        )


@event.listens_for(Session, "before_flush")
def preserve_deleted_rows(session, context, instances):
    # Rules must be rejected before the generic soft-delete conversion below.
    _before_flush(session)
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, ClaimHistoryMixin) and (
            obj in session.deleted or session.is_modified(obj)
        ):
            raise ValueError("Claim intake history is append-only.")
        if isinstance(obj, (AuditLog, CoverageRuleHistory)) and (
            obj in session.deleted or session.is_modified(obj)
        ):
            raise ValueError("Audit records are append-only.")
    for obj in list(session.deleted):
        if isinstance(obj, AuditMixin):
            session.add(obj)
            obj.soft_delete()


class DiagnosisCode(Base):
    __tablename__ = "diagnosis_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=False)


class ServiceCode(Base):
    __tablename__ = "service_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=False)


class InsuranceCompany(Base):
    __tablename__ = "insurance_companies"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    organization_id = Column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), unique=True
    )
    organization = relationship("Organization")


class DiagnosisServiceRule(Base):
    __tablename__ = "diagnosis_service_rules"
    __table_args__ = (
        Index(
            "uq_diagnosis_service_rule_current_global",
            "diagnosis_id",
            "service_id",
            unique=True,
            postgresql_where=text("insurer_id IS NULL AND is_deleted = false"),
            sqlite_where=text("insurer_id IS NULL AND is_deleted = false"),
        ),
        Index(
            "uq_diagnosis_service_rule_current_insurer",
            "diagnosis_id",
            "service_id",
            "insurer_id",
            unique=True,
            postgresql_where=text("insurer_id IS NOT NULL AND is_deleted = false"),
            sqlite_where=text("insurer_id IS NOT NULL AND is_deleted = false"),
        ),
    )

    id = Column(Integer, primary_key=True)
    diagnosis_id = Column(Integer, ForeignKey("diagnosis_codes.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("service_codes.id"), nullable=False)

    # Nullable insurer_id:
    # If NULL, this rule applies to all insurances (Fallback).
    # If set, it overrides the fallback rule for this specific insurer.
    insurer_id = Column(Integer, ForeignKey("insurance_companies.id"), nullable=True)

    # The business logic outcome of the rule (e.g., whether the service is covered)
    is_covered = Column(Boolean, default=False, nullable=False)

    # Relationships for easy querying
    diagnosis = relationship("DiagnosisCode")
    service = relationship("ServiceCode")
    insurer = relationship("InsuranceCompany")

    def soft_delete(self):
        raise CoverageMutationError(
            "Use audited soft_delete_rule() for coverage rules."
        )

    def restore(self):
        raise CoverageMutationError("Use audited restore_rule() for coverage rules.")

    def __repr__(self):
        insurer_name = self.insurer.name if self.insurer else "ALL (Fallback)"
        return f"<Rule: {self.diagnosis.code} + {self.service.code} | Insurer: {insurer_name} -> Covered: {self.is_covered}>"


# Relational projections of core FHIR R4 resources, not complete NPHIES profiles.
ResourceJSON = JSON().with_variant(JSONB(), "postgresql")


class Patient(Base):
    __tablename__ = "patients"
    id = Column(Integer, primary_key=True)
    fhir_id = Column(String(64), unique=True, nullable=False)
    identifier_system = Column(String(255), nullable=False)
    identifier_value = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    birth_date = Column(Date)
    gender = Column(String(16))
    resource = Column(ResourceJSON)
    __table_args__ = (
        Index(
            "ix_patient_identifier",
            "identifier_system",
            "identifier_value",
            unique=True,
        ),
    )
    coverages = relationship("Coverage", back_populates="beneficiary")
    encounters = relationship("Encounter", back_populates="patient")
    claims = relationship("Claim", back_populates="patient")


class Organization(Base):
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True)
    fhir_id = Column(String(64), unique=True, nullable=False)
    identifier_system = Column(String(255), nullable=False)
    identifier_value = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    organization_type = Column(String(64), nullable=False)
    resource = Column(ResourceJSON)
    __table_args__ = (
        Index(
            "ix_organization_identifier",
            "identifier_system",
            "identifier_value",
            unique=True,
        ),
    )
    coverages = relationship("Coverage", back_populates="payor")
    practitioners = relationship("Practitioner", back_populates="organization")
    encounters = relationship("Encounter", back_populates="service_provider")
    claims = relationship(
        "Claim", back_populates="provider", foreign_keys="Claim.provider_id"
    )
    insured_claims = relationship(
        "Claim", back_populates="insurer", foreign_keys="Claim.insurer_id"
    )


class Practitioner(Base):
    __tablename__ = "practitioners"
    id = Column(Integer, primary_key=True)
    fhir_id = Column(String(64), unique=True, nullable=False)
    identifier_system = Column(String(255), nullable=False)
    identifier_value = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    organization_id = Column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    resource = Column(ResourceJSON)
    __table_args__ = (
        Index(
            "ix_practitioner_identifier",
            "identifier_system",
            "identifier_value",
            unique=True,
        ),
    )
    organization = relationship("Organization", back_populates="practitioners")
    claims = relationship("Claim", back_populates="practitioner")


class Coverage(Base):
    __tablename__ = "coverages"
    id = Column(Integer, primary_key=True)
    fhir_id = Column(String(64), unique=True, nullable=False)
    beneficiary_id = Column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payor_id = Column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status = Column(String(32), nullable=False)
    policy_number = Column(String(128), nullable=False)
    period_start = Column(DateTime(timezone=True))
    period_end = Column(DateTime(timezone=True))
    resource = Column(ResourceJSON)
    __table_args__ = (
        UniqueConstraint(
            "id", "beneficiary_id", "payor_id", name="uq_coverage_parties"
        ),
        CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
            name="ck_coverage_period",
        ),
    )
    beneficiary = relationship("Patient", back_populates="coverages")
    payor = relationship("Organization", back_populates="coverages")
    claims = relationship(
        "Claim",
        back_populates="coverage",
        foreign_keys="Claim.coverage_id",
        primaryjoin="Coverage.id == Claim.coverage_id",
    )


class Encounter(Base):
    __tablename__ = "encounters"
    id = Column(Integer, primary_key=True)
    fhir_id = Column(String(64), unique=True, nullable=False)
    patient_id = Column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_provider_id = Column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status = Column(String(32), nullable=False)
    encounter_class = Column(String(32), nullable=False)
    period_start = Column(DateTime(timezone=True))
    period_end = Column(DateTime(timezone=True))
    resource = Column(ResourceJSON)
    __table_args__ = (
        UniqueConstraint("id", "patient_id", name="uq_encounter_patient"),
        CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
            name="ck_encounter_period",
        ),
    )
    patient = relationship("Patient", back_populates="encounters")
    service_provider = relationship("Organization", back_populates="encounters")
    claims = relationship(
        "Claim",
        back_populates="encounter",
        foreign_keys="Claim.encounter_id",
        primaryjoin="Encounter.id == Claim.encounter_id",
    )


class Claim(Base):
    __tablename__ = "claims"
    id = Column(Integer, primary_key=True)
    fhir_id = Column(String(64), unique=True, nullable=False)
    patient_id = Column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_id = Column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    insurer_id = Column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    practitioner_id = Column(
        ForeignKey("practitioners.id", ondelete="RESTRICT"), index=True
    )
    coverage_id = Column(
        ForeignKey("coverages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    encounter_id = Column(ForeignKey("encounters.id", ondelete="RESTRICT"), index=True)
    status = Column(String(32), nullable=False)
    use = Column(String(32), nullable=False, default="claim")
    currency = Column(String(3), nullable=False, default="SAR")
    total = Column(Numeric(14, 2), nullable=False)
    resource = Column(ResourceJSON)
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_claim_total_nonnegative"),
        ForeignKeyConstraint(
            ["coverage_id", "patient_id", "insurer_id"],
            ["coverages.id", "coverages.beneficiary_id", "coverages.payor_id"],
            name="fk_claim_coverage_parties",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["encounter_id", "patient_id"],
            ["encounters.id", "encounters.patient_id"],
            name="fk_claim_encounter_patient",
            ondelete="RESTRICT",
        ),
    )
    patient = relationship("Patient", back_populates="claims")
    provider = relationship(
        "Organization", back_populates="claims", foreign_keys=[provider_id]
    )
    insurer = relationship(
        "Organization", back_populates="insured_claims", foreign_keys=[insurer_id]
    )
    practitioner = relationship("Practitioner", back_populates="claims")
    coverage = relationship(
        "Coverage",
        back_populates="claims",
        foreign_keys=[coverage_id],
        primaryjoin="Claim.coverage_id == Coverage.id",
    )
    encounter = relationship(
        "Encounter",
        back_populates="claims",
        foreign_keys=[encounter_id],
        primaryjoin="Claim.encounter_id == Encounter.id",
    )


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True)
    password_hash = Column(String(512), nullable=False)
    role = Column(String(16), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    failed_login_attempts = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until = Column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'provider')", name="ck_user_role"),
        CheckConstraint("token_version >= 0", name="ck_user_token_version"),
        CheckConstraint("failed_login_attempts >= 0", name="ck_user_login_attempts"),
        CheckConstraint(
            "username = lower(username)", name="ck_user_username_lowercase"
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    actor_user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    action = Column(String(64), nullable=False, index=True)
    outcome = Column(String(16), nullable=False)
    reason = Column(String(64), nullable=False)
    http_status = Column(Integer, nullable=False)
    request_id = Column(String(36), nullable=False, index=True)
    endpoint = Column(String(128), nullable=False)
    client_ip = Column(String(64))
    __table_args__ = (
        CheckConstraint("outcome IN ('success', 'failure')", name="ck_audit_outcome"),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="ck_audit_status"),
        Index("ix_audit_created_at", "created_at"),
    )

    def soft_delete(self):
        raise ValueError("Audit records are append-only.")

    def restore(self):
        raise ValueError("Audit records are append-only.")


class NphiesTerminology(Base):
    __tablename__ = "nphies_terminology"
    __table_args__ = (
        UniqueConstraint("code_system_url", "code", name="uq_terminology_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    code_system_url = Column(String, nullable=False, index=True)
    code = Column(String, nullable=False, index=True)
    display = Column(String)
    definition = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")


# History shares migration metadata, but has no mutable audit/soft-delete mixin.
HistoryBase = declarative_base(metadata=Base.metadata)


def _history_uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A UUID-compatible history identifier is required.") from None


def _history_uuid_checks(column, name, *, nullable=False):
    pattern = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    prefix = f"{column} IS NULL OR " if nullable else ""
    return (
        CheckConstraint(prefix + f"{column} ~ '^{pattern}$'", name=name).ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            prefix + f"(length({column}) = 36 AND {column} = lower({column}) "
            f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
            f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
            f"AND length(replace({column}, '-', '')) = 32 "
            f"AND replace({column}, '-', '') NOT GLOB '*[^0-9a-f]*')",
            name=name,
        ).ddl_if(dialect="sqlite"),
    )


def _history_object_checks(column, name, *, original_text=False):
    postgres_value = f"{column}::jsonb" if original_text else column
    return (
        CheckConstraint(f"jsonb_typeof({postgres_value}) = 'object'", name=name).ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            f"json_valid({column}) AND json_type({column}) = 'object'", name=name
        ).ddl_if(dialect="sqlite"),
    )


class ClaimHistoryMixin:
    """Historical rows have no mutable audit columns or transaction helpers.

    Database triggers protect persisted rows, not against privileged owners
    disabling triggers. Snapshot contents must never be logged or audited.
    """

    def soft_delete(self):
        raise ValueError("Claim intake history is append-only.")

    def restore(self):
        raise ValueError("Claim intake history is append-only.")


class ClaimIntake(ClaimHistoryMixin, HistoryBase):
    __tablename__ = "claim_intakes"

    id = Column(Integer, primary_key=True)
    # Application-server UUID generation, using the repository's UUID text form.
    public_id = Column(String(36), nullable=False, default=lambda: str(uuid4()))
    owner_user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    idempotency_key = Column(String(36), nullable=False)
    canonical_input_hash = Column(String(64), nullable=False)
    # Text preserves the accepted JSON exactly, including numeric lexemes,
    # whitespace and object order that a JSONB round trip would discard.
    original_request_snapshot = Column(Text, nullable=False)
    validated_submission_snapshot = Column(ResourceJSON, nullable=False)
    schema_version = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_claim_intake_public_id"),
        UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_claim_intake_owner_idempotency"
        ),
        Index("ix_claim_intake_owner_created", "owner_user_id", "created_at", "id"),
        CheckConstraint(
            "length(trim(schema_version)) > 0 AND length(schema_version) <= 64",
            name="ck_claim_intake_schema_version",
        ),
        CheckConstraint(
            "canonical_input_hash ~ '^[0-9a-f]{64}$'", name="ck_claim_intake_input_hash"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(canonical_input_hash) = 64 AND canonical_input_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_claim_intake_input_hash",
        ).ddl_if(dialect="sqlite"),
        *_history_uuid_checks("public_id", "ck_claim_intake_public_uuid"),
        *_history_uuid_checks("idempotency_key", "ck_claim_intake_idempotency_uuid"),
        *_history_object_checks(
            "original_request_snapshot",
            "ck_claim_intake_original_object",
            original_text=True,
        ),
        *_history_object_checks(
            "validated_submission_snapshot", "ck_claim_intake_validated_object"
        ),
    )

    @validates("public_id", "idempotency_key")
    def validate_uuid(self, key, value):
        return _history_uuid(value)


class ClaimValidationAttempt(ClaimHistoryMixin, HistoryBase):
    __tablename__ = "claim_validation_attempts"

    id = Column(Integer, primary_key=True)
    intake_id = Column(
        ForeignKey("claim_intakes.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no = Column(Integer, nullable=False)
    actor_user_id = Column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_id = Column(String(36), nullable=False, index=True)
    occurred_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    result = Column(String(16), nullable=False)
    reason = Column(String(64), nullable=False)
    operation_outcome_snapshot = Column(ResourceJSON, nullable=False)
    # JSON facts from dataclasses.asdict(ClaimValidationReport); no ORM objects.
    validation_report_snapshot = Column(ResourceJSON, nullable=False)
    validation_schema_version = Column(String(64), nullable=False)
    revalidation_idempotency_key = Column(String(36))

    __table_args__ = (
        UniqueConstraint("intake_id", "attempt_no", name="uq_claim_attempt_number"),
        UniqueConstraint(
            "intake_id",
            "revalidation_idempotency_key",
            name="uq_claim_attempt_idempotency",
        ),
        CheckConstraint("attempt_no > 0", name="ck_claim_attempt_number"),
        CheckConstraint(
            "result IN ('PASSED', 'FAILED', 'UNAVAILABLE')",
            name="ck_claim_attempt_result",
        ),
        CheckConstraint(
            "reason IN ('validation_passed', 'validation_failed', 'validation_unavailable')",
            name="ck_claim_attempt_reason",
        ),
        CheckConstraint(
            "reason = 'validation_' || lower(result)",
            name="ck_claim_attempt_result_reason",
        ),
        CheckConstraint(
            "length(trim(request_id)) > 0 AND length(request_id) <= 36",
            name="ck_claim_attempt_request_id",
        ),
        CheckConstraint(
            "length(trim(validation_schema_version)) > 0 AND length(validation_schema_version) <= 64",
            name="ck_claim_attempt_schema_version",
        ),
        Index("ix_claim_attempt_occurred", "occurred_at"),
        *_history_uuid_checks(
            "revalidation_idempotency_key",
            "ck_claim_attempt_idempotency_uuid",
            nullable=True,
        ),
        *_history_object_checks(
            "operation_outcome_snapshot", "ck_claim_attempt_outcome_object"
        ),
        *_history_object_checks(
            "validation_report_snapshot", "ck_claim_attempt_report_object"
        ),
    )

    @validates("revalidation_idempotency_key")
    def validate_uuid(self, key, value):
        return _history_uuid(value) if value is not None else None


class ClaimIntakeEvent(ClaimHistoryMixin, HistoryBase):
    __tablename__ = "claim_intake_events"

    id = Column(Integer, primary_key=True)
    intake_id = Column(
        ForeignKey("claim_intakes.id", ondelete="RESTRICT"), nullable=False
    )
    event_no = Column(Integer, nullable=False)
    event_type = Column(String(32), nullable=False)
    actor_user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    request_id = Column(String(36), nullable=False, index=True)
    reason = Column(String(64), nullable=False)
    # Only scalar validation metadata is allowed here, never request/patient data.
    details = Column(
        ResourceJSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    occurred_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("intake_id", "event_no", name="uq_claim_event_number"),
        CheckConstraint("event_no > 0", name="ck_claim_event_number"),
        CheckConstraint(
            "event_type IN ('intake.created', 'validation.completed')",
            name="ck_claim_event_type",
        ),
        CheckConstraint(
            "reason IN ('intake_accepted', 'validation_completed')",
            name="ck_claim_event_reason",
        ),
        CheckConstraint(
            "(event_type = 'intake.created' AND reason = 'intake_accepted') OR (event_type = 'validation.completed' AND reason = 'validation_completed')",
            name="ck_claim_event_type_reason",
        ),
        CheckConstraint(
            "length(trim(request_id)) > 0 AND length(request_id) <= 36",
            name="ck_claim_event_request_id",
        ),
        CheckConstraint(
            "(details - 'attempt_no' - 'result') = '{}'::jsonb",
            name="ck_claim_event_detail_keys",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "json_remove(details, '$.attempt_no', '$.result') = '{}'",
            name="ck_claim_event_detail_keys",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "(NOT (details ? 'result') OR (jsonb_typeof(details->'result') = 'string' AND details->>'result' IN ('PASSED', 'FAILED', 'UNAVAILABLE'))) AND (NOT (details ? 'attempt_no') OR (jsonb_typeof(details->'attempt_no') = 'number' AND details->>'attempt_no' ~ '^[1-9][0-9]*$'))",
            name="ck_claim_event_detail_values",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(json_type(details, '$.result') IS NULL OR (json_type(details, '$.result') = 'text' AND json_extract(details, '$.result') IN ('PASSED', 'FAILED', 'UNAVAILABLE'))) AND (json_type(details, '$.attempt_no') IS NULL OR (json_type(details, '$.attempt_no') = 'integer' AND json_extract(details, '$.attempt_no') > 0))",
            name="ck_claim_event_detail_values",
        ).ddl_if(dialect="sqlite"),
        Index("ix_claim_event_occurred", "occurred_at"),
        *_history_object_checks("details", "ck_claim_event_details_object"),
    )

    @validates("details")
    def validate_details(self, key, value):
        if not isinstance(value, dict) or set(value) - {"attempt_no", "result"}:
            raise ValueError("Only controlled claim event metadata is permitted.")
        if "attempt_no" in value and (
            type(value["attempt_no"]) is not int or value["attempt_no"] < 1
        ):
            raise ValueError("A positive event attempt number is required.")
        if "result" in value and value["result"] not in (
            "PASSED",
            "FAILED",
            "UNAVAILABLE",
        ):
            raise ValueError("A supported validation result is required.")
        return value


class CoverageRuleHistory(HistoryBase):
    __tablename__ = "coverage_rule_history"

    id = Column(Integer, primary_key=True)
    rule_id = Column(
        ForeignKey("diagnosis_service_rules.id", ondelete="RESTRICT"), nullable=False
    )
    action = Column(String(16), nullable=False)
    diagnosis_id = Column(Integer, nullable=False)
    service_id = Column(Integer, nullable=False)
    insurer_id = Column(Integer)
    diagnosis_code = Column(String, nullable=False)
    service_code = Column(String, nullable=False)
    insurer_name = Column(String)
    old_is_covered = Column(Boolean)
    new_is_covered = Column(Boolean, nullable=False)
    old_is_deleted = Column(Boolean)
    new_is_deleted = Column(Boolean, nullable=False)
    actor_user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"))
    source = Column(String(64), nullable=False)
    reason = Column(String(500), nullable=False)
    occurred_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('CREATE', 'UPDATE', 'SOFT_DELETE', 'RESTORE')",
            name="ck_rule_history_action",
        ),
        CheckConstraint(
            "source IN ('update_rule.py', 'coverage_mutations.py', 'seed.py', 'migrate_sqlite.py')",
            name="ck_rule_history_source",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0 AND length(reason) <= 500",
            name="ck_rule_history_reason",
        ),
        CheckConstraint(
            "(insurer_id IS NULL AND insurer_name IS NULL) OR "
            "(insurer_id IS NOT NULL AND insurer_name IS NOT NULL)",
            name="ck_rule_history_scope",
        ),
        CheckConstraint(
            "(action = 'CREATE' AND old_is_covered IS NULL AND old_is_deleted IS NULL) OR "
            "(action <> 'CREATE' AND old_is_covered IS NOT NULL AND old_is_deleted IS NOT NULL AND ("
            "(action = 'UPDATE' AND old_is_deleted = false AND new_is_deleted = false AND old_is_covered <> new_is_covered) OR "
            "(action = 'SOFT_DELETE' AND old_is_deleted = false AND new_is_deleted = true AND old_is_covered = new_is_covered) OR "
            "(action = 'RESTORE' AND old_is_deleted = true AND new_is_deleted = false AND old_is_covered = new_is_covered)))",
            name="ck_rule_history_transition",
        ),
        Index("ix_rule_history_rule_id_id", "rule_id", "id"),
        Index("ix_rule_history_occurred_at", "occurred_at"),
    )

    def soft_delete(self):
        raise ValueError("Audit records are append-only.")

    def restore(self):
        raise ValueError("Audit records are append-only.")


# create_all() is used by isolated SQLite fixtures. Production uses Alembic.
event.listen(DiagnosisServiceRule, "before_insert", _before_rule_insert)
event.listen(DiagnosisServiceRule, "before_update", _before_rule_update)
event.listen(DiagnosisServiceRule, "before_delete", _before_rule_delete)
event.listen(DiagnosisServiceRule, "after_insert", _after_rule_write)
event.listen(DiagnosisServiceRule, "after_update", _after_rule_write)
event.listen(CoverageRuleHistory, "after_insert", _after_history_insert)
event.listen(CoverageRuleHistory, "before_insert", _before_history_insert)

for operation in ("UPDATE", "DELETE"):
    event.listen(
        CoverageRuleHistory.__table__,
        "after_create",
        DDL(
            f"CREATE TRIGGER prevent_rule_history_{operation.lower()} "
            f"BEFORE {operation} ON coverage_rule_history BEGIN "
            "SELECT RAISE(ABORT, 'Coverage rule history is append-only'); END"
        ).execute_if(dialect="sqlite"),
    )


for history_model in (ClaimIntake, ClaimValidationAttempt, ClaimIntakeEvent):
    table = history_model.__table__
    for operation in ("UPDATE", "DELETE"):
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER prevent_{table.name}_{operation.lower()} "
                f"BEFORE {operation} ON {table.name} BEGIN "
                "SELECT RAISE(ABORT, 'Claim intake history is append-only'); END"
            ).execute_if(dialect="sqlite"),
        )
    event.listen(
        table,
        "after_create",
        DDL(
            "CREATE OR REPLACE FUNCTION reject_claim_intake_history_mutation() "
            "RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'Claim intake history is append-only'; END; $$ LANGUAGE plpgsql"
        ).execute_if(dialect="postgresql"),
    )
    for operation, level in (("UPDATE OR DELETE", "ROW"), ("TRUNCATE", "STATEMENT")):
        suffix = "truncate" if operation == "TRUNCATE" else "mutation"
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER prevent_{table.name}_{suffix} BEFORE {operation} "
                f"ON {table.name} FOR EACH {level} EXECUTE FUNCTION reject_claim_intake_history_mutation()"
            ).execute_if(dialect="postgresql"),
        )
