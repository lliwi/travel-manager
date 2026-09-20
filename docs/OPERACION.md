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

### Métricas

`/metrics` expone en formato Prometheus el estado de la API y del proceso
documental. Las sondas dicen si el sistema está vivo; esto dice cómo va.

| Métrica | Para qué |
| --- | --- |
| `travelmanager_http_requests_total` | Tráfico por regla y por código de respuesta. |
| `travelmanager_http_request_duration_seconds` | Histograma de latencia. El objetivo del requerimiento es un p95 por debajo de 500 ms. |
| `travelmanager_documents` | Documentos por estado del proceso. |
| `travelmanager_documents_stuck` | **La que hay que vigilar**: documentos parados en un estado intermedio más de 30 minutos. Si sube, el proceso documental se ha detenido. |
| `travelmanager_queue_depth` | Tareas aceptadas y no empezadas. Un valor que solo crece es un worker caído. |
| `travelmanager_ai_runs` | Ejecuciones de IA por estado. Un `error` que crece es un proveedor que ha dejado de responder. |
| `travelmanager_ai_duration_ms_avg` | Duración media por tarea. Sirve para notar que un modelo se ha vuelto cuatro veces más lento tras cambiarlo en el panel. |
| `travelmanager_alerts_open` | Alertas abiertas por severidad. |
| `travelmanager_trips` | Viajes por estado. |

Las cifras del negocio se consultan a la base de datos en el momento del
scrape, no se llevan en memoria. Por eso el worker de Celery se ve sin exponer
ningún puerto: lo que hace acaba en esas tablas.

**No es público.** Nginx lo niega fuera de la red privada y la aplicación lo
vuelve a comprobar por su cuenta. Si el recolector vive en otra máquina, defina
`METRICS_TOKEN` y haga que presente `Authorization: Bearer <token>`.

Ejemplo de recolección:

```yaml
scrape_configs:
  - job_name: travel-manager
    metrics_path: /metrics
    static_configs:
      - targets: ['travelmanager_web:5000']
```

Alertas que merecen la pena:

```yaml
- alert: ProcesoDocumentalDetenido
  expr: travelmanager_documents_stuck > 0
  for: 15m

- alert: ColaCreciendo
  expr: travelmanager_queue_depth > 50
  for: 10m
```

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
$COMPOSE exec web flask apply-retention # qué documentos han vencido su plazo
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
| `maintenance.verify_audit_chain` | 05:00 | Comprueba la cadena de auditoría y avisa si está rota |

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
$COMPOSE exec web flask apply-retention

# Purgar de verdad: pide confirmación antes de borrar
$COMPOSE exec web flask apply-retention --execute
```

La tarea nocturna de las 04:00 solo informa; nunca borra. La pasada destructiva
la ejecuta una persona con el mandato para hacerlo.

Lo que se borra es el contenido del documento. La fila de metadatos, su hash y
la traza de auditoría permanecen: se borra el original, no la prueba de que
existió.

## Búsqueda de vuelos y alojamiento

El asistente de planificación puede consultar opciones reales a través de
SerpApi. Se activa en **Ajustes → Búsqueda de vuelos y alojamiento**, con la
clave que se obtiene en serpapi.com. Sin ella, el asistente sigue funcionando:
orienta sobre cómo viajar en lugar de decir qué hay.

**Cada consulta se cobra.** Una planificación gasta hasta dos búsquedas —una de
vuelos y otra de alojamiento—, y cada vez que alguien pulsa «Dónde comprarlo»
en un vuelo se gasta otra. Ese botón va aparte y bajo demanda precisamente por
eso: resolverlo para los ocho resultados de cada planificación gastaría el
presupuesto de un día en seis planificaciones, y nadie mira ocho. El ajuste «Búsquedas máximas al día» es el freno:
alcanzado el límite el asistente sigue respondiendo sin opciones concretas, en
lugar de generar una factura que nadie esperaba. El consumo se cuenta desde la
propia auditoría, así que se puede revisar:

```bash
$COMPOSE exec web flask shell -c "
from app.models.audit import AuditEvent
print(AuditEvent.query.filter_by(accion='busqueda_viajes.consulta').count())"
```

Tres límites del conector que conviene saber antes de prometer nada:

- **No reserva.** Devuelve opciones y un enlace; la compra se hace fuera.
- **Los precios son los de Google** en el momento de la consulta: orientativos,
  y pueden haber cambiado cuando alguien vaya a comprar.
- **No cubre trenes.** Renfe, SNCF y demás quedan fuera. Para un trayecto
  ferroviario el asistente orienta como antes y lo dice; una lista vacía no
  significa que no haya tren.

## Salida a internet (proxy)

Si el servidor no alcanza internet directamente, se configura en **Ajustes →
Salida a internet**. Afecta a todo lo que sale: búsqueda de vuelos y
alojamiento, fuentes oficiales de recomendaciones y proveedores de IA externos.

| Ajuste | Qué poner |
| --- | --- |
| Proxy de salida | `http://proxy.corp.local:3128`, sin usuario ni contraseña. |
| Usuario / Contraseña | Solo si el proxy autentica. La contraseña se guarda cifrada. |
| Destinos que no pasan | Además de los internos, que nunca pasan. |

