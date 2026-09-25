"""Full backups as an encrypted ZIP, and restoring from one.

Everything goes in one file: every table (settings, AI configuration and
users included), every stored document, and the key the application encrypts
secrets with. The key has to travel: API keys, the directory password, MFA
secrets and travellers' document numbers are stored encrypted with it, and a
backup restored without it would bring those back as noise. Because it
travels, the whole backup is encrypted with a passphrase that is never
stored anywhere -- losing it is losing the backup, and the screen says so.

**The format.** A ZIP whose members are each encrypted with AES-256-GCM
under a key derived from the passphrase (scrypt), with the member's name as
associated data, so a member cannot be swapped for another or renamed without
the restore noticing. ``manifiesto.json`` is the one member in the clear --
what it contains and how to derive the key -- and an encrypted copy of it is
compared on restore, so its figures cannot be edited either.

**Tables are exported row by row, not with pg_dump.** Three reasons: the
test suite runs on SQLite and must be able to prove a round trip; the
secrets have to be re-encrypted when the copy is restored on an installation
with a different key, which needs the rows in hand; and a dump taken by a
newer client than the server -- which is what the web image carries -- is a
restore problem waiting for the day it is needed.

**A restore validates everything before it touches anything.** Wrong
passphrase, a different schema version, a truncated or tampered file: all
refused while the installation is still intact. Only then are the documents
written, the tables replaced in one transaction, and the objects that the copy
does not know removed.
"""
import base64
import datetime as dt
import decimal
import enum
import hashlib
import io
import json
import logging
import os
import tempfile
import uuid
import zipfile
import zlib

from flask import current_app

from app import __version__
from app.extensions import db
from app.models.backup import BackupJob
from app.models.enums import AuditResourceType, BackupJobState, BackupJobType
from app.services import audit_service, storage_service
from app.utils.errors import ConflictError, ResourceNotFound, ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

FORMATO = 1

#: Where backups and uploaded copies wait in the document store. Excluded
#: from every backup, or each copy would contain all the previous ones.
PREFIJO_COPIAS = 'copias-de-seguridad/'

#: Never exported, never replaced. See ``app/models/backup.py``.
TABLAS_EXCLUIDAS = frozenset({'backup_jobs'})

FILAS_POR_BLOQUE = 2000
LONGITUD_MINIMA_FRASE = 12
CONFIRMACION = 'RESTAURAR'

#: Largest ZIP the import accepts. Far above the 32 MB of a single document,
#: which is the global limit and would refuse any real backup.
TAMANO_MAXIMO_IMPORTACION = 4 * 1024 ** 3

_VERIFICADOR = b'travel-manager/copia-de-seguridad'
_SCRYPT = {'n': 2 ** 15, 'r': 8, 'p': 1}


# ======================================================================
# Encryption
# ======================================================================
def _derivar(frase, sal, n=_SCRYPT['n'], r=_SCRYPT['r'], p=_SCRYPT['p']):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=sal, length=32, n=n, r=r, p=p).derive(frase.encode())


def _asociado(nombre):
    return f'travel-manager/{FORMATO}/{nombre}'.encode()


def _cifrar(clave, nombre, datos):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(clave).encrypt(nonce, datos, _asociado(nombre))


def _descifrar(clave, nombre, blob):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(clave).decrypt(blob[:12], blob[12:], _asociado(nombre))
    except InvalidTag:
        raise ValidationError(
            f'La copia está dañada o ha sido modificada («{nombre}» no se '
            f'puede descifrar).'
        ) from None


def _validar_frase(frase):
    if not frase or len(frase) < LONGITUD_MINIMA_FRASE:
        raise ValidationError(
            f'La frase de paso debe tener al menos {LONGITUD_MINIMA_FRASE} caracteres.'
        )


def _proteger_frase(frase):
    """What travels through the queue: the passphrase, encrypted.

    Celery hands task arguments to Redis. A passphrase that unlocks a backup
    of everything must not sit there in the clear.
    """
    from app.utils.crypto import encrypt_secret

    return encrypt_secret(frase)


def _recuperar_frase(protegida):
    from app.utils.crypto import decrypt_secret

    return decrypt_secret(protegida)


