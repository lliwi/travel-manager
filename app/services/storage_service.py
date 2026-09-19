"""Document storage behind a swappable backend.

Specification section 3.1: attachments live outside PostgreSQL, with only
metadata, the hash and the object key in the database. Section 3.2 adds that
downloads must be authorised server-side and that dangerous uploads must not be
servable.

Two buckets, deliberately:

* **quarantine** -- where an upload lands before validation and the antivirus
  scan. Nothing ever reads a file from here except the pipeline.
* **documents** -- the permanent home, written only after the file is proven
  clean and its hash re-verified.
"""
import io
import logging
import threading

from flask import current_app

from app.utils.errors import StorageError

logger = logging.getLogger(__name__)

#: Cached clients, per application instance.
_clients = {}
_lock = threading.Lock()


# ======================================================================
# Backends
# ======================================================================
class StorageBackend:
    """What every storage backend must provide."""

    name = 'base'

    def put(self, bucket, key, stream, content_type=None, metadata=None):
        raise NotImplementedError

    def get(self, bucket, key):
        """Return the object's bytes as a file-like object."""
        raise NotImplementedError

    def stream(self, bucket, key, chunk_size=1024 * 256):
        """Yield the object in chunks, so a large file never sits in memory."""
        data = self.get(bucket, key)
        while True:
            chunk = data.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def copy(self, src_bucket, src_key, dst_bucket, dst_key):
        raise NotImplementedError

    def delete(self, bucket, key):
        raise NotImplementedError

    def exists(self, bucket, key):
        raise NotImplementedError

    def presigned_url(self, bucket, key, expires=60, filename=None):
        """A temporary direct URL, or None when the backend cannot issue one."""
        return None

    def ensure_buckets(self, *buckets):
        raise NotImplementedError


class S3Backend(StorageBackend):
    """S3-compatible storage (MinIO in the reference deployment)."""

    name = 's3'

    def __init__(self, config):
        import boto3
        from botocore.client import Config as BotoConfig

        self.client = boto3.client(
            's3',
            endpoint_url=config['S3_ENDPOINT_URL'],
            aws_access_key_id=config['S3_ACCESS_KEY'],
            aws_secret_access_key=config['S3_SECRET_KEY'],
            region_name=config['S3_REGION'],
            use_ssl=config['S3_USE_SSL'],
            config=BotoConfig(signature_version='s3v4', retries={'max_attempts': 3}),
        )

    def put(self, bucket, key, stream, content_type=None, metadata=None):
        try:
            if hasattr(stream, 'seek'):
                stream.seek(0)
            self.client.upload_fileobj(
                stream, bucket, key,
                ExtraArgs={
                    k: v for k, v in {
                        'ContentType': content_type or 'application/octet-stream',
                        'Metadata': {
                            str(mk): str(mv) for mk, mv in (metadata or {}).items()
                        } or None,
                    }.items() if v is not None
                },
            )
        except Exception as exc:
            raise StorageError(f'No se pudo almacenar el objeto: {exc}') from exc
        return key

    def get(self, bucket, key):
        try:
            buffer = io.BytesIO()
            self.client.download_fileobj(bucket, key, buffer)
            buffer.seek(0)
            return buffer
        except Exception as exc:
            raise StorageError(f'No se pudo recuperar el objeto: {exc}') from exc

    def copy(self, src_bucket, src_key, dst_bucket, dst_key):
        try:
            self.client.copy_object(
                Bucket=dst_bucket,
                Key=dst_key,
                CopySource={'Bucket': src_bucket, 'Key': src_key},
            )
        except Exception as exc:
            raise StorageError(f'No se pudo copiar el objeto: {exc}') from exc
        return dst_key

    def delete(self, bucket, key):
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
            return True
        except Exception as exc:
            logger.warning('No se pudo eliminar %s/%s: %s', bucket, key, exc)
            return False

    def exists(self, bucket, key):
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def presigned_url(self, bucket, key, expires=60, filename=None):
        """Issue a short-lived direct URL.

        The TTL is deliberately tiny: once issued, the URL bypasses our own
        authorisation, so it must not outlive the click that produced it. The
        response is forced to ``attachment`` with a generic content type so an
        uploaded SVG or HTML file cannot execute on our origin.
        """
        params = {
            'Bucket': bucket,
            'Key': key,
            'ResponseContentDisposition': _content_disposition(filename),
            'ResponseContentType': 'application/octet-stream',
        }
        try:
            return self.client.generate_presigned_url(
                'get_object', Params=params, ExpiresIn=expires
            )
        except Exception as exc:
            logger.warning('No se pudo firmar la URL de %s/%s: %s', bucket, key, exc)
            return None

    def ensure_buckets(self, *buckets):
        """Create the buckets if they do not exist yet."""
        for bucket in buckets:
            try:
                self.client.head_bucket(Bucket=bucket)
            except Exception:
                try:
                    self.client.create_bucket(Bucket=bucket)
                    logger.info('Bucket creado: %s', bucket)
                except Exception as exc:
                    logger.error('No se pudo crear el bucket %s: %s', bucket, exc)


