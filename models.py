from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship, Session, with_loader_criteria


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
    for obj in list(session.dirty) + list(session.deleted):
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
        CheckConstraint("source = 'update_rule.py'", name="ck_rule_history_source"),
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
