"""Put a backup's documents back into the object store, for restore.sh.

The mirror image of ``volcar_documentos``: same client, same buckets, and the
keys are restored exactly as they were, because the database rows point at
them by name.
"""
import sys
import tarfile

from app import create_app
from app.services import storage_service


def main(origen):
    app = create_app()
    with app.app_context():
        backend = storage_service.get_backend()
        bucket, _cuarentena = storage_service.buckets()
        cliente = backend.client

        total = 0
        with tarfile.open(origen, mode='r') as tar:
            for info in tar:
                if not info.isfile():
                    continue
                cuerpo = tar.extractfile(info)
                if cuerpo is None:
                    continue
                cliente.put_object(
                    Bucket=bucket, Key=info.name, Body=cuerpo.read(),
                )
                total += 1

    print(f'objetos={total}')


if __name__ == '__main__':
    main(sys.argv[1])