# ======================================================================
# Values
# ======================================================================
def _codificar(valor):
    """A column value as JSON, tagged so it comes back as the same type.

    Tags only at the top level, and JSON columns always wrapped, so a JSON
    document that happens to contain «$u» is never mistaken for a UUID.
    """
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    if isinstance(valor, enum.Enum):
        return valor.value
    if isinstance(valor, str):
        return valor
    if isinstance(valor, dt.datetime):
        return {'$dt': valor.isoformat()}
    if isinstance(valor, dt.date):
        return {'$d': valor.isoformat()}
    if isinstance(valor, dt.time):
        return {'$t': valor.isoformat()}
    if isinstance(valor, uuid.UUID):
        return {'$u': str(valor)}
    if isinstance(valor, decimal.Decimal):
        return {'$n': str(valor)}
    if isinstance(valor, (bytes, bytearray, memoryview)):
        return {'$b': base64.b64encode(bytes(valor)).decode('ascii')}
    if isinstance(valor, (dict, list)):
        return {'$j': valor}
    raise ValidationError(f'Tipo de dato sin copia prevista: {type(valor).__name__}.')


def _decodificar(valor):
    if not isinstance(valor, dict):
        return valor
    (etiqueta, contenido), = valor.items()
    if etiqueta == '$dt':
        return dt.datetime.fromisoformat(contenido)
    if etiqueta == '$d':
        return dt.date.fromisoformat(contenido)
    if etiqueta == '$t':
        return dt.time.fromisoformat(contenido)
    if etiqueta == '$u':
        return uuid.UUID(contenido)
    if etiqueta == '$n':
        return decimal.Decimal(contenido)
    if etiqueta == '$b':
        return base64.b64decode(contenido)
    if etiqueta == '$j':
        return contenido
    raise ValidationError(f'Valor con una etiqueta desconocida: {etiqueta}.')


# ======================================================================
# What goes in
# ======================================================================
def _tablas():
    """Every table of the model, parents before children."""
    import app.models  # noqa: F401 -- every model registered on the metadata

    return [t for t in db.metadata.sorted_tables if t.name not in TABLAS_EXCLUIDAS]


def _cubos():
    """``(papel, bucket)``: named by role, so a copy restores onto an
    installation whose buckets are called something else."""
    documentos, cuarentena = storage_service.buckets()
    return (('documentos', documentos), ('cuarentena', cuarentena))


def _objetos(bucket):
    backend = storage_service.get_backend()
    return [k for k in backend.list_keys(bucket) if not k.startswith(PREFIJO_COPIAS)]


def revision_del_esquema():
    """The Alembic revision this database is at, or None without Alembic."""
    from alembic.runtime.migration import MigrationContext

    with db.engine.connect() as conexion:
        return MigrationContext.configure(conexion).get_current_revision()


def _claves_de_cifrado():
    return {
        'actual': current_app.config.get('SECRETS_ENCRYPTION_KEY'),
        'anterior': current_app.config.get('SECRETS_ENCRYPTION_KEY_PREVIOUS'),
    }


# ======================================================================
# Jobs
# ======================================================================
def solicitar_copia(actor, frase, confirmacion_frase):
    """Queue a full backup. Returns the job."""
    _validar_frase(frase)
    if frase != confirmacion_frase:
        raise ValidationError('Las dos frases de paso no coinciden.')
    _sin_otra_en_marcha()

    trabajo = _nuevo_trabajo(actor, BackupJobType.COPIA)
    _audit(actor, 'backup.requested', trabajo)
    _despachar('run_backup', trabajo, frase)
    return trabajo


