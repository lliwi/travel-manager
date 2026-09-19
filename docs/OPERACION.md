# Operación

## Supervisión

### Sondas

| Ruta | Qué comprueba |
| --- | --- |
| `/healthz` | El proceso responde. No toca dependencias. |
| `/readyz` | PostgreSQL y Redis son accesibles. Devuelve `503` si no. |

Use `/healthz` para reinicios automáticos y `/readyz` para decidir si enviar
tráfico: un proceso vivo cuya base de datos no responde debe salir del balanceo,
no reiniciarse.

### Registro

Los registros salen en JSON por la salida estándar, con el identificador de
correlación de cada petición:

```bash
COMPOSE="docker compose -f docker/docker-compose.yml"
$COMPOSE logs -f web
$COMPOSE logs -f worker
$COMPOSE logs web | grep '"level": "ERROR"'
$COMPOSE logs web | grep '8ec6c0a3c6ce4b93'   # seguir una petición concreta
```

Las contraseñas, tokens y claves API se redactan antes de escribirse.

### Comprobaciones periódicas

```bash
$COMPOSE exec web flask verify-audit    # integridad de la auditoría
$COMPOSE exec web flask ai-health       # proveedores de IA
$COMPOSE ps                             # estado de los servicios
```

## Tareas programadas

| Tarea | Cuándo | Qué hace |
| --- | --- | --- |
| `alerts.recalculate_active_trips` | 03:00 | Recalcula las alertas de los viajes no cerrados |
| `maintenance.expire_advisories` | 03:30 | Marca como caducadas las recomendaciones vencidas |
| `maintenance.apply_retention` | 04:00 | Aplica la política de retención (en simulación por defecto) |
| `maintenance.purge_web_cache` | 04:30 | Limpia la caché de investigación |

El recálculo nocturno existe porque algunas alertas solo se vuelven ciertas con
el calendario: un pasaporte se acerca a su caducidad y un viaje pasa de próximo
a en curso sin que nadie edite nada.

## Retención

La política se configura en Administración → Ajustes:

| Ajuste | Por defecto |
| --- | --- |
| `RETENCION_DOCUMENTOS_DIAS` | 1825 (5 años) |
| `RETENCION_AUDITORIA_DIAS` | 2555 (7 años) |
| `RETENCION_EJECUCIONES_IA_DIAS` | 365 |

La purga de originales **se ejecuta en simulación por defecto**. Borrar un
original es irreversible y el plazo es una decisión de la organización, así que
la pasada destructiva se activa a propósito:

```bash
# Ver qué se purgaría
$COMPOSE exec worker python -c "
from app.tasks.maintenance_tasks import apply_retention
print(apply_retention.run(dry_run=True))"

# Purgar de verdad
$COMPOSE exec worker python -c "
from app.tasks.maintenance_tasks import apply_retention
print(apply_retention.run(dry_run=False))"
```

Lo que se borra es el contenido del documento. La fila de metadatos, su hash y
la traza de auditoría permanecen: se borra el original, no la prueba de que
existió.

## Copias de seguridad

Hay dos almacenes que respaldar, y hacerlo por separado no sirve de nada si no
se restauran juntos: la base de datos guarda las claves de los objetos y el
almacén guarda los objetos.

### PostgreSQL

```bash
COMPOSE="docker compose -f docker/docker-compose.yml"
FECHA=$(date +%Y%m%d_%H%M%S)

$COMPOSE exec -T postgres pg_dump -U travel -Fc travel_manager \
  > backup_db_${FECHA}.dump

# Cifrar antes de sacarla de la máquina
gpg --symmetric --cipher-algo AES256 backup_db_${FECHA}.dump
```

### Documentos (MinIO)

```bash
docker run --rm --network travel-manager_backend \
  -v "$(pwd):/backup" minio/mc:latest sh -c "
    mc alias set tm http://minio:9000 \$S3_ACCESS_KEY \$S3_SECRET_KEY &&
    mc mirror tm/travel-documents /backup/documentos_${FECHA}"
```

### Restauración

```bash
$COMPOSE stop web worker beat

gpg --decrypt backup_db_20260601_030000.dump.gpg > restore.dump
$COMPOSE exec -T postgres pg_restore -U travel -d travel_manager \
  --clean --if-exists < restore.dump

# ... restaurar también los objetos ...

$COMPOSE start web worker beat
$COMPOSE exec web flask verify-audit
```

La verificación final no es opcional: si la cadena de auditoría no cuadra tras
la restauración, la copia estaba incompleta o alterada.

**Pruebe la restauración periódicamente.** Una copia que nunca se ha restaurado
no es una copia de seguridad, es una suposición.

## Escalado

El procesamiento documental es lo que más cuesta. Para aumentarlo:

```yaml
# docker/docker-compose.yml
worker:
  deploy:
    replicas: 3
```

O suba la concurrencia de cada worker con `CELERY_CONCURRENCY`. Tenga en cuenta
que OCR e inferencia son intensivos en memoria: dos tareas simultáneas por
worker suele ser el punto de equilibrio.

El servicio `beat` debe permanecer con **una sola instancia**: varias
programarían la misma tarea varias veces.

## Rotación de claves

Para rotar `SECRETS_ENCRYPTION_KEY` sin parar el servicio:

1. Genere la nueva clave.
2. Mueva la actual a `SECRETS_ENCRYPTION_KEY_PREVIOUS` y ponga la nueva en
   `SECRETS_ENCRYPTION_KEY`.
3. Reinicie. A partir de ahí se cifra con la nueva y se sigue pudiendo descifrar
   con la anterior.
4. Vuelva a guardar cada clave API desde Administración → Proveedores de IA.
5. Vacíe `SECRETS_ENCRYPTION_KEY_PREVIOUS` y reinicie.

Si se salta el paso 4, las claves antiguas dejarán de poder leerse al vaciar la
clave anterior. La aplicación no se cae: informa de que no hay clave configurada
para ese proveedor.

## Incidentes frecuentes

### Documentos atascados en «pendiente de revisión»

No es un fallo: la fase 1 exige revisión humana. Consúltelos en el panel del
gestor.

### Un documento en «Error de proceso»

El detalle del documento muestra el código, el mensaje y el histórico de
transiciones. «Reintentar» reanuda desde el paso que falló.

### ClamAV no responde

El pipeline **reintenta**, no da los archivos por limpios. Las subidas se
acumulan en estado «validado» hasta que el analizador vuelve.

### La cadena de auditoría no verifica

Es serio: indica que se ha modificado o borrado una fila directamente en la base
de datos. `flask verify-audit` señala el evento concreto. Investigue quién tiene
acceso directo a PostgreSQL antes de hacer nada más.
