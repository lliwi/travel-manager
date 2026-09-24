"""Shared pytest fixtures.

The suite runs against SQLite with the ``testing`` configuration, which swaps
every external dependency for an in-process substitute: storage in memory, no
antivirus, no OCR, and a deterministic AI provider. A test must never pass or
fail because a real service happened to be reachable.
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('FLASK_ENV', 'testing')

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402
from app.models.enums import (  # noqa: E402
    DocumentType,
    RoleCode,
    SegmentType,
    TripStatus,
    UserStatus,
)


@pytest.fixture(scope='session')
def _application():
    """The Flask application, built once for the whole session."""
    application = create_app('testing')
    with application.app_context():
        _db.create_all()
    yield application
    with application.app_context():
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def app(_application):
    """A fresh application context per test.

    Deliberately *not* one context for the whole session: ``g`` lives on the
    application context, and Flask-Login caches the logged-in user there. A
    shared context would hand one test the previous test's cached user, which
    is detached once the session is discarded.
    """
    with _application.app_context():
        yield _application

        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()
        _db.session.remove()

        # Drop the memoised storage backend so objects do not leak between tests.
        from app.services import storage_service

        storage_service.reset_backend()


@pytest.fixture
def seeded(app):
    """Roles, settings, alert rules and catalogues, ready to use."""
    from app.services import settings_service
    from app.services.seed_service import (
        seed_alert_rules,
        seed_catalogs,
        seed_organization,
        seed_roles,
        seed_web_sources,
    )

    seed_roles()
    seed_organization()
    settings_service.seed_defaults()
    seed_alert_rules()
    seed_web_sources()
    seed_catalogs()
    return True


@pytest.fixture
def client(app):
    """A test client."""
    return app.test_client()


# ======================================================================
# Users
# ======================================================================
def _make_user(username, email, nombre, role_code, password='Contrasena-Test-2026'):
    from app.models.user import Role, User
    from app.utils.crypto import hash_password

    user = User(
        username=username,
        email=email,
        nombre=nombre,
        apellidos='Pruebas',
        password_hash=hash_password(password),
        estado=UserStatus.ACTIVO,
    )
    role = Role.get(role_code)
    if role is not None:
        user.roles.append(role)
    _db.session.add(user)
    _db.session.commit()
    return user


@pytest.fixture
def admin(seeded):
    """An administrator."""
    return _make_user('admin', 'admin@example.test', 'Admin', RoleCode.ADMINISTRADOR)


@pytest.fixture
def gestor(seeded):
    """A travel manager."""
    return _make_user('gestor', 'gestor@example.test', 'Gestora', RoleCode.GESTOR)


@pytest.fixture
def viajero(seeded):
    """A traveller who will be assigned to the trip."""
    return _make_user('viajero', 'viajero@example.test', 'Viajero', RoleCode.USUARIO)


@pytest.fixture
def otro_viajero(seeded):
    """A second traveller, assigned to the same trip.

    Exists so traveller-to-traveller isolation can be tested, which is what
    acceptance criterion 5.4 turns on.
    """
    return _make_user('viajero2', 'viajero2@example.test', 'Otra', RoleCode.USUARIO)


@pytest.fixture
def ajeno(seeded):
    """A user assigned to nothing. The subject of acceptance criterion 5.1."""
    return _make_user('ajeno', 'ajeno@example.test', 'Ajeno', RoleCode.USUARIO)


@pytest.fixture
def password():
    """The password every fixture user has."""
    return 'Contrasena-Test-2026'


# ======================================================================
# Trips
# ======================================================================
@pytest.fixture
def trip(gestor, viajero):
    """A confirmed trip from Madrid to Berlin with one traveller assigned."""
    from app.services import trip_service

    created = trip_service.create_trip(
        gestor,
        titulo='Reunión de coordinación en Berlín',
        estado=TripStatus.CONFIRMADO,
        inicio_local=datetime(2026, 6, 1, 8, 0),
        inicio_tz='Europe/Madrid',
        fin_local=datetime(2026, 6, 4, 20, 0),
        fin_tz='Europe/Madrid',
    )
    trip_service.add_traveler(gestor, created, viajero)
    trip_service.add_destination(
        gestor, created, ciudad='Berlín', pais_codigo='DE',
        zona_horaria='Europe/Berlin',
        inicio_local=datetime(2026, 6, 1, 12, 0),
        fin_local=datetime(2026, 6, 4, 10, 0),
    )
    return created


@pytest.fixture
def traveler_row(trip, viajero):
    """The traveller assignment row of ``viajero`` on ``trip``."""
    return trip.traveler_for(viajero.id)


@pytest.fixture
def segment_factory(trip):
    """Build a flight segment on the trip with correct instant triples."""
    from app.models.itinerary import TravelSegment
    from app.utils.timeutil import set_instant

    def _factory(numero='IB3210', origen='MAD', destino='BER',
                 salida=None, llegada=None,
                 salida_tz='Europe/Madrid', llegada_tz='Europe/Berlin',
                 tipo=SegmentType.VUELO, traveler=None,
                 origen_pais='ES', destino_pais='DE', **kwargs):
        segment = TravelSegment(
            trip_id=trip.id,
            trip_traveler_id=traveler.id if traveler is not None else None,
            tipo=tipo,
            numero=numero,
            origen_codigo=origen,
            destino_codigo=destino,
            origen_pais=origen_pais,
            destino_pais=destino_pais,
            **kwargs,
        )
        set_instant(segment, 'salida', salida or datetime(2026, 6, 1, 10, 0), salida_tz)
        set_instant(segment, 'llegada', llegada or datetime(2026, 6, 1, 12, 30), llegada_tz)
        _db.session.add(segment)
        _db.session.commit()
        return segment

    return _factory


# ======================================================================
# Authentication helpers
# ======================================================================
@pytest.fixture
def login(client, password):
    """Log a user in through the real login route."""

    def _login(user, pwd=None):
        response = client.post(
            '/auth/login',
            data={'username': user.username, 'password': pwd or password},
            follow_redirects=False,
        )
        return response

    return _login


@pytest.fixture
def api_login(client, password):
    """Log a user in through the API."""

    def _login(user, pwd=None):
        return client.post(
            '/api/v1/auth/login',
            json={'username': user.username, 'password': pwd or password},
        )

    return _login


@pytest.fixture
def as_user(client, login):
    """Context manager logging a user in for the duration of a block."""
    from contextlib import contextmanager

    @contextmanager
    def _as(user):
        login(user)
        try:
            yield client
        finally:
            client.post('/auth/logout')

    return _as


@pytest.fixture
def db():
    """The SQLAlchemy session, for tests that need direct access."""
    return _db


# ======================================================================
# Documents
# ======================================================================
#: A minimal but structurally valid PDF whose text layer holds a flight booking.
#: Built by hand rather than committed as a binary, so the fixture is readable
#: and the test does not depend on an opaque file.
def _booking_pdf_bytes():
    texto = (
        'IBERIA - TARJETA DE EMBARQUE\\n'
        'Vuelo: IB3210   MAD - BER\\n'
        'Localizador: XYZ12A\\n'
        'Salida: 01/06/2026 10:00  Terminal 4\\n'
        'Llegada: 01/06/2026 12:30  Terminal 1\\n'
        'Pasajero: VIAJERO PRUEBAS'
    )
    contenido = f'BT /F1 10 Tf 40 750 Td 14 TL ({texto}) Tj ET'.encode('latin-1')

    objetos = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(contenido)).encode() + b' >>\nstream\n'
        + contenido + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]

    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for index, body in enumerate(objetos, start=1):
        offsets.append(len(out))
        out += f'{index} 0 obj\n'.encode() + body + b'\nendobj\n'

    xref_at = len(out)
    out += f'xref\n0 {len(objetos) + 1}\n'.encode()
    out += b'0000000000 65535 f \n'
    for offset in offsets:
        out += f'{offset:010d} 00000 n \n'.encode()
    out += (
        f'trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n'
        f'startxref\n{xref_at}\n%%EOF\n'
    ).encode()
    return bytes(out)


@pytest.fixture
def booking_pdf():
    """Bytes of a small PDF flight confirmation."""
    return _booking_pdf_bytes()


@pytest.fixture
def eicar_bytes():
    """The EICAR antivirus test string, assembled at runtime.

    Split so the literal never sits in the repository as one scannable token --
    a developer's own antivirus would quarantine the source file.
    """
    parts = ['X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-',
             'ANTIVIRUS-TEST-FILE!$H+H*']
    return ''.join(parts).encode('ascii')


@pytest.fixture
def documento_subido(app, gestor, trip, booking_pdf):
    """A document uploaded through the real service, awaiting processing."""
    import io

    from werkzeug.datastructures import FileStorage

    from app.services import document_service

    upload = FileStorage(
        stream=io.BytesIO(booking_pdf),
        filename='reserva_vuelo_iberia.pdf',
        content_type='application/pdf',
    )
    # Committing without enqueuing: the tests drive the pipeline synchronously
    # so a failure points at the step that failed, not at a worker.
    document = document_service.upload(
        gestor, trip, upload, tipo=DocumentType.RESERVA, commit=False
    )
    _db.session.commit()
    return document


@pytest.fixture
def documento_procesado(app, documento_subido):
    """A document taken through the whole pipeline, awaiting review.

    Each task is invoked directly rather than through Celery, so the test is
    synchronous and a failure is attributable to one step.
    """
    from app.tasks import document_tasks

    document_id = str(documento_subido.id)
    for _, task in document_tasks.PIPELINE:
        task.run(document_id)

    _db.session.refresh(documento_subido)
    return documento_subido


@pytest.fixture
def documento_aprobado(app, gestor, documento_procesado):
    """A processed document whose extraction a manager has approved.

    The extraction payload is set deterministically rather than left to the stub
    provider's empty answer, so the approval exercises the real mapping from
    extracted fields onto itinerary entities.
    """
    from app.models.enums import DocumentClassification
    from app.services import extraction_service

    document = documento_procesado
    document.clasificacion = DocumentClassification.VUELO
    _db.session.commit()

    extraction = document.current_extraction
    assert extraction is not None, 'El pipeline debe haber creado una extracción.'

    campos = {
        'localizador': 'XYZ12A',
        'aerolinea': 'Iberia',
        'numero_vuelo': 'IB3210',
        'pasajero': 'VIAJERO PRUEBAS',
        'origen_codigo': 'MAD',
        'origen_pais': 'ES',
        'terminal_origen': '4',
        'salida': {'local': '2026-06-01T10:00', 'zona_horaria': 'Europe/Madrid'},
        'destino_codigo': 'BER',
        'destino_pais': 'DE',
        'terminal_destino': '1',
        'llegada': {'local': '2026-06-01T12:30', 'zona_horaria': 'Europe/Berlin'},
    }
    extraction.payload = {
        'campos': campos,
        'procedencias': {
            'numero_vuelo': {'pagina': 1, 'fragmento': 'Vuelo: IB3210'},
            'localizador': {'pagina': 1, 'fragmento': 'Localizador: XYZ12A'},
            'origen_codigo': {'pagina': 1, 'fragmento': 'MAD - BER'},
            'destino_codigo': {'pagina': 1, 'fragmento': 'MAD - BER'},
            # No literal span: inferred from the airport catalogue, so this one
            # is recorded with origin 'ia' rather than 'documento'.
            'salida': {'pagina': 1, 'fragmento': None},
        },
    }
    extraction.payload_normalizado = {'campos': campos}
    extraction.confianzas = dict.fromkeys(campos, 0.95)
    extraction.confianza_global = 0.95
    _db.session.commit()

    extraction_service.approve(gestor, extraction, comentario='Revisado y correcto.')
    _db.session.refresh(document)
    return document


# ======================================================================
# Corporate directory
# ======================================================================
@pytest.fixture
def directorio(monkeypatch):
    """Replace the real connection with ldap3's in-memory server.

    Lives here and not in ``test_directorio`` because more than one module
    needs a directory now -- a traveller provisioned from it, a passport
    registered by somebody who signs in through it -- and importing a fixture
    between test modules collides with the name of the parameter that receives
    it.
    """
    from ldap3 import MOCK_SYNC, Connection, Server

    from app.services.identity import ldap as ldap_module
    from app.services.identity.ldap import LDAPIdentityProvider
    from tests.test_directorio import _arbol

    servidor = Server('directorio.test', get_info=None)

    def _conectar(self, usuario=None, contrasena=None):
        conf = ldap_module._conf()
        usuario = usuario if usuario is not None else conf['usuario']
        contrasena = contrasena if contrasena is not None else conf['contrasena']

        conexion = Connection(
            servidor, user=usuario, password=contrasena,
            client_strategy=MOCK_SYNC,
        )
        # Por conexión: cada Connection en MOCK_SYNC tiene su propio árbol en
        # memoria, así que sembrarlo una sola vez dejaría vacías a las demás.
        _arbol(conexion)
        return conexion if conexion.bind() else None

    monkeypatch.setattr(LDAPIdentityProvider, '_conectar', _conectar)
    return LDAPIdentityProvider()