def solicitar_restauracion(actor, fichero, frase, confirmacion):
    """Check an uploaded copy and queue its restore. Returns the job.

    What can be checked quickly is checked here, while the person is still
    looking at the form: that it is one of our copies, of this schema version,
    and that the passphrase opens it. A mistyped passphrase found out in the
    worker ten minutes later is a worse answer to the same question.
    """
    if (confirmacion or '').strip() != CONFIRMACION:
        raise ValidationError(
            f'Escriba {CONFIRMACION} para confirmar que quiere sustituir todos '
            f'los datos actuales.'
        )
    _validar_frase(frase)
    if fichero is None or not getattr(fichero, 'filename', ''):
        raise ValidationError('Elija el fichero ZIP de la copia.')
    _sin_otra_en_marcha()

    flujo = fichero.stream
    flujo.seek(0)
    try:
        with zipfile.ZipFile(flujo) as zf:
            manifiesto = _leer_manifiesto(zf)
            _comprobar_compatible(manifiesto)
            _clave_de(zf, manifiesto, frase)
    except zipfile.BadZipFile:
        raise ValidationError('El fichero no es un ZIP válido.') from None

    trabajo = _nuevo_trabajo(actor, BackupJobType.RESTAURACION)
    flujo.seek(0)
    bucket, _ = storage_service.buckets()
    trabajo.objeto = f'{PREFIJO_COPIAS}importadas/{trabajo.id}.zip'
    trabajo.nombre_fichero = os.path.basename(fichero.filename)[:200]
    storage_service.get_backend().put(bucket, trabajo.objeto, flujo,
                                      content_type='application/zip')
    trabajo.resumen = _resumen_del_manifiesto(manifiesto)
    db.session.commit()

    _audit(actor, 'backup.restore_requested', trabajo, {
        'fichero': trabajo.nombre_fichero,
        'copia_creada_en': manifiesto.get('creado_en'),
    })
    _despachar('run_restore', trabajo, frase)
    return trabajo


def _sin_otra_en_marcha():
    if BackupJob.query.filter(BackupJob.estado.in_(
        [BackupJobState.PENDIENTE.value, BackupJobState.EN_CURSO.value]
    )).first():
        # A copy taken while a restore rewrites the tables would be neither
        # the old state nor the new one.
        raise ConflictError('Ya hay una copia o una restauración en marcha.')


def _nuevo_trabajo(actor, tipo):
    trabajo = BackupJob(
        tipo=tipo, estado=BackupJobState.PENDIENTE,
        lanzado_por_id=getattr(actor, 'id', None),
        lanzado_por_nombre=getattr(actor, 'nombre_completo', None),
    )
    db.session.add(trabajo)
    db.session.commit()
    return trabajo


def _despachar(tarea, trabajo, frase):
    from app.tasks.dispatch import is_eager

    if is_eager():
        (ejecutar_copia if tarea == 'run_backup' else ejecutar_restauracion)(
            trabajo.id, frase,
        )
        return

    try:
        from app.tasks import backup_tasks

        getattr(backup_tasks, tarea).apply_async(
            args=[str(trabajo.id), _proteger_frase(frase)],
        )
    except Exception as exc:
        logger.exception('No se pudo encolar %s', trabajo.id)
        _fallar(trabajo.id, f'No se pudo encolar: {exc}')


# ======================================================================
# Making a copy
# ======================================================================
def ejecutar_copia(trabajo_id, frase):
    trabajo = get_or_404(trabajo_id)
    if trabajo.estado is not BackupJobState.PENDIENTE:
        return trabajo
    _empezar(trabajo, 'Exportando la base de datos…')

    ruta = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            ruta = tmp.name
        manifiesto = _escribir_copia(ruta, frase, trabajo)

        with open(ruta, 'rb') as fichero:
            suma = hashlib.sha256()
            for bloque in iter(lambda: fichero.read(1024 * 1024), b''):
                suma.update(bloque)
            fichero.seek(0)
            bucket, _ = storage_service.buckets()
            trabajo.objeto = f'{PREFIJO_COPIAS}{trabajo.id}.zip'
            storage_service.get_backend().put(bucket, trabajo.objeto, fichero,
                                              content_type='application/zip')

        trabajo.tamano = os.path.getsize(ruta)
        trabajo.sha256 = suma.hexdigest()
        trabajo.nombre_fichero = (
            f'travel-manager-{trabajo.created_at:%Y%m%d-%H%M%S}.zip'
        )
        trabajo.resumen = _resumen_del_manifiesto(manifiesto)
        _terminar(trabajo)
        _audit(trabajo.lanzado_por, 'backup.created', trabajo, {
            'tamano': trabajo.tamano, 'sha256': trabajo.sha256,
            'filas': sum(manifiesto['tablas'].values()),
            'objetos': sum(manifiesto['objetos'].values()),
        })
    except Exception as exc:
        logger.exception('La copia %s falló', trabajo_id)
        _fallar(trabajo_id, str(getattr(exc, 'mensaje', None) or exc))
    finally:
        if ruta and os.path.exists(ruta):
            os.remove(ruta)
    return get_or_404(trabajo_id)


