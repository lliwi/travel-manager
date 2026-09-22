"""Domain model package.

Importing this module registers every table with SQLAlchemy's metadata, which
is what both ``db.create_all()`` and Alembic autogenerate rely on. The
application factory imports it once inside an app context.
"""
from app.models.advisory import DISCLAIMER, SecurityAdvisory, WebFetch, WebSource
from app.models.ai import AIProviderConfig, AIRun, AITaskBinding
from app.models.alert import Alert, AlertRuleSetting, AlertRun, AlertTransition
from app.models.audit import GENESIS_HASH, AuditEvent
from app.models.base import (
    GUID,
    BaseModel,
    EnumType,
    InstantMixin,
    JSONBType,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDMixin,
)
from app.models.catalog import Country, Location
from app.models.document import (
    Document,
    DocumentPage,
    DocumentStateTransition,
    DocumentText,
)
from app.models.extraction import Extraction, ExtractionApplication
from app.models.itinerary import (
    Accommodation,
    FieldProvenance,
    OtherService,
    TravelSegment,
    VehicleRental,
)
from app.models.notification import Notification
from app.models.settings import SystemSetting, TravelerDocument
from app.models.trip import Trip, TripDestination, TripTraveler
from app.models.user import (
    MFARecoveryCode,
    Organization,
    Role,
    RoleGroupMapping,
    User,
    user_roles,
)

__all__ = [
    'Notification',
    # Base
    'BaseModel', 'UUIDMixin', 'TimestampMixin', 'SoftDeleteMixin', 'InstantMixin',
    'GUID', 'JSONBType', 'EnumType',
    # Identity
    'MFARecoveryCode', 'Organization', 'Role', 'RoleGroupMapping', 'User',
    'user_roles',
    # Trips
    'Trip', 'TripTraveler', 'TripDestination',
    # Itinerary
    'TravelSegment', 'Accommodation', 'VehicleRental', 'OtherService', 'FieldProvenance',
    # Documents
    'Document', 'DocumentText', 'DocumentPage', 'DocumentStateTransition',
    # Extraction
    'Extraction', 'ExtractionApplication',
    # Alerts
    'Alert', 'AlertTransition', 'AlertRuleSetting', 'AlertRun',
    # Advisories and research
    'SecurityAdvisory', 'WebSource', 'WebFetch', 'DISCLAIMER',
    # AI
    'AIProviderConfig', 'AITaskBinding', 'AIRun',
    # Catalogues and settings
    'Country', 'Location', 'SystemSetting', 'TravelerDocument',
    # Audit
    'AuditEvent', 'GENESIS_HASH',
]
