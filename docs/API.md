# API REST v1

Base: `/api/v1`

## Autenticación

La API comparte la sesión con la interfaz: un `POST` a `/auth/login` deja una
cookie de sesión. No hay JWT en la fase 1; el enganche para tokens de máquina
(un `request_loader` de Flask-Login) queda preparado para la fase 2.

Las peticiones que modifican estado requieren el token CSRF en la cabecera
`X-CSRFToken`. Se obtiene del login o de `/me`. Eximir la API de CSRF dejaría
abierto cualquier endpoint autenticado por cookie a peticiones entre sitios.

```bash
# Login
curl -c cookies.txt -X POST http://localhost/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "gestor", "password": "..."}'

# Petición autenticada
curl -b cookies.txt http://localhost/api/v1/trips

# Petición que modifica estado
curl -b cookies.txt -X POST http://localhost/api/v1/trips \
  -H 'Content-Type: application/json' \
  -H 'X-CSRFToken: <token>' \
  -d '{"titulo": "Reunión en Berlín"}'
```

## Formato de respuesta

Éxito:

```json
{
  "data": { },
  "request_id": "8ec6c0a3c6ce4b939f74fc88cb7fea06"
}
```

Listados paginados:

```json
{
  "data": [ ],
  "meta": {
    "pagina": 1, "por_pagina": 20, "total": 42, "paginas": 3,
    "tiene_siguiente": true, "tiene_anterior": false
  },
  "request_id": "..."
}
```

Error:

```json
{
  "error": {
    "codigo": "forbidden",
    "mensaje": "No tiene permisos para realizar esta operación.",
    "detalles": { }
  },
  "request_id": "..."
}
```

`request_id` corresponde a la cabecera `X-Request-ID` y aparece en los registros
del servidor, de modo que un error notificado por un usuario puede localizarse.

### Códigos de error

| Código | HTTP | Significado |
| --- | --- | --- |
| `unauthenticated` | 401 | No hay sesión |
| `invalid_credentials` | 401 | Usuario o contraseña incorrectos |
| `account_locked` | 423 | Bloqueo temporal por intentos fallidos |
| `forbidden` | 403 | Autenticado, sin permiso para esta operación |
| `not_found` | 404 | No existe, o no tiene relación con el recurso |
| `conflict` | 409 | Choca con el estado actual |
| `invalid_transition` | 409 | Transición de estado no permitida |
| `validation_error` | 422 | Datos no válidos |
| `payload_too_large` | 413 | Archivo demasiado grande |
| `rate_limited` | 429 | Demasiadas peticiones |
| `ai_policy_blocked` | 403 | La política de salida de datos lo impide |
| `ssrf_blocked` | 400 | Destino web no permitido |

Un recurso con el que el solicitante no tiene ninguna relación responde `404`,
no `403`: así la API no puede usarse para averiguar qué viajes existen.

---

## Endpoints

### Sesión

| Método | Ruta | Descripción |
| --- | --- | --- |
| `POST` | `/auth/login` | Iniciar sesión |
| `POST` | `/auth/logout` | Cerrar sesión |
| `GET` | `/me` | Usuario actual, roles y permisos efectivos |

### Viajes

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/trips` | Listado, filtrado por lo que el solicitante puede ver |
| `POST` | `/trips` | Crear |
| `GET` | `/trips/{id}` | Detalle |
| `PATCH` | `/trips/{id}` | Modificar |
| `DELETE` | `/trips/{id}` | Eliminar (borrado lógico) |

Parámetros de `GET /trips`: `page`, `per_page`, `estado` (repetible), `buscar`,
`gestor_id`, `orden`.

### Personas viajeras y destinos

| Método | Ruta |
| --- | --- |
| `GET\|POST` | `/trips/{id}/travelers` |
| `DELETE` | `/trips/{id}/travelers/{user_id}` |
| `GET\|POST` | `/trips/{id}/destinations` |
| `DELETE` | `/trips/{id}/destinations/{destination_id}` |

### Itinerario

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/trips/{id}/itinerary` | Línea temporal consolidada |
| `POST` | `/trips/{id}/itinerary/{tipo}` | Crear elemento |
| `PATCH` | `/trips/{id}/itinerary/{tipo}/{item_id}` | Modificar |
| `DELETE` | `/trips/{id}/itinerary/{tipo}/{item_id}` | Eliminar |

