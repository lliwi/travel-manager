"""Copias de seguridad completas desde el panel: un ZIP cifrado, y vuelta atrás.

Lo que tiene que cumplirse, y que solo se descubre el día que hace falta: que
lo restaurado es exactamente lo copiado -- tablas, documentos y secretos
legibles --, que una copia equivocada, incompleta o manipulada se rechaza
antes de tocar nada, y que el ZIP no deja leer nada sin su frase de paso.
"""
import io
import re
import zipfile
from pathlib import Path

import pytest

from app.extensions import db
from app.models.backup import BackupJob
from app.models.enums import AIProviderCode, BackupJobState
from app.services import ai_provider_service, backup_service, settings_service, storage_service
from app.utils.errors import ConflictError, ValidationError

FRASE = 'una frase de paso larga'
CLAVE_API = 'sk-clave-secreta-de-prueba-1234'
CONTRASENA_LDAP = 'contrasena-del-directorio'


class _Fichero:
    """What a form upload looks like to the service."""

    def __init__(self, datos, nombre='copia.zip'):
        self.stream = io.BytesIO(datos)
        self.filename = nombre


def _bucket():
    return storage_service.buckets()[0]


def _poner(clave, datos):
    storage_service.get_backend().put(_bucket(), clave, io.BytesIO(datos))


def _leer(clave):
    return storage_service.get_backend().get(_bucket(), clave).read()


@pytest.fixture
def instalacion(app, admin, trip):
    """Something worth copying: a trip, a document, and secrets."""
    ai_provider_service.create(admin, 'OpenAI', AIProviderCode.OPENAI, api_key=CLAVE_API)
    settings_service.set_value('LDAP_CONTRASENA', CONTRASENA_LDAP)
    _poner('documentos/uno/original.pdf', b'%PDF-1.4 contenido del documento')
    return trip


def _copia(admin):
    trabajo = backup_service.solicitar_copia(admin, FRASE, FRASE)
    assert trabajo.estado is BackupJobState.COMPLETADO, trabajo.error
    return trabajo, b''.join(backup_service.descargar(admin, trabajo))


def _restaurar(admin, datos, frase=FRASE):
    return backup_service.solicitar_restauracion(admin, _Fichero(datos), frase,
                                                 backup_service.CONFIRMACION)


@pytest.mark.integration
class TestIdaYVuelta:
    def test_lo_restaurado_es_lo_copiado(self, admin, instalacion):
        from app.models.trip import Trip

        titulo = instalacion.titulo
        _, datos = _copia(admin)

        instalacion.titulo = 'Cambiado después de la copia'
        settings_service.set_value('ZONA_HORARIA_POR_DEFECTO', 'America/Lima')
        db.session.commit()

        trabajo = _restaurar(admin, datos)

        assert trabajo.estado is BackupJobState.COMPLETADO, trabajo.error
        assert db.session.get(Trip, instalacion.id).titulo == titulo
        assert settings_service.get('ZONA_HORARIA_POR_DEFECTO') == 'Europe/Madrid'

    def test_los_documentos_vuelven_y_lo_posterior_se_va(self, admin, instalacion):
        _, datos = _copia(admin)
        _poner('documentos/uno/original.pdf', b'sobrescrito')
        _poner('documentos/posterior/original.pdf', b'subido despues')

        _restaurar(admin, datos)

        assert _leer('documentos/uno/original.pdf') == b'%PDF-1.4 contenido del documento'
        assert not storage_service.get_backend().exists(
            _bucket(), 'documentos/posterior/original.pdf')

    def test_los_secretos_se_siguen_leyendo(self, admin, instalacion):
        from app.models.ai import AIProviderConfig
        from app.utils.crypto import decrypt_secret

        _, datos = _copia(admin)
        _restaurar(admin, datos)

        assert settings_service.get('LDAP_CONTRASENA') == CONTRASENA_LDAP
        config = AIProviderConfig.query.filter_by(nombre='OpenAI').one()
        assert decrypt_secret(config.api_key_encrypted) == CLAVE_API

    def test_la_auditoria_sigue_integra_y_dice_que_se_sustituyo(self, admin, instalacion):
        from app.models.audit import AuditEvent
        from app.services import audit_service

        _, datos = _copia(admin)
        _restaurar(admin, datos)

        ok, problemas = audit_service.verify_chain()
        assert ok, problemas
        ultimo = AuditEvent.query.order_by(AuditEvent.id.desc()).first()
        assert ultimo.accion == 'backup.restored'
        assert ultimo.metadatos['eventos_sustituidos'] > 0

    def test_el_seguimiento_de_la_restauracion_sobrevive_a_ella(self, admin, instalacion):
        """A restore that wiped its own job would finish in silence."""
        _, datos = _copia(admin)

        trabajo = _restaurar(admin, datos)

        assert db.session.get(BackupJob, trabajo.id) is not None


