"""Alert rules (specification section 2.4).

Importing this package registers every rule with the registry in
``app.services.alerts.base``.
"""
from app.services.alerts.rules.boarding import BoardingPassRule
from app.services.alerts.rules.connections import ConnectionMarginRule
from app.services.alerts.rules.data_quality import (
    IncompleteDataRule,
    MissingSupportingDocumentRule,
)
from app.services.alerts.rules.documents import TravelDocumentExpiryRule
from app.services.alerts.rules.lodging import (
    LateArrivalForBookingRule,
    UncoveredNightRule,
)
from app.services.alerts.rules.overlaps import OverlappingSegmentsRule
from app.services.alerts.rules.policy import (
    LodgingCostOverPolicyRule,
    ShortNoticeBookingRule,
    TripCostOverPolicyRule,
)
from app.services.alerts.rules.timezones import TimezoneShiftRule

__all__ = [
    'BoardingPassRule',
    'ConnectionMarginRule',
    'OverlappingSegmentsRule',
    'LateArrivalForBookingRule',
    'UncoveredNightRule',
    'IncompleteDataRule',
    'MissingSupportingDocumentRule',
    'TimezoneShiftRule',
    'TravelDocumentExpiryRule',
    'TripCostOverPolicyRule',
    'LodgingCostOverPolicyRule',
    'ShortNoticeBookingRule',
]
