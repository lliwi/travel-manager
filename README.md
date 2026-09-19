# Travel Manager — Gestor de viajes asistido por IA

Aplicación web corporativa para centralizar la gestión de viajes de empresa.
Los gestores crean viajes, adjuntan documentación (vuelos, trenes, hoteles,
alquiler de vehículo), la asignan a personas viajeras y consultan la información
con ayuda de IA. El sistema extrae y normaliza los datos de los documentos,
detecta incidencias logísticas y genera recomendaciones de seguridad
contextualizadas por destino y fechas.

Implementa la **fase 1** del
[requerimiento técnico](requerimiento_tecnico_gestor_viajes_ia.md).

---

## Características principales

- **Tres perfiles de acceso**: administrador, gestor y usuario. Un usuario
  consulta exclusivamente los viajes que tiene asignados.
- **Gestión completa del viaje**: destinos, fechas con zona horaria, personas
  viajeras, segmentos de transporte, alojamientos, vehículos y otros servicios.
- **Tratamiento documental seguro**: validación del tipo real del archivo,
  análisis antivirus, almacenamiento con verificación de hash, OCR cuando hace
  falta, clasificación y extracción de datos estructurados.
- **Revisión humana obligatoria**: ningún dato extraído entra en el itinerario
  sin que un gestor lo apruebe, y queda registrado quién lo confirmó.
- **Procedencia por campo**: cada valor almacenado sabe si vino del documento,
  de la IA o de una persona, con su confianza y la página que lo respalda.
- **Motor de alertas**: conexiones ajustadas, solapamientos, llegadas tardías,
  noches sin alojamiento, datos incompletos y cambios de huso horario, con
  umbrales configurables desde administración.
- **Asistente de IA con control de acceso**: responde solo con los datos que
  quien pregunta tiene derecho a ver.
- **Configuración de IA desde la aplicación**: proveedor, endpoint, modelo,
  claves y asignación por tarea se administran en el panel, no en el
  despliegue. Las claves se guardan cifradas.
- **Recomendaciones de seguridad** con fuente, fecha, confianza y validación
  por un gestor antes de ser visibles.
- **Auditoría inmutable** encadenada por hash, verificable en cualquier momento.

## Tecnologías

| Capa | Tecnología |
| --- | --- |
| Backend | Flask 3 (application factory + blueprints), Python 3.11 |
| Base de datos | PostgreSQL 16, SQLAlchemy 2, Alembic |
| Cola de trabajos | Celery 5 + Redis 7 |
| Almacenamiento | MinIO (compatible con S3) |
| Antivirus | ClamAV |
| OCR | Tesseract (español e inglés) |
| IA | Ollama (local, por defecto), vLLM, OpenAI y DeepSeek |
| Frontend | Jinja2 + Bootstrap 5, JavaScript sin dependencias |
| API | REST versionada en `/api/v1` |
| Servidor | Gunicorn tras Nginx |
| Contenedores | Docker Compose |

## Requisitos previos