**Lo interno nunca sale por el proxy**, y esto no es configurable a la baja:
`localhost`, `127.0.0.1`, `[::1]`, `host.docker.internal` y los demás
contenedores van siempre directos.

**La red local tampoco**, mientras «La red local no pasa por el proxy» esté
activado —que es lo que viene de fábrica—. Con eso, cualquier dirección privada
(10.x, 172.16–31.x, 192.168.x y las de bucle) se alcanza directamente sin que
haya que escribir ningún rango. Desactívelo solo si en su organización el
proxy es también la salida de su propia red.

El caso que importa es la **inferencia local**: mandar un modelo propio por un
proxy corporativo o falla o —peor— funciona despacio mientras el proxy registra
cada prompt, y un prompt de extracción es el documento entero.

El campo **NO_PROXY** admite las tres formas, y se pueden mezclar:

| Lo que escribe | Qué cubre |
| --- | --- |
| `ia.corp.local` | Ese nombre exacto. |
| `corp.local` | Todo lo que acabe así: `ia.corp.local`, `a.b.corp.local`. |
| `10.8.0.0/16` | Todas las direcciones del rango. |

Los nombres **no se resuelven** para decidir la ruta. Preguntar al DNS antes de
cada petición añadiría una consulta a cada llamada y haría que el camino
dependiera de lo que el resolutor contestara en ese momento: una máquina que se
conoce por su nombre se escribe por su nombre.

La cabecera del bloque en Ajustes dice **por dónde está saliendo el tráfico
ahora mismo**, que no siempre es lo que está escrito: el ajuste manda sobre las
variables del entorno.

### Sin tocar Ajustes

También se respetan `HTTPS_PROXY` y `NO_PROXY` del entorno, que el compose ya
pasa a los contenedores. El orden es: lo de Ajustes gana; si está en blanco, se
usa el entorno.

### Certificados

Un proxy que inspecciona TLS reemite los certificados con su propia autoridad.
Apunte `SSL_CERT_FILE` al paquete de CA de su organización y monte el fichero
en el contenedor; no hay nada más que configurar.

```yaml
# docker/docker-compose.yml, servicio web
environment:
  SSL_CERT_FILE: /etc/ssl/corp/ca-bundle.crt
volumes:
  - /ruta/al/ca-bundle.crt:/etc/ssl/corp/ca-bundle.crt:ro
```

**El directorio LDAP no pasa por aquí**: no es HTTP, es una conexión TCP
directa al controlador de dominio, que normalmente es interno.

## Segundo factor (MFA)

Un código de seis dígitos de una aplicación de autenticación, además de la
contraseña. Cualquiera puede activarlo desde **Mi perfil**; en **Ajustes →
Seguridad de las cuentas** se decide a quién se le exige:

