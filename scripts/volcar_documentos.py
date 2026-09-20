"""Write every stored document to one tar, for scripts/backup.sh.

Uses the application's own S3 client rather than a separate tool: nothing extra
to install, no credentials on a command line, and it behaves the same against
MinIO as against any S3.

Writes to a path rather than to stdout on purpose. Creating the application
logs a line to stdout, and a log line in front of a tar makes it not a tar --
which is the kind of corruption nobody notices until the day they restore.
"""
import io
import sys
import tarfile

from app import create_app
from app.services import storage_service


def main(destino):
    app = create_app()
    with app.app_context():
        backend = storage_service.get_backend()
        bucket, _cuarentena = storage_service.buckets()
        cliente = backend.client

        total = 0
        with tarfile.open(destino, mode='w') as tar:
            paginador = cliente.get_paginator('list_objects_v2')
            for pagina in paginador.paginate(Bucket=bucket):
                for objeto in pagina.get('Contents') or []:
                    cuerpo = cliente.get_object(
                        Bucket=bucket, Key=objeto['Key'],
                    )['Body'].read()
                    info = tarfile.TarInfo(objeto['Key'])
                    info.size = len(cuerpo)
                    info.mtime = int(objeto['LastModified'].timestamp())
                    tar.addfile(info, io.BytesIO(cuerpo))
                    total += 1

    print(f'objetos={total}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/tmp/documentos.tar')