- Docker 24 o superior y Docker Compose v2
- 6 GB de RAM libres (ClamAV necesita algo más de 1 GB por sí solo)
- 10 GB de disco
- [Ollama](https://ollama.com) instalado **en el host**, con un modelo
  descargado:

  ```bash
  ollama pull llama3.1:8b     # o qwen3:8b, mistral-nemo, el que prefiera
  ```

  Ollama se ejecuta fuera de Docker a propósito: así aprovecha la GPU del
  equipo sin configuración adicional y el modelo no se descarga dentro de un
  volumen.

  La instalación deja configurado un proveedor apuntando a él. Qué modelo usar,
  y si añadir otros proveedores, se decide después en
  **Administración → Proveedores de IA**.

## Instalación

### 1. Clonar e instalar

```bash
git clone <url-del-repositorio> travel-manager
cd travel-manager
./setup.sh
```

`setup.sh` comprueba los requisitos, genera todos los secretos, crea un
certificado autofirmado para desarrollo, construye las imágenes, arranca los
servicios, aplica las migraciones, carga los catálogos y le pide los datos del
primer administrador. Es seguro volver a ejecutarlo: los secretos existentes se
conservan.

### 2. Acceder

- Aplicación: <http://localhost>
- Consola de MinIO: <http://localhost:9001>

### 3. Arranque diario

`start.sh` levanta el sistema y aplica por el camino lo que haga falta:
reconstruye las imágenes si el código cambió, ejecuta las migraciones
pendientes y siembra los ajustes o catálogos nuevos. Todo es idempotente.

```bash
./start.sh --prod           # producción: Gunicorn tras Nginx, ClamAV activo
./start.sh --prod --stop    # detener producción

./start.sh --dev            # desarrollo: recarga automática, sin Nginx
./start.sh --dev --stop     # detener desarrollo
```

Opciones adicionales:

| Opción | Efecto |
| --- | --- |
| `--logs` | Seguir el registro tras arrancar |
| `--status` | Mostrar el estado y salir |
| `--build` | Forzar la reconstrucción de las imágenes |
| `--no-build` | No reconstruir aunque haya cambios |
| `--no-migrate` | No aplicar migraciones ni siembra |

En desarrollo el código se monta desde el host, PostgreSQL, Redis y MinIO
publican su puerto en loopback para poder usar herramientas locales, y el
antivirus queda desactivado (`ANTIVIRUS_BACKEND=noop`): ClamAV tarda minutos en
descargar sus firmas y en desarrollo rara vez aporta.

`--stop` detiene los servicios conservando los datos. Para borrarlos también
hace falta pedirlo explícitamente con `docker compose ... down -v`.

### Instalación manual

Si prefiere no usar `setup.sh`:

```bash
cp .env.example docker/.env
# Edite docker/.env y sustituya todos los valores "cambie-esto-..."
python3 -c "import secrets; print(secrets.token_hex(32))"                      # SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SECRETS_ENCRYPTION_KEY

docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build
docker compose -f docker/docker-compose.yml exec web flask db upgrade
docker compose -f docker/docker-compose.yml exec web flask seed
docker compose -f docker/docker-compose.yml exec web flask create-admin
```

## Estructura del proyecto

```
travel-manager/
├── app/
│   ├── __init__.py              # create_app y registro de componentes
│   ├── config.py                # configuración por entorno
│   ├── extensions.py            # singletons de extensiones Flask
│   ├── cli.py                   # comandos flask
│   ├── models/                  # modelo de datos (34 tablas)
│   │   ├── enums.py             #   todos los enumerados, con su etiqueta
│   │   ├── base.py              #   mixins, GUID, JSONB, EnumType
│   │   └── ...
│   ├── blueprints/              # superficie web (Jinja) y API
│   │   ├── auth/ trips/ documents/ alerts/ advisories/ admin/
│   │   └── api/v1/              #   API REST versionada
│   ├── services/                # lógica de negocio
│   │   ├── authorization_service.py   # única fuente de verdad de permisos
│   │   ├── identity/            #   proveedores de identidad (local, LDAP)
│   │   ├── alerts/              #   motor de reglas y reconciliador
│   │   ├── ai/                  #   proveedores, guardas y esquemas
│   │   └── ...
│   ├── tasks/                   # tareas Celery
│   ├── utils/                   # tiempo, cifrado, errores, decoradores
│   ├── templates/               # plantillas Jinja
│   └── static/                  # CSS y JavaScript
├── docker/                      # compose, Dockerfiles y configuración
├── migrations/                  # migraciones Alembic
├── tests/                       # suite de pruebas
├── docs/                        # documentación técnica
├── setup.sh                     # instalación inicial
└── start.sh                     # arranque diario
```

## Uso

### Comandos de administración

```bash
COMPOSE="docker compose -f docker/docker-compose.yml"

$COMPOSE exec web flask create-admin      # crear un administrador (o sobrescribirlo)
$COMPOSE exec web flask create-user       # crear una cuenta con rol
$COMPOSE exec web flask seed              # recargar catálogos y ajustes
$COMPOSE exec web flask ai-health         # comprobar los proveedores de IA
$COMPOSE exec web flask verify-audit      # verificar la cadena de auditoría
$COMPOSE exec web flask db upgrade        # aplicar migraciones
```

`create-admin` y `create-user` piden los datos por pantalla. Para
automatizarlos, páselos como opciones y la contraseña en la variable
`TRAVEL_ADMIN_PASSWORD`, que no queda en el historial del intérprete:

```bash
export TRAVEL_ADMIN_PASSWORD='...'
$COMPOSE exec -T -e TRAVEL_ADMIN_PASSWORD web flask create-admin \
  --username admin --email admin@empresa.test --nombre Ana
```

Si la cuenta ya existe, ambos comandos ofrecen sobrescribirla: contraseña
nueva, roles reemplazados y sesiones cerradas. Interactivamente muestran qué
cambia y piden confirmación; sin terminal hace falta `--overwrite`. Queda
registrado en la auditoría como `user.overwritten`.

### Registro y diagnóstico

```bash
$COMPOSE logs -f web         # aplicación
$COMPOSE logs -f worker      # procesamiento documental
$COMPOSE ps                  # estado de los servicios
```

### Pruebas

```bash
pytest                       # toda la suite
pytest -m acceptance         # criterios de aceptación del requerimiento
pytest -m security           # autorización, aislamiento y controles de IA
```

## Seguridad

- **Autenticación** local con Argon2id, bloqueo temporal por intentos fallidos y
  respuesta idéntica ante cualquier fallo, para no revelar qué cuentas existen.
- **Autorización** resuelta en un único punto (`authorization_service.can`),
  compartido por la interfaz y la API. Un endpoint nuevo sin declarar su
  autorización hace fallar la suite de pruebas.
- **Aislamiento entre viajeros** por construcción: la consulta de itinerario ya
  viene filtrada, de modo que la IA no puede revelar lo que nunca recibió.
- **Documentos** validados por sus bytes reales, analizados por antivirus antes
  de cualquier otro proceso y servidos siempre como descarga.
- **Claves de IA** cifradas en reposo; por defecto, ni los documentos ni los
  datos personales salen a proveedores remotos.
- **Búsqueda web** restringida a una lista blanca de dominios, con validación de
  la dirección resuelta en cada redirección para impedir SSRF.
- **Auditoría** encadenada por hash: alterar o borrar una fila directamente en
  la base de datos es detectable con `flask verify-audit`.

## Documentación adicional

- [INSTALL.md](INSTALL.md) — instalación detallada y resolución de problemas
- [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md) — decisiones de diseño
- [docs/API.md](docs/API.md) — referencia de la API REST
- [docs/OPERACION.md](docs/OPERACION.md) — copias de seguridad, retención y
  supervisión

## Estado

Fase 1 del requerimiento, completa. Las fases 2 y 3 (integración AD/LDAP,
conectores de fuentes oficiales, notificaciones e integraciones con proveedores
de viaje) quedan preparadas en la arquitectura pero no implementadas.