| `MFA_OBLIGATORIO` | A quién |
| --- | --- |
| `ninguno` | A nadie. Sigue siendo opcional (valor por defecto). |
| `administradores` | Solo a los administradores. |
| `gestion` | Gestores y administradores. |
| `todos` | A todo el mundo. |

Quien deba tenerlo y no lo tenga es llevado a configurarlo al entrar, y también
si ya tenía la sesión abierta cuando se cambió la política. Cerrar sesión sigue
siendo posible: nadie se queda atrapado en una sesión que no puede terminar.

Vale igual para las cuentas del directorio. El directorio comprueba la
contraseña; el segundo factor es nuestro y va encima.

### Cuando alguien pierde el teléfono

Al activarlo se entregan **diez códigos de recuperación**, de un solo uso, que
se muestran una única vez porque se guardan con hash. Sirven en el mismo campo
que los seis dígitos.

Si se han perdido también:

```bash
# Desde Usuarios, un administrador puede retirar el segundo factor de una
# cuenta. Y si quien está fuera es el único administrador:
$COMPOSE exec web flask mfa-reset <usuario|correo>
```

Las dos vías cierran las sesiones abiertas de esa cuenta y quedan auditadas con
quién lo hizo. Asegúrese de con quién habla antes de hacerlo: después, esa
cuenta vuelve a tener un solo factor.

## Directorio corporativo (AD / LDAP)

Se activa en **Ajustes → Directorio corporativo**. Hacen falta cuatro cosas: el
servidor, un usuario de consulta con su contraseña, y la ruta de la OU o del
grupo donde están las personas. El botón **Probar conexión** dice cuántas ve.

**No sustituye a las cuentas locales.** Las dos funcionan a la vez y cada cuenta
sabe de dónde viene; en la lista de usuarios hay una columna «Origen». Esto es
deliberado: un administrador que se quede sin directorio sigue pudiendo entrar,
que es la diferencia entre una tarde mala y quedarse fuera de la aplicación que
gestiona los viajes.

**Los roles son nuestros.** El directorio dice quién es alguien, no qué puede
hacer. La primera vez que una persona entra se le crea la cuenta con el rol
básico de usuario, y cualquier cosa por encima de eso se concede aquí, a mano,
en Usuarios. Mientras no se configuren correspondencias entre grupos y roles,
un rol concedido a mano no se lo quita un inicio de sesión posterior.

| Ajuste | Qué poner |
| --- | --- |
| Servidor / Puerto | El controlador de dominio. 389, o 636 con LDAPS. |
| LDAPS / STARTTLS | Uno de los dos en producción. Sin ninguno, la contraseña viaja legible. |
| Usuario de consulta | `CN=svc,OU=Servicios,DC=corp,DC=local` o `svc@corp.local`. Le basta con leer. |
| Ruta de la OU o del grupo | Dónde están las personas. Se distingue solo cuál de las dos es. |
| Atributo del nombre de usuario | `sAMAccountName` en AD; `uid` en OpenLDAP. |

Indicar una **OU** deja entrar a quien esté dentro. Indicar un **grupo** deja
entrar solo a sus miembros, que es lo habitual cuando la OU contiene a toda la
empresa y solo algunas personas usan el gestor de viajes.

**Probar conexión** no dice solo que el servidor responda: dice con qué
atributo se identifican las personas y con cuáles se rellena cada campo, y
avisa de lo que viene vacío. Eso es lo que distingue una configuración correcta
de una que conecta y luego falla en cada inicio de sesión:

> Conexión correcta. Se ven 4 personas en la ruta. Se identifican por «uid»
> (por ejemplo, «alopez»). Se leen además: nombre (givenName), apellidos (sn),
> correo (mail), puesto (title). Sin valor en la primera persona encontrada:
> teléfono, departamento.

Un correo vacío ahí significa que esas personas no recibirán ninguna
notificación, y se ve antes de que nadie entre.

### Las cuentas del directorio son de solo lectura