@pytest.mark.integration
class TestConOtraClaveDeCifrado:
    """Restored on another installation, the secrets are re-encrypted."""

    def test_los_secretos_se_recifran_con_la_clave_local(self, app, admin, instalacion):
        from cryptography.fernet import Fernet

        from app.models.ai import AIProviderConfig
        from app.utils.crypto import decrypt_secret

        _, datos = _copia(admin)
        original = app.config['SECRETS_ENCRYPTION_KEY']
        app.config['SECRETS_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
        try:
            trabajo = _restaurar(admin, datos)

            assert trabajo.estado is BackupJobState.COMPLETADO, trabajo.error
            assert settings_service.get('LDAP_CONTRASENA') == CONTRASENA_LDAP
            config = AIProviderConfig.query.filter_by(nombre='OpenAI').one()
            assert decrypt_secret(config.api_key_encrypted) == CLAVE_API
        finally:
            app.config['SECRETS_ENCRYPTION_KEY'] = original

    def test_no_queda_ninguna_columna_cifrada_sin_recifrar(self):
        """A column newly encrypted and not listed would restore as noise."""
        raiz = Path(__file__).resolve().parent.parent / 'app'
        asignadas = set()
        for ruta in raiz.rglob('*.py'):
            asignadas |= set(re.findall(r'\.(\w+)\s*=\s*encrypt_secret\(', ruta.read_text()))
        # settings_service encrypts into a local variable that becomes «valor».
        conocidas = {campo for _modelo, campo in backup_service._campos_cifrados()}
        assert asignadas <= conocidas, asignadas - conocidas


@pytest.mark.integration
class TestSeRechazaAntesDeTocarNada:
    def test_una_frase_equivocada(self, admin, instalacion):
        _, datos = _copia(admin)

        with pytest.raises(ValidationError, match='frase de paso no es'):
            _restaurar(admin, datos, frase='otra frase distinta')

    def test_una_copia_manipulada(self, admin, instalacion):
        from app.models.trip import Trip

        _, datos = _copia(admin)
        entrada = zipfile.ZipFile(io.BytesIO(datos))
        salida = io.BytesIO()
        with zipfile.ZipFile(salida, 'w') as zf:
            for nombre in entrada.namelist():
                contenido = entrada.read(nombre)
                if nombre.startswith('tablas/trips/'):
                    contenido = contenido[:-1] + bytes([contenido[-1] ^ 1])
                zf.writestr(nombre, contenido)
        instalacion.titulo = 'Estado actual'
        db.session.commit()

        trabajo = _restaurar(admin, salida.getvalue())

        assert trabajo.estado is BackupJobState.ERROR
        assert 'dañada o ha sido modificada' in trabajo.error
        assert db.session.get(Trip, instalacion.id).titulo == 'Estado actual'

    def test_una_copia_a_la_que_le_falta_un_trozo(self, admin, instalacion):
        _, datos = _copia(admin)
        entrada = zipfile.ZipFile(io.BytesIO(datos))
        salida = io.BytesIO()
        with zipfile.ZipFile(salida, 'w') as zf:
            for nombre in entrada.namelist():
                if not nombre.startswith('objetos/'):
                    zf.writestr(nombre, entrada.read(nombre))

        trabajo = _restaurar(admin, salida.getvalue())

        assert trabajo.estado is BackupJobState.ERROR
        assert 'incompleta' in trabajo.error

    def test_otra_version_del_esquema(self, admin, instalacion, monkeypatch):
        _, datos = _copia(admin)
        monkeypatch.setattr(backup_service, 'revision_del_esquema', lambda: 'otra')

        with pytest.raises(ValidationError, match='versión'):
            _restaurar(admin, datos)

    def test_un_zip_que_no_es_una_copia(self, admin, instalacion):
        salida = io.BytesIO()
        with zipfile.ZipFile(salida, 'w') as zf:
            zf.writestr('hola.txt', 'hola')

        with pytest.raises(ValidationError, match='no es una copia'):
            _restaurar(admin, salida.getvalue())

    def test_sin_escribir_la_confirmacion(self, admin, instalacion):
        _, datos = _copia(admin)

        with pytest.raises(ValidationError, match='RESTAURAR'):
            backup_service.solicitar_restauracion(admin, _Fichero(datos), FRASE, 'si')


@pytest.mark.security
class TestElZipNoDejaLeerNada:
    def test_ni_secretos_ni_datos_en_claro(self, admin, instalacion):
        _, datos = _copia(admin)

        for aguja in (CLAVE_API, CONTRASENA_LDAP, instalacion.titulo,
                      'contenido del documento', 'admin@example.test'):
            assert aguja.encode() not in datos, aguja

    def test_la_frase_no_se_guarda(self, admin, instalacion):
        trabajo, _ = _copia(admin)

        fila = {c.name: getattr(trabajo, c.name) for c in BackupJob.__table__.columns}
        assert FRASE not in repr(fila)

    def test_la_frase_viaja_cifrada_por_la_cola(self, app):
        protegida = backup_service._proteger_frase(FRASE)

        assert FRASE not in protegida
        assert backup_service._recuperar_frase(protegida) == FRASE

    def test_frases_cortas_o_distintas(self, admin, instalacion):
        with pytest.raises(ValidationError, match='al menos'):
            backup_service.solicitar_copia(admin, 'corta', 'corta')
        with pytest.raises(ValidationError, match='no coinciden'):
            backup_service.solicitar_copia(admin, FRASE, FRASE + 'x')

    def test_una_copia_no_contiene_las_anteriores(self, admin, instalacion):
        _copia(admin)
        _, segunda = _copia(admin)

        nombres = zipfile.ZipFile(io.BytesIO(segunda)).namelist()
        assert not [n for n in nombres if backup_service.PREFIJO_COPIAS in n]


@pytest.mark.unit
class TestLosValores:
    @pytest.mark.parametrize('valor', [
        None, True, 3, 2.5, 'texto',
        __import__('uuid').uuid4(),
        __import__('decimal').Decimal('12.34'),
        __import__('datetime').datetime(2026, 9, 25, 10, 0,
                                        tzinfo=__import__('datetime').UTC),
        __import__('datetime').date(2026, 9, 25),
        b'\x00\x01binario',
        {'$u': 'un JSON que parece una etiqueta'},
        [1, 2, {'a': None}],
    ])
    def test_vuelven_como_eran(self, valor):
        import json

        crudo = json.loads(json.dumps(backup_service._codificar(valor)))
        assert backup_service._decodificar(crudo) == valor


@pytest.mark.integration
class TestLaPantalla:
    def test_se_ve_y_se_llega_desde_administracion(self, as_user, admin, seeded):
        with as_user(admin) as client:
            assert client.get('/admin/copias').status_code == 200
            assert '/admin/copias' in client.get('/admin/').get_data(as_text=True)

    def test_hacer_descargar_y_restaurar(self, as_user, admin, instalacion):
        with as_user(admin) as client:
            client.post('/admin/copias/crear', data={'frase': FRASE, 'frase2': FRASE})
            trabajo = BackupJob.query.one()
            respuesta = client.get(f'/admin/copias/{trabajo.id}/descargar')
            assert respuesta.status_code == 200
            assert respuesta.headers['Content-Disposition'].startswith('attachment')
            datos = respuesta.data

            respuesta = client.post('/admin/copias/importar', data={
                'fichero': (io.BytesIO(datos), 'copia.zip'),
                'frase': FRASE, 'confirmacion': 'RESTAURAR',
            }, content_type='multipart/form-data')
            assert respuesta.status_code == 302

        restauracion = BackupJob.query.filter_by(tipo='restauracion').one()
        assert restauracion.estado is BackupJobState.COMPLETADO, restauracion.error

    def test_importar_admite_mas_que_un_documento_suelto(
        self, app, as_user, admin, instalacion,
    ):
        """The global 32 MB limit would refuse any real backup."""
        _, datos = _copia(admin)
        limite = app.config['MAX_CONTENT_LENGTH']
        app.config['MAX_CONTENT_LENGTH'] = len(datos) // 2
        try:
            with as_user(admin) as client:
                respuesta = client.post('/admin/copias/importar', data={
                    'fichero': (io.BytesIO(datos), 'copia.zip'),
                    'frase': FRASE, 'confirmacion': 'RESTAURAR',
                }, content_type='multipart/form-data')
        finally:
            app.config['MAX_CONTENT_LENGTH'] = limite

        assert respuesta.status_code == 302

    def test_esta_en_el_menu_de_administracion(self, as_user, admin, gestor, seeded):
        with as_user(admin) as client:
            assert 'Copias de seguridad' in client.get('/dashboard/').get_data(as_text=True)
        with as_user(gestor) as client:
            assert 'Copias de seguridad' not in client.get('/dashboard/').get_data(as_text=True)

    def test_un_gestor_no_entra(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            assert client.get('/admin/copias').status_code in (403, 404)

    def test_no_dos_a_la_vez(self, admin, instalacion, monkeypatch):
        monkeypatch.setattr(backup_service, '_despachar', lambda *a: None)
        backup_service.solicitar_copia(admin, FRASE, FRASE)

        with pytest.raises(ConflictError):
            backup_service.solicitar_copia(admin, FRASE, FRASE)