class MemoryBackend(StorageBackend):
    """In-process storage for the test suite."""

    name = 'memory'

    def __init__(self, config=None):
        self._objects = {}

    def put(self, bucket, key, stream, content_type=None, metadata=None):
        if hasattr(stream, 'seek'):
            stream.seek(0)
        self._objects[(bucket, key)] = {
            'data': stream.read(),
            'content_type': content_type,
            'metadata': metadata or {},
        }
        return key

    def get(self, bucket, key):
        entry = self._objects.get((bucket, key))
        if entry is None:
            raise StorageError(f'El objeto {bucket}/{key} no existe.')
        return io.BytesIO(entry['data'])

    def copy(self, src_bucket, src_key, dst_bucket, dst_key):
        entry = self._objects.get((src_bucket, src_key))
        if entry is None:
            raise StorageError(f'El objeto {src_bucket}/{src_key} no existe.')
        self._objects[(dst_bucket, dst_key)] = dict(entry)
        return dst_key

    def delete(self, bucket, key):
        return self._objects.pop((bucket, key), None) is not None

    def exists(self, bucket, key):
        return (bucket, key) in self._objects

    def ensure_buckets(self, *buckets):
        return True


BACKENDS = {'s3': S3Backend, 'memory': MemoryBackend}


def get_backend():
    """The configured backend, built once per application instance."""
    app = current_app._get_current_object()
    key = id(app)

    backend = _clients.get(key)
    if backend is not None:
        return backend

    with _lock:
        backend = _clients.get(key)
        if backend is None:
            name = app.config['STORAGE_BACKEND']
            backend_cls = BACKENDS.get(name)
            if backend_cls is None:
                raise StorageError(f'Backend de almacenamiento desconocido: {name}')
            backend = backend_cls(app.config)
            _clients[key] = backend
    return backend


def reset_backend():
    """Drop cached backends. Used between tests."""
    _clients.clear()


# ======================================================================
# Keys
# ======================================================================
def quarantine_key(document_id, extension):
    """Object key while the file awaits validation and scanning."""
    suffix = f'.{extension.lstrip(".")}' if extension else ''
    return f'cuarentena/{document_id}/original{suffix}'


def document_key(trip_id, document_id, extension):
    """Permanent object key, partitioned by trip so purging a trip is one prefix."""
    suffix = f'.{extension.lstrip(".")}' if extension else ''
    return f'documentos/{trip_id}/{document_id}/original{suffix}'


def buckets():
    """``(bucket_documentos, bucket_cuarentena)``."""
    return (
        current_app.config['S3_BUCKET_DOCUMENTS'],
        current_app.config['S3_BUCKET_QUARANTINE'],
    )


# ======================================================================
# Operations
# ======================================================================
def store_quarantine(document_id, extension, stream, content_type=None):
    """Put an incoming upload in quarantine."""
    _, quarantine = buckets()
    key = quarantine_key(document_id, extension)
    get_backend().ensure_buckets(*buckets())
    get_backend().put(quarantine, key, stream, content_type=content_type)
    return quarantine, key


def promote(document, verify_hash=True):
    """Move a scanned file from quarantine to permanent storage.

    The hash is recomputed from what actually landed in the destination, not
    trusted from the upload: section 5.2 promises the original stays available
    *with its hash*, and that promise is only worth something if verified after
    the write.
    """
    from app.utils.hashing import sha256_stream

    documents_bucket, quarantine = buckets()
    backend = get_backend()
    destination = document_key(document.trip_id, document.id, document.extension)

    backend.copy(quarantine, document.objeto_cuarentena, documents_bucket, destination)

    if verify_hash:
        stored = backend.get(documents_bucket, destination)
        digest, size = sha256_stream(stored)
        if digest != document.hash_sha256:
            backend.delete(documents_bucket, destination)
            raise StorageError(
                'El hash del objeto almacenado no coincide con el del archivo recibido.'
            )

    backend.delete(quarantine, document.objeto_cuarentena)
    return documents_bucket, destination


def open_document(document):
    """Open the stored original for reading."""
    if not document.objeto_storage:
        raise StorageError('El documento no tiene un objeto almacenado.')
    return get_backend().get(document.bucket, document.objeto_storage)


def stream_document(document, chunk_size=1024 * 256):
    """Stream the stored original in chunks."""
    return get_backend().stream(document.bucket, document.objeto_storage, chunk_size)


def presigned_download(document, expires=None):
    """A short-lived direct download URL, or None when unavailable."""
    expires = expires or current_app.config['STORAGE_PRESIGNED_TTL']
    return get_backend().presigned_url(
        document.bucket, document.objeto_storage,
        expires=expires, filename=document.nombre_original,
    )


def delete_document_object(document, include_quarantine=True):
    """Permanently remove a document's stored bytes.

    Used by the retention job and after an infected upload. Soft-deleting a
    document does *not* call this: the original is retained until the retention
    policy says otherwise.
    """
    backend = get_backend()
    deleted = False
    if document.objeto_storage and document.bucket:
        deleted = backend.delete(document.bucket, document.objeto_storage)
    if include_quarantine and document.objeto_cuarentena:
        _, quarantine = buckets()
        backend.delete(quarantine, document.objeto_cuarentena)
    return deleted


def _content_disposition(filename):
    """Force a download, with an ASCII-safe filename.

    Rendering an uploaded file inline would turn a malicious SVG or HTML upload
    into stored XSS on our own origin.
    """
    if not filename:
        return 'attachment'
    safe = ''.join(c if 32 <= ord(c) < 127 and c not in '"\\' else '_' for c in filename)
    return f'attachment; filename="{safe}"'
