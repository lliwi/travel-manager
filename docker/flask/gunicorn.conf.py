"""Gunicorn configuration, present for one reason: the metrics.

``prometheus_client`` keeps its counters in memory. With four workers that
means four sets of counters, and a scrape answered by any one of them reports
roughly a quarter of the traffic -- a wrong number that looks like a plausible
one, which is the worst kind. The library's multiprocess mode fixes it by
having every worker write into a shared directory and summing across the files
at scrape time, but it needs two things this file provides:

- the directory emptied before the workers start, or counters from processes
  that died in a previous run are added to today's totals for ever;
- ``mark_process_dead`` when a worker exits, or its files are never reclaimed
  and the directory grows without bound. Gunicorn recycles workers, so this is
  not a rare event.

Everything else about how the server runs stays on the command line in
``docker-compose.yml``, where it is visible next to the service it belongs to.
"""

import os
import shutil


def on_starting(server):
    """Clear the shared directory before any worker writes to it."""
    directorio = os.environ.get('PROMETHEUS_MULTIPROC_DIR')
    if not directorio:
        server.log.warning(
            'PROMETHEUS_MULTIPROC_DIR no está definida: /metrics informará de '
            'las peticiones de un solo worker.'
        )
        return

    shutil.rmtree(directorio, ignore_errors=True)
    os.makedirs(directorio, exist_ok=True)
    server.log.info('Métricas multiproceso en %s', directorio)


def child_exit(server, worker):
    """Reclaim a dead worker's metric files, keeping its counts in the totals."""
    directorio = os.environ.get('PROMETHEUS_MULTIPROC_DIR')
    if not directorio:
        return

    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)