def _escribir_copia(ruta, frase, trabajo):
    sal = os.urandom(16)
    clave = _derivar(frase, sal)
    manifiesto = {
        'aplicacion': 'Travel Manager',
        'formato': FORMATO,
        'version': __version__,
        'revision': revision_del_esquema(),
        'creado_en': utcnow().isoformat(),
        'kdf': dict(_SCRYPT, algoritmo='scrypt',
                    sal=base64.b64encode(sal).decode('ascii')),
        'tablas': {},
        'objetos': {},
    }

    def guardar(zf, nombre, datos):
        # Stored, not deflated: ciphertext does not compress, and the tables
        # are compressed before being encrypted.
        zf.writestr(zipfile.ZipInfo(nombre), _cifrar(clave, nombre, datos),
                    compress_type=zipfile.ZIP_STORED)

    with zipfile.ZipFile(ruta, 'w', allowZip64=True) as zf:
        guardar(zf, 'verificador', _VERIFICADOR)
        guardar(zf, 'clave', json.dumps(_claves_de_cifrado()).encode('utf-8'))

        for tabla in _tablas():
            filas = 0
            bloque, numero = [], 0
            consulta = db.select(tabla).order_by(*tabla.primary_key.columns)
            for fila in db.session.execute(consulta).mappings():
                bloque.append({c: _codificar(v) for c, v in fila.items()})
                if len(bloque) >= FILAS_POR_BLOQUE:
                    guardar(zf, _nombre_bloque(tabla.name, numero), _empaquetar(bloque))
                    filas += len(bloque)
                    bloque, numero = [], numero + 1
            if bloque:
                guardar(zf, _nombre_bloque(tabla.name, numero), _empaquetar(bloque))
                filas += len(bloque)
            manifiesto['tablas'][tabla.name] = filas

        _progreso(trabajo, 'Copiando los documentos…')
        backend = storage_service.get_backend()
        for papel, bucket in _cubos():
            claves = _objetos(bucket)
            for clave_objeto in claves:
                guardar(zf, f'objetos/{papel}/{clave_objeto}',
                        backend.get(bucket, clave_objeto).read())
            manifiesto['objetos'][papel] = len(claves)

        crudo = json.dumps(manifiesto, ensure_ascii=False, indent=2).encode('utf-8')
        zf.writestr('manifiesto.json', crudo)
        guardar(zf, 'manifiesto.cifrado', crudo)
    return manifiesto


def _nombre_bloque(tabla, numero):
    return f'tablas/{tabla}/{numero:06d}'


def _empaquetar(filas):
    return zlib.compress(
        '\n'.join(json.dumps(f, ensure_ascii=False) for f in filas).encode('utf-8')
    )


def _desempaquetar(datos):
    texto = zlib.decompress(datos).decode('utf-8')
    return [json.loads(linea) for linea in texto.split('\n') if linea]


# ======================================================================
# Restoring
# ======================================================================
def _leer_manifiesto(zf):
    try:
        manifiesto = json.loads(zf.read('manifiesto.json'))
    except KeyError:
        raise ValidationError('El ZIP no es una copia de Travel Manager.') from None
    if manifiesto.get('aplicacion') != 'Travel Manager':
        raise ValidationError('El ZIP no es una copia de Travel Manager.')
    return manifiesto


def _comprobar_compatible(manifiesto):
    if manifiesto.get('formato') != FORMATO:
        raise ValidationError(
            f'La copia usa el formato {manifiesto.get("formato")}; esta versión '
            f'solo sabe leer el {FORMATO}.'
        )
    actual = revision_del_esquema()
    if manifiesto.get('revision') != actual:
        # Rows from another schema would have to be migrated on the way in,
        # and a restore is the wrong moment to guess how.
        raise ValidationError(
            f'La copia es de la versión «{manifiesto.get("revision")}» del '
            f'esquema y esta instalación está en «{actual}». Restáurela en una '
            f'instalación de la misma versión y actualícela después.'
        )


