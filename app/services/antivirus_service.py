"""Antimalware scanning behind a swappable adapter.

Specification sections 3.2 and 6: every upload is scanned before processing,
through ClamAV or a corporate service reached by an adapter.
"""
import logging

from flask import current_app

from app.utils.errors import AntivirusError, TransientError

logger = logging.getLogger(__name__)


class ScanResult:
    """The outcome of scanning one file."""

    __slots__ = ('limpio', 'firma', 'motor', 'detalle')

    def __init__(self, limpio, firma=None, motor=None, detalle=None):
        self.limpio = limpio
        self.firma = firma
        self.motor = motor
        self.detalle = detalle

    def __bool__(self):
        return self.limpio

    def __repr__(self):
        estado = 'limpio' if self.limpio else f'infectado ({self.firma})'
        return f'<ScanResult {estado}>'


class AntivirusScanner:
    """What every scanner adapter must provide."""

    name = 'base'

    def scan(self, stream):
        """Scan a file-like object. Returns a :class:`ScanResult`."""
        raise NotImplementedError

    def health_check(self):
        """Report reachability as ``(ok, detalle)``."""
        return True, 'disponible'


class ClamAVScanner(AntivirusScanner):
    """ClamAV over its TCP socket."""

    name = 'clamav'

    def __init__(self, host, port, timeout=120):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _client(self):
        import clamd

        return clamd.ClamdNetworkSocket(
            host=self.host, port=self.port, timeout=self.timeout
        )

    def scan(self, stream):
        try:
            if hasattr(stream, 'seek'):
                stream.seek(0)
            response = self._client().instream(stream)
        except Exception as exc:
            # A scanner that cannot be reached is a retryable condition, not a
            # reason to let an unscanned file through.
            raise TransientError(
                f'No se pudo contactar con el antivirus: {exc}'
            ) from exc
        finally:
            if hasattr(stream, 'seek'):
                stream.seek(0)

        status, signature = response.get('stream', ('ERROR', None))
        version = self._version()

        if status == 'OK':
            return ScanResult(True, motor=version)
        if status == 'FOUND':
            return ScanResult(False, firma=signature, motor=version)

        raise AntivirusError(f'El antivirus devolvió un estado inesperado: {status}')

    def _version(self):
        try:
            return str(self._client().version())[:120]
        except Exception:
            return 'clamav'

    def health_check(self):
        try:
            self._client().ping()
            return True, self._version()
        except Exception as exc:
            return False, str(exc)[:200]


class NoopScanner(AntivirusScanner):
    """Passes everything. For the test suite and for deployments that scan
    upstream at the gateway.

    Never the default: making "no scanning" the fallback would silently
    disable a control section 3.2 requires.
    """

    name = 'noop'

    def scan(self, stream):
        return ScanResult(True, motor='noop', detalle='Análisis omitido por configuración.')

    def health_check(self):
        return True, 'omitido por configuración'


SCANNERS = {'clamav': ClamAVScanner, 'noop': NoopScanner}


def get_scanner():
    """Build the configured scanner."""
    name = current_app.config['ANTIVIRUS_BACKEND']
    if name == 'clamav':
        return ClamAVScanner(
            current_app.config['CLAMAV_HOST'],
            current_app.config['CLAMAV_PORT'],
            current_app.config['CLAMAV_TIMEOUT'],
        )
    scanner_cls = SCANNERS.get(name)
    if scanner_cls is None:
        raise AntivirusError(f'Motor antivirus desconocido: {name}')
    return scanner_cls()


def scan(stream):
    """Scan a file-like object with the configured scanner."""
    return get_scanner().scan(stream)


def health_check():
    """Report whether the configured scanner is reachable."""
    try:
        return get_scanner().health_check()
    except Exception as exc:
        return False, str(exc)[:200]
