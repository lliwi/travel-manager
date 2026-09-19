"""Content hashing for uploaded documents.

Specification section 5.2 requires the original to remain available *with its
hash*, so the hash is computed while streaming the upload and verified again
after the file lands in object storage.
"""
import hashlib

#: Read size for streaming hashes. 1 MiB balances syscalls against memory.
CHUNK_SIZE = 1024 * 1024


def sha256_stream(stream, chunk_size=CHUNK_SIZE, reset=True):
    """Hash a file-like object without loading it into memory.

    Returns:
        ``(hex_digest, total_bytes)``.
    """
    digest = hashlib.sha256()
    total = 0
    if reset and hasattr(stream, 'seek'):
        stream.seek(0)
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    if reset and hasattr(stream, 'seek'):
        stream.seek(0)
    return digest.hexdigest(), total


def sha256_bytes(data):
    """Hash a bytes object."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def sha256_text(*parts):
    """Hash an ordered sequence of values, used for deterministic dedup keys."""
    joined = '|'.join('' if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()