def _clave_de(zf, manifiesto, frase):
    kdf = manifiesto.get('kdf') or {}
    clave = _derivar(frase, base64.b64decode(kdf['sal']),
                     n=kdf['n'], r=kdf['r'], p=kdf['p'])
    try:
        verificador = _descifrar(clave, 'verificador', zf.read('verificador'))
    except ValidationError:
        raise ValidationError('La frase de paso no es la de esta copia.') from None
    if verificador != _VERIFICADOR:
        raise ValidationError('La frase de paso no es la de esta copia.')
    return clave


def ejecutar_restauracion(trabajo_id, frase):
    trabajo = get_or_404(trabajo_id)
    if trabajo.estado is not BackupJobState.PENDIENTE:
        return trabajo
    _empezar(trabajo, 'Comprobando la copia…')

    ruta = None
    try:
        bucket, _ = storage_service.buckets()
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            ruta = tmp.name
            storage_service.get_backend().download_to(bucket, trabajo.objeto, tmp)

        with zipfile.ZipFile(ruta) as zf:
            manifiesto = _leer_manifiesto(zf)
            _comprobar_compatible(manifiesto)
            clave = _clave_de(zf, manifiesto, frase)
            _validar_entera(zf, clave, manifiesto)

            antes = _estado_de_la_auditoria()
            _progreso(trabajo, 'Restaurando los documentos…')
            conservados = _restaurar_objetos(zf, clave)
            _progreso(trabajo, 'Sustituyendo la base de datos…')
            _restaurar_tablas(zf, clave, manifiesto)
            _limpiar_objetos(conservados)

        storage_service.get_backend().delete(bucket, trabajo.objeto)
        trabajo = get_or_404(trabajo_id)
        trabajo.objeto = None
        _terminar(trabajo)
        # Appended to the restored chain: the trail says it was replaced, and
        # how long the one it replaced was, so the gap is declared.
        _audit(None, 'backup.restored', trabajo, {
            'copia_creada_en': manifiesto.get('creado_en'),
            'lanzado_por': trabajo.lanzado_por_nombre,
            'eventos_sustituidos': antes['eventos'],
            'ultimo_hash_sustituido': antes['ultimo_hash'],
        })
    except Exception as exc:
        db.session.rollback()
        logger.exception('La restauración %s falló', trabajo_id)
        _fallar(trabajo_id, str(getattr(exc, 'mensaje', None) or exc))
    finally:
        if ruta and os.path.exists(ruta):
            os.remove(ruta)
    return get_or_404(trabajo_id)


def _validar_entera(zf, clave, manifiesto):
    """Decrypt every member once before applying any of them.

    GCM authenticates each member, so this finds a truncated download or a
    tampered file while nothing has been touched yet.
    """
    cifrado = _descifrar(clave, 'manifiesto.cifrado', zf.read('manifiesto.cifrado'))
    if json.loads(cifrado) != manifiesto:
        raise ValidationError('El manifiesto de la copia ha sido modificado.')

    esperadas = {t.name for t in _tablas()}
    if set(manifiesto['tablas']) != esperadas:
        raise ValidationError('Las tablas de la copia no son las de esta instalación.')

    filas = dict.fromkeys(manifiesto['tablas'], 0)
    objetos = dict.fromkeys(manifiesto['objetos'], 0)
    for nombre in zf.namelist():
        if nombre in ('manifiesto.json',):
            continue
        datos = _descifrar(clave, nombre, zf.read(nombre))
        if nombre.startswith('tablas/'):
            filas[nombre.split('/')[1]] += len(_desempaquetar(datos))
        elif nombre.startswith('objetos/'):
            objetos[nombre.split('/')[1]] += 1

    if filas != manifiesto['tablas'] or objetos != manifiesto['objetos']:
        raise ValidationError('A la copia le faltan partes: está incompleta.')


def _restaurar_objetos(zf, clave):
    """Write every document back. Returns ``{bucket: {claves}}`` it holds."""
    backend = storage_service.get_backend()
    cubos = dict(_cubos())
    conservados = {bucket: set() for bucket in cubos.values()}
    for nombre in zf.namelist():
        if not nombre.startswith('objetos/'):
            continue
        _, papel, clave_objeto = nombre.split('/', 2)
        bucket = cubos[papel]
        backend.put(bucket, clave_objeto,
                    io.BytesIO(_descifrar(clave, nombre, zf.read(nombre))))
        conservados[bucket].add(clave_objeto)
    return conservados


