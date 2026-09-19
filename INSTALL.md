# Guía de instalación

## Instalación rápida

```bash
git clone <url-del-repositorio> travel-manager
cd travel-manager
./setup.sh          # una sola vez: genera secretos, construye y crea el admin
./start.sh --prod   # arranques posteriores
```

El instalador se encarga de todo. Continúe leyendo solo si algo falla o si
necesita una instalación manual.

### Arranque y parada

```bash
./start.sh --prod           ./start.sh --prod --stop
./start.sh --dev            ./start.sh --dev --stop
```

`start.sh` aplica las actualizaciones necesarias al arrancar: reconstruye las
imágenes si el código cambió, ejecuta las migraciones pendientes y siembra los
ajustes y catálogos nuevos sin tocar lo que ya esté configurado. Consulte
`./start.sh --help` para el resto de opciones.

---

## Requisitos

| Recurso | Mínimo | Recomendado |
| --- | --- | --- |
| RAM | 6 GB | 8 GB |
| Disco | 10 GB | 20 GB |
| CPU | 2 núcleos | 4 núcleos |
| Docker | 24 | última estable |

ClamAV consume por sí solo algo más de 1 GB de RAM porque mantiene su base de
firmas en memoria. Si no dispone de ese margen, ponga `ANTIVIRUS_BACKEND=noop`
en `docker/.env` y quite el servicio `clamav` del compose — pero solo si otro
control analiza los archivos antes de que lleguen a la aplicación.

### Ollama en el host

La IA se ejecuta fuera de Docker, en su propia máquina:

```bash
# Linux
curl -fsSL https://ollama.com/install.sh | sh

# macOS
brew install ollama

ollama pull llama3.1:8b
ollama serve          # si no arranca solo
```

Compruebe que responde:

```bash
curl http://localhost:11434/api/tags
```

Modelos alternativos, según la memoria disponible:

| Modelo | RAM | Notas |
| --- | --- | --- |
| `llama3.2:3b` | ~4 GB | Rápido; extracción menos fiable |
| `llama3.1:8b` | ~8 GB | Equilibrio recomendado |
| `qwen2.5:14b` | ~12 GB | Mejor extracción estructurada |
| `mistral-nemo` | ~8 GB | Buen rendimiento en español |

El modelo se elige **en la aplicación**, en Administración → Proveedores de
IA, no en un archivo de configuración: así cambiarlo no exige reiniciar nada.
El botón «Probar conexión» comprueba que Ollama responde y que el modelo está
descargado.

---

## Instalación manual

### 1. Configuración

```bash
cp .env.example docker/.env
chmod 600 docker/.env
```

Genere cada secreto y sustitúyalo en `docker/.env`:

```bash
# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# SECRETS_ENCRYPTION_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# POSTGRES_PASSWORD, REDIS_PASSWORD, S3_SECRET_KEY
python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(28)))"
```

### 2. Certificado TLS

Para desarrollo:

```bash
mkdir -p docker/nginx/certs
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout docker/nginx/certs/server.key \
  -out docker/nginx/certs/server.crt \
  -subj "/C=ES/O=Travel Manager/CN=localhost"
chmod 600 docker/nginx/certs/server.key
```

En producción, coloque ahí el certificado emitido por su autoridad de
certificación y active la redirección a HTTPS descomentando la línea `return
301 https://...` en `docker/nginx/app.conf`.

### 3. Arranque

```bash
COMPOSE="docker compose -f docker/docker-compose.yml --env-file docker/.env"

$COMPOSE build
$COMPOSE up -d
$COMPOSE exec web flask db upgrade
$COMPOSE exec web flask seed
$COMPOSE exec web flask create-admin
```

`create-admin` pide los datos por pantalla, así que necesita una terminal.
Para automatizarlo (o si usa `exec -T`, que no asigna terminal), pase los datos
como opciones y la contraseña en una variable de entorno:

```bash
export TRAVEL_ADMIN_PASSWORD='...'
$COMPOSE exec -T -e TRAVEL_ADMIN_PASSWORD web flask create-admin \
  --username admin --email admin@empresa.test --nombre Ana --apellidos Gestora
```

Use la variable y no `--password`: una contraseña escrita en la línea de
órdenes queda en el historial del intérprete y es visible con `ps`.

### 4. Comprobación

```bash
curl http://localhost/healthz          # proceso vivo
curl http://localhost/readyz           # dependencias accesibles
$COMPOSE exec web flask ai-health      # proveedores de IA
$COMPOSE exec web flask verify-audit   # integridad de la auditoría
```

---

## Resolución de problemas

### La aplicación no arranca

```bash
docker compose -f docker/docker-compose.yml logs web
```

**`SECRET_KEY must be set to a strong, unique value in production`**
La comprobación de arranque ha encontrado el valor de ejemplo. Genere uno real.

