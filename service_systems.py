"""Single source of truth for supported NPHIES service coding systems."""

from typing import Literal, get_args

ServiceSystem = Literal[
    "http://nphies.sa/terminology/CodeSystem/services",
    "http://nphies.sa/terminology/CodeSystem/procedures",
    "http://nphies.sa/terminology/CodeSystem/laboratory",
    "http://nphies.sa/terminology/CodeSystem/imaging",
    "http://nphies.sa/terminology/CodeSystem/oral-health-ip",
    "http://nphies.sa/terminology/CodeSystem/oral-health-op",
    "http://nphies.sa/terminology/CodeSystem/medication-codes",
]

# Derive lookup values from the schema type; never maintain a second list.
SERVICE_SYSTEMS: tuple[str, ...] = get_args(ServiceSystem)