def _limpiar_objetos(conservados):
    """Remove what the copy does not know, once the tables no longer point at it."""
    backend = storage_service.get_backend()
    for bucket, claves in conservados.items():
        for clave_objeto in _objetos(bucket):
            if clave_objeto not in claves:
                backend.delete(bucket, clave_objeto)


def _restaurar_tablas(zf, clave, manifiesto):
    """Replace every table, in one transaction.

    References to a row inserted later -- a user who created another user,
    a table referencing one that comes after it -- are written empty on insert
    and filled in once every row exists, since a foreign key checked at once
    would refuse them otherwise.
    """
    tablas = _tablas()
    posicion = {t.name: i for i, t in enumerate(tablas)}
    bloques = {}
    for nombre in sorted(n for n in zf.namelist() if n.startswith('tablas/')):
        bloques.setdefault(nombre.split('/')[1], []).append(nombre)

    for tabla in reversed(tablas):
        db.session.execute(tabla.delete())

    pendientes = []
    for tabla in tablas:
        diferidas = {
            fk.parent.name for fk in tabla.foreign_keys
            if fk.parent.nullable
            and posicion.get(fk.column.table.name, -1) >= posicion[tabla.name]
        }
        clave_primaria = [c.name for c in tabla.primary_key.columns]
        for nombre in bloques.get(tabla.name, []):
            filas = [
                {c: _decodificar(v) for c, v in f.items()}
                for f in _desempaquetar(_descifrar(clave, nombre, zf.read(nombre)))
            ]
            for fila in filas:
                aplazado = {c: fila[c] for c in diferidas if fila.get(c) is not None}
                if aplazado:
                    pendientes.append((tabla, {c: fila[c] for c in clave_primaria}, aplazado))
                    fila.update(dict.fromkeys(aplazado))
            if filas:
                db.session.execute(tabla.insert(), filas)

    for tabla, pk, valores in pendientes:
        condicion = db.and_(*(tabla.c[c] == v for c, v in pk.items()))
        db.session.execute(tabla.update().where(condicion).values(**valores))

    _recifrar(zf, clave)
    _reajustar_secuencias(tablas)
    db.session.commit()


def _campos_cifrados():
    """Every column holding something encrypted with the application key.

    ``tests/test_copias_desde_el_panel.py`` fails if a model starts
    encrypting a column that is not listed here: restored on another
    installation, it would come back unreadable and nothing would say so.
    """
    from app.models.ai import AIProviderConfig
    from app.models.settings import TravelerDocument
    from app.models.user import User

    return (
        (AIProviderConfig, 'api_key_encrypted'),
        (TravelerDocument, 'numero_encrypted'),
        (User, 'mfa_secret_cifrado'),
    )


def _recifrar(zf, clave):
    """Re-encrypt the secrets when this installation uses another key."""
    from cryptography.fernet import Fernet, MultiFernet

    from app.models.settings import SystemSetting
    from app.services import settings_service
    from app.utils.crypto import _normalise_key, encrypt_secret

    origen = json.loads(_descifrar(clave, 'clave', zf.read('clave')))
    propias = _claves_de_cifrado()
    if _normalise_key(origen['actual']) == _normalise_key(propias['actual']):
        return

    fernet = MultiFernet([
        Fernet(_normalise_key(k)) for k in (origen['actual'], origen.get('anterior')) if k
    ])

    def traducir(token):
        return encrypt_secret(fernet.decrypt(token.encode()).decode()) if token else token

    for modelo, campo in _campos_cifrados():
        for fila in modelo.query.filter(getattr(modelo, campo).isnot(None)).all():
            setattr(fila, campo, traducir(getattr(fila, campo)))

    for ajuste in SystemSetting.query.all():
        if settings_service.es_secreto(ajuste.clave) and ajuste.valor:
            crudo = ajuste.valor.get('v') if isinstance(ajuste.valor, dict) else None
            if crudo:
                ajuste.valor = {'v': traducir(crudo)}
    db.session.flush()