`{tipo}` es `segmento`, `alojamiento`, `vehiculo` o `servicio`.

**Las fechas se envían como hora local más zona horaria**, nunca como un
instante UTC:

```json
{
  "numero": "IB3210",
  "origen_codigo": "MAD",
  "destino_codigo": "BER",
  "salida":  {"local": "2026-06-01T10:00", "tz": "Europe/Madrid"},
  "llegada": {"local": "2026-06-01T12:30", "tz": "Europe/Berlin"}
}
```

Enviar un campo `*_utc` se rechaza con `validation_error`: esa columna es
derivada, y permitir escribirla directamente dejaría que las tres columnas del
triple se desincronizasen.

### Documentos

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/trips/{id}/documents` | Listado |
| `POST` | `/trips/{id}/documents` | Subir (`multipart/form-data`, campo `archivo`) |
| `GET` | `/documents/{id}` | Detalle, estado del proceso y extracción |
| `GET` | `/documents/{id}/download` | Descargar el original |
| `POST` | `/documents/{id}/review` | Aprobar, corregir o descartar |
| `POST` | `/documents/{id}/reprocess` | Reintentar desde donde falló |
| `DELETE` | `/documents/{id}` | Eliminar |

La subida responde `202`: el procesamiento es asíncrono. Consulte
`/documents/{id}` para seguir el progreso:

```json
{
  "estado_proceso": "pendiente_revision",
  "progreso": {"paso": 9, "total": 11},
  "clasificacion": "vuelo",
  "clasificacion_confianza": 0.99
}
```

Revisión:

```json
{
  "accion": "aprobar",
  "correcciones": {"numero_vuelo": "IB3211"},
  "comentario": "Corregido el número: el documento estaba borroso."
}
```

### Alertas

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/trips/{id}/alerts` | Listado |
| `PATCH` | `/trips/{id}/alerts` | Recalcular |
| `GET` | `/alerts/{id}` | Detalle con evidencia e histórico |
| `PATCH` | `/alerts/{id}` | Cambiar estado con comentario |
| `POST` | `/alerts/{id}/explain` | Explicación generada por IA |

### IA e investigación

| Método | Ruta | Descripción |
| --- | --- | --- |
| `POST` | `/trips/{id}/ai/query` | Preguntar sobre el viaje |
| `POST` | `/trips/{id}/ai/summary` | Resumen ejecutivo e itinerario legible |
| `POST` | `/trips/{id}/research` | Consultar fuentes públicas autorizadas |

La respuesta de una consulta incluye la procedencia de los datos:

```json
{
  "respuesta": "Su vuelo IB3210 sale de Madrid el 1 de junio a las 10:00 (CEST).",
  "referencias": [{"id": "...", "tipo": "segmento"}],
  "confianza": 0.9,
  "datos_insuficientes": false,
  "fecha_datos": "2026-06-01T09:15:00+00:00",
  "proveedor": "ollama",
  "modelo": "llama3.1:8b"
}
```

Las referencias se validan contra el conjunto autorizado antes de devolverse:
una que el modelo no recibió se descarta.

### Recomendaciones de seguridad

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/trips/{id}/security-advisories` | Listado |
| `POST` | `/trips/{id}/security-advisories` | Generar |
| `PATCH` | `/security-advisories/{id}` | Validar o rechazar |

Un usuario con perfil de viajero solo recibe las validadas.

### Administración

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET\|POST` | `/users` | Listar y crear cuentas |
| `GET\|PATCH\|DELETE` | `/users/{id}` | Consultar, modificar, desactivar |
| `POST` | `/users/{id}/unlock` | Desbloquear tras intentos fallidos |
| `GET` | `/roles` | Roles disponibles |
| `GET` | `/audit-events` | Auditoría, con filtros y paginación |
| `POST` | `/audit-events/verify` | Verificar la cadena de integridad |

## Límites de tasa

| Endpoint | Límite |
| --- | --- |
| `/auth/login` | 10 por minuto, 40 por hora |
| Resto | 400 por día, 100 por hora |

Nginx aplica además un límite en el borde. Al superarse se devuelve `429`.