Autentican, y nada más. Ni su contraseña ni sus datos personales se cambian
desde aquí: se leen del directorio y se refrescan en cada inicio de sesión, así
que un cambio hecho en la aplicación parecería funcionar y se desharía solo.
La pantalla de perfil los muestra sin formulario y no ofrece cambiar la
contraseña.

Lo que sí es nuestro se sigue administrando: **los roles y si la cuenta puede
usarse**. Es la única parte editable de una cuenta del directorio en Usuarios.

### Probarlo en desarrollo

El compose de desarrollo levanta un OpenLDAP con un árbol sembrado:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d openldap
```

| Ajuste | Valor |
| --- | --- |
| Servidor | `openldap` |
| Usuario de consulta | `cn=admin,dc=corp,dc=test` |
| Contraseña | `Directorio.2026` |
| Ruta | `ou=usuarios,dc=corp,dc=test` (4 personas) o `cn=viajes,ou=usuarios,dc=corp,dc=test` (2) |
| Atributo | `uid` |

Cuentas: `alopez`, `bmartin`, `cruiz`, `dperez`, todas con `Directorio.2026`.
`alopez` y `bmartin` están en el grupo; `dperez` no, para poder comprobar que
acotando por grupo se queda fuera.

**Es LDAP, no AD.** Aquí el nombre de usuario es `uid` y los grupos son
`groupOfNames`; en Active Directory son `sAMAccountName` y `group`. El conector
sirve para los dos porque esos nombres se configuran, pero una prueba que pase
aquí no garantiza por sí sola que el dominio real esté bien puesto: para eso
está el botón de probar conexión contra el dominio de verdad.

## Correo

El servidor de salida se configura en Administración → Ajustes → Correo: host,
puerto, usuario, contraseña, TLS y remitente. La contraseña se guarda cifrada y
no vuelve a mostrarse.

En desarrollo, `docker-compose.dev.yml` levanta **Mailpit**, que acepta
cualquier correo, no entrega ninguno y los enseña en <http://localhost:8025>.
Sirve para ver qué se habría enviado sin escribirle a nadie de verdad: apunte
el host a `mailpit` y el puerto a `1025`.

Mientras `CORREO_HABILITADO` esté desactivado no se envía nada, y ese es el
estado de fábrica: un despliegue al que nadie le ha dicho a dónde mandar el
correo no debe empezar a escribir a la primera dirección que encuentre.

## Copias de seguridad

Hay dos almacenes que respaldar, y hacerlo por separado no sirve de nada si no
se restauran juntos: la base de datos guarda las claves de los objetos y el
almacén guarda los objetos. Los scripts los tratan siempre como una sola cosa.

```bash
./scripts/backup.sh                    # deja ./backups/backup_AAAAMMDD_HHMMSS
./scripts/backup.sh --output /mnt/nas  # en otro sitio
./scripts/backup.sh --encrypt          # cifrado con gpg, para sacarla de la máquina
```

Cada copia lleva la base de datos, un tar con los documentos, un manifiesto y
las sumas de verificación. Si cualquiera de las dos mitades falla no se deja
nada detrás: media copia que parece entera es peor que ninguna, porque nadie la
vuelve a hacer.

### Restauración

```bash
./scripts/restore.sh --from backups/backup_20260601_030000
```

Comprueba las sumas antes de tocar nada, detiene la aplicación, restaura las
dos mitades y termina ejecutando `flask verify-audit`. Esa comprobación final
no es un adorno: si la cadena de auditoría no cuadra después de restaurar, la
copia estaba incompleta o alterada, y conviene saberlo antes de dejar entrar a
nadie.

Para ensayar sin tocar los documentos, `--only-database`.

**Pruebe la restauración periódicamente.** Una copia que nunca se ha restaurado
no es una copia de seguridad, es una suposición. Una prueba que sirve: cree un
viaje con un título reconocible, restaure una copia anterior y compruebe que ha
desaparecido; si sigue ahí, la restauración no hizo lo que parecía.

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