def _reajustar_secuencias(tablas):
    """Move PostgreSQL's counters past the ids the copy brought back.

    Otherwise the next audit event would be given an id that already exists.
    """
    if db.engine.dialect.name != 'postgresql':
        return
    for tabla in tablas:
        for columna in tabla.primary_key.columns:
            if not isinstance(columna.type, db.Integer):
                continue
            maximo = db.select(db.func.max(columna)).scalar_subquery()
            secuencia = db.func.pg_get_serial_sequence(tabla.name, columna.name)
            # setval is strict: for a column with no sequence behind it the
            # lookup is NULL and the call does nothing, which is what we want.
            db.session.execute(db.select(db.func.setval(
                secuencia, db.func.coalesce(maximo, 1), maximo.isnot(None),
            )))


def _estado_de_la_auditoria():
    from app.models.audit import AuditEvent

    ultimo = AuditEvent.query.order_by(AuditEvent.id.desc()).first()
    return {
        'eventos': AuditEvent.query.count(),
        'ultimo_hash': ultimo.hash_propio if ultimo else None,
    }


# ======================================================================
# Downloading and housekeeping
# ======================================================================
def descargar(actor, trabajo):
    """The ZIP as a stream of chunks. Recorded: it contains everything."""
    if not trabajo.descargable:
        raise ValidationError('Esta copia no se puede descargar.')
    bucket, _ = storage_service.buckets()
    flujo = storage_service.get_backend().stream(bucket, trabajo.objeto)
    trabajo.descargado_en = utcnow()
    db.session.commit()
    _audit(actor, 'backup.downloaded', trabajo)
    return flujo


def eliminar(actor, trabajo):
    """Remove the ZIP from the server, keeping the record that it existed."""
    if trabajo.activo:
        raise ValidationError('No se puede eliminar mientras está en marcha.')
    if trabajo.objeto:
        bucket, _ = storage_service.buckets()
        storage_service.get_backend().delete(bucket, trabajo.objeto)
        trabajo.objeto = None
        db.session.commit()
        _audit(actor, 'backup.deleted', trabajo)
    return trabajo


def recientes(limite=30):
    return BackupJob.query.order_by(BackupJob.created_at.desc()).limit(limite).all()


def get_or_404(trabajo_id):
    try:
        trabajo = db.session.get(BackupJob, uuid.UUID(str(trabajo_id)))
    except (ValueError, TypeError):
        trabajo = None
    if trabajo is None:
        raise ResourceNotFound('La copia indicada no existe.')
    return trabajo


def _resumen_del_manifiesto(manifiesto):
    return {
        'creado_en': manifiesto.get('creado_en'),
        'version': manifiesto.get('version'),
        'revision': manifiesto.get('revision'),
        'filas': sum((manifiesto.get('tablas') or {}).values()),
        'tablas': len(manifiesto.get('tablas') or {}),
        'objetos': sum((manifiesto.get('objetos') or {}).values()),
    }


def _empezar(trabajo, progreso):
    trabajo.estado = BackupJobState.EN_CURSO
    trabajo.iniciado_en = utcnow()
    trabajo.progreso = progreso
    db.session.commit()


def _progreso(trabajo, texto):
    trabajo.progreso = texto
    db.session.commit()


def _terminar(trabajo):
    trabajo.estado = BackupJobState.COMPLETADO
    trabajo.progreso = None
    trabajo.terminado_en = utcnow()
    db.session.commit()


def _fallar(trabajo_id, mensaje):
    db.session.rollback()
    trabajo = get_or_404(trabajo_id)
    trabajo.estado = BackupJobState.ERROR
    trabajo.error = mensaje[:500]
    trabajo.progreso = None
    trabajo.terminado_en = utcnow()
    db.session.commit()
    _audit(None, 'backup.failed', trabajo, {'error': trabajo.error})
    return trabajo


def _audit(actor, accion, trabajo, extra=None):
    metadatos = {'tipo': str(trabajo.tipo)}
    metadatos.update(extra or {})
    audit_service.record(
        accion,
        recurso_tipo=AuditResourceType.CONFIGURACION,
        recurso_id=str(trabajo.id),
        actor=actor,
        metadatos=metadatos,
    )