**`SECRETS_ENCRYPTION_KEY must be set in production`**
Sin ella no se pueden cifrar las claves de IA ni los datos personales de los
viajeros, así que la aplicación se niega a arrancar en lugar de guardarlos en
claro.

### ClamAV no responde

En el primer arranque descarga su base de firmas, lo que puede llevar entre
cinco y quince minutos. Hasta que termina, las subidas de documentos se quedan
en estado «validado»: el pipeline reintenta el análisis, no da el archivo por
limpio.

```bash
docker compose -f docker/docker-compose.yml logs clamav | tail -20
```

### El proveedor aparece como «no accesible»

Abra Administración → Proveedores de IA y pulse «Probar conexión»: el mensaje
dice exactamente qué falla, incluido el comando para descargar un modelo que
falte.

### `flask ai-health` no encuentra Ollama

Desde el contenedor, el host es `host.docker.internal`. En Linux esto funciona
gracias al `extra_hosts: host-gateway` del compose. Compruebe:

```bash
docker compose -f docker/docker-compose.yml exec web \
  curl -s http://host.docker.internal:11434/api/tags
```

Si no responde, verifique que Ollama escucha en todas las interfaces:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

### «El modelo no está disponible en Ollama»

```bash
ollama pull llama3.1:8b
ollama list
```

El nombre en `OLLAMA_DEFAULT_MODEL` debe coincidir con el de `ollama list`.

### Un documento se queda en «Error de proceso»

Abra el detalle del documento: muestra el código de error, el mensaje y el
histórico completo de transiciones, de modo que se ve en qué paso falló. El
botón «Reintentar» reanuda desde ese punto, no desde el principio.

```bash
docker compose -f docker/docker-compose.yml logs worker | tail -50
```

### Las alertas no se recalculan

Compruebe que el worker está vivo:

```bash
docker compose -f docker/docker-compose.yml ps worker
docker compose -f docker/docker-compose.yml exec worker \
  celery -A app.tasks.celery_app.celery inspect ping
```

Si la cola no está disponible, el recálculo se ejecuta en línea: será más lento,
pero las alertas nunca quedan obsoletas por una caída del worker.

### `flask create-admin` termina con «Aborted!»

El comando pide los datos por pantalla y no encontró una terminal. Ocurre con
`docker compose exec -T`, desde un script o a través de una tubería. O quita
el `-T`, o pasa los datos como opciones:

```bash
export TRAVEL_ADMIN_PASSWORD='...'
docker compose -f docker/docker-compose.yml exec -T \
  -e TRAVEL_ADMIN_PASSWORD web flask create-admin \
  --username admin --email admin@empresa.test --nombre Ana
```

El propio comando explica ambas salidas si lo ejecuta sin terminal.

### Restablecer la contraseña de un administrador

`create-admin` sobre una cuenta que ya existe ofrece sobrescribirla:

```bash
docker compose -f docker/docker-compose.yml exec web \
  flask create-admin --username admin --email admin@empresa.test --nombre Ana
```

Muestra qué va a cambiar y pide confirmación. Sobrescribir supone:

- establecer la contraseña nueva,
- **reemplazar** los roles por el indicado (no se suman a los que tuviera),
- invalidar todas sus sesiones abiertas,
- levantar el bloqueo por intentos fallidos,
- y reactivar la cuenta si estaba eliminada.

Sin terminal hace falta `--overwrite` para confirmarlo, de modo que un script
de despliegue no pueda restablecer la contraseña de un administrador porque un
nombre de usuario coincidiera por casualidad:

```bash
export TRAVEL_ADMIN_PASSWORD='...'
docker compose -f docker/docker-compose.yml exec -T \
  -e TRAVEL_ADMIN_PASSWORD web flask create-admin --overwrite \
  --username admin --email admin@empresa.test --nombre Ana
```

La cuenta se localiza por el nombre de usuario **o** por el correo, así que
también sirve para renombrar. Si cada uno apunta a una cuenta distinta, el
comando se niega en lugar de elegir: corrija uno de los dos datos.

Cada sobrescritura queda en la auditoría como `user.overwritten`, con los roles
antes y después.

---

## Actualización

```bash
git pull
./start.sh --prod
```

`start.sh` detecta que el código ha cambiado, reconstruye, aplica las
migraciones pendientes y siembra lo nuevo. Los tres pasos son idempotentes:
`flask seed` crea lo que falta y nunca sobrescribe lo que un administrador haya
ajustado.

Si prefiere hacerlo a mano:

```bash
COMPOSE="docker compose -f docker/docker-compose.yml"
$COMPOSE build && $COMPOSE up -d
$COMPOSE exec web flask db upgrade
$COMPOSE exec web flask seed
```

---

## Desinstalación

```bash
COMPOSE="docker compose -f docker/docker-compose.yml"
$COMPOSE down                # detener, conservando los datos
$COMPOSE down -v             # detener y BORRAR todos los volúmenes
```

`down -v` elimina la base de datos, los documentos almacenados y la auditoría.
No es reversible.
