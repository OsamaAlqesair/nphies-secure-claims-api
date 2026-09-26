"""Canonical ICD-10-AM identity shared by schema, lookup and importer."""

from typing import Literal, get_args

ICD10AMSystem = Literal["http://hl7.org/fhir/sid/icd-10-am"]
ICD10_AM_SYSTEM: str = get_args(ICD10AMSystem)[0]
