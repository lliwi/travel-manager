# Requerimiento técnico — Gestor de viajes asistido por IA

**Versión:** 1.0  
**Estado:** Propuesta para desarrollo  
**Stack objetivo:** Python/Flask + PostgreSQL

## 1. Objetivo

Construir una aplicación web corporativa para centralizar la gestión de viajes de empresa. Los gestores podrán crear viajes, adjuntar documentación (vuelos, trenes, hoteles, alquiler de vehículo y otros), asignarlos a personas usuarias y consultar información con ayuda de IA. La aplicación extraerá y normalizará los datos de los documentos, identificará posibles incidencias logísticas y añadirá recomendaciones de seguridad basadas en el destino y las fechas.

En la primera fase se usará autenticación con usuarios locales. La arquitectura deberá dejar preparada la integración posterior con Active Directory/LDAP, sin rediseñar el modelo de usuarios ni las reglas de autorización.

## 2. Alcance funcional de la fase 1

### 2.1 Gestión de usuarios y permisos

Se implementarán tres perfiles de acceso:

| Perfil | Capacidades |
| --- | --- |
| **Administrador** | Configuración global, gestión de usuarios y roles, proveedores de IA, catálogos, políticas de retención, auditoría y parámetros de integración AD/LDAP. |
| **Gestor** | Alta, edición y cancelación de viajes; carga y consulta de documentación; extracción/revisión de datos; asignación de viajes a usuarios; consultas a IA y gestión de alertas. |
| **Usuario** | Consulta exclusivamente de los viajes que tenga asignados, sus documentos procesados, itinerario, alertas y recomendaciones de seguridad. |

Reglas mínimas:

- Un viaje podrá tener una o varias personas viajeras asignadas.
- Un gestor podrá administrar todos los viajes, salvo que se configure en el futuro un ámbito organizativo.
- Los usuarios no podrán ver ni descargar información de viajes no asignados.
- Las operaciones sensibles (alta/baja de usuario, cambios de rol, acceso/descarga de documentos, modificaciones y consultas de IA) quedarán auditadas.

### 2.2 Gestión del viaje

El gestor podrá crear un viaje con los siguientes datos manuales, ampliables por extracción automática:

- Título o referencia interna, finalidad, estado y observaciones.
- Fechas y zona horaria de inicio y fin.
- Destinos, ciudades, países y escalas.
- Personas viajeras asignadas y gestor responsable.
- Segmentos de transporte, alojamientos, alquiler de vehículo, traslados y otros servicios.
- Contactos, localizadores, proveedores y costes si se habilita su tratamiento.

Estados sugeridos: `borrador`, `en preparación`, `confirmado`, `en curso`, `finalizado`, `cancelado`.

### 2.3 Carga y tratamiento documental

El sistema permitirá adjuntar documentos a un viaje: PDF, imágenes (JPG, PNG, TIFF), mensajes o confirmaciones exportadas y, opcionalmente, archivos EML. Cada documento conservará su original y metadatos de trazabilidad.

Flujo de procesamiento:

1. Validación de tipo, tamaño, antivirus y almacenamiento seguro.
2. OCR cuando el documento no contenga texto extraíble.
3. Clasificación documental: vuelo, tren, hotel, vehículo, seguro, visado, otro.
4. Extracción de campos estructurados mediante reglas y/o IA.
5. Normalización de fechas, zonas horarias, aeropuertos, estaciones, localizadores y nombres.
6. Presentación de una pantalla de revisión: el gestor acepta, corrige o descarta los datos extraídos.
7. Persistencia de datos aprobados y generación de un historial de cambios.

Los datos extraídos deberán guardar su **origen** (`documento`, `manual`, `IA`), un indicador de confianza y el documento/página de referencia cuando sea posible. Ningún dato de extracción se considerará definitivo sin confirmación del gestor cuando la confianza sea baja o haya conflicto.

### 2.4 Itinerario, alertas y validaciones

El sistema consolidará todos los servicios en una línea temporal por viajero y viaje. Deberá detectar, como mínimo:

- Conexiones de avión, tren o traslados con margen inferior al umbral configurable.
- Solapamientos de reservas o segmentos incompatibles.
- Llegada posterior al inicio de una reserva de hotel o vehículo.
- Datos incompletos: ausencia de localizador, fechas, destino, viajero o documento soporte.
- Pasaportes, visados, seguros o requisitos documentales próximos a vencer, si esos datos se habilitan.
- Cambios de zona horaria que afecten a la planificación.

Las alertas tendrán severidad (`informativa`, `media`, `alta`, `crítica`), descripción, regla generadora, fecha, estado (`abierta`, `aceptada`, `resuelta`, `descartada`) y responsable. Los umbrales deberán ser configurables por administración (por ejemplo, 90 min entre vuelos Schengen y 150 min en conexiones internacionales).

### 2.5 Asistente IA

La IA se integrará a través de una capa de proveedores intercambiable. El sistema deberá priorizar despliegues locales y permitir, por configuración, proveedores remotos:

| Modalidad | Uso previsto |
| --- | --- |
| **Ollama local** | Desarrollo, pruebas y despliegues con modelos locales. |
| **vLLM autogestionado** | Inferencia local/privada de mayor capacidad, compatible con APIs tipo OpenAI. |
| **OpenAI API** | Proveedor opcional para capacidades que requieran modelos remotos. |
| **DeepSeek API** | Proveedor opcional alternativo. |

Casos de uso de IA:

- Extraer y resumir información de documentos de viaje.
- Responder preguntas sobre un viaje, usando únicamente datos autorizados para quien consulta.
- Explicar alertas y proponer acciones correctivas.
- Generar un resumen ejecutivo e itinerario legible.
- Ayudar al gestor a investigar opciones de transporte y fuentes de información pública en Internet.
- Redactar recomendaciones de seguridad contextualizadas por fecha y destino.

Requisitos de control:

- Selección de proveedor/modelo por entorno y por tarea.
- Claves API almacenadas cifradas y nunca expuestas en cliente, logs ni prompts.
- Política explícita de salida de datos: por defecto, documentos y PII no salen a proveedores externos sin habilitación administrativa.
- Registro de cada ejecución: usuario, proveedor, modelo, finalidad, fuentes internas, fecha, coste/tokens si aplica y resultado resumido. No registrar el contenido sensible completo salvo configuración aprobada.
- Protección frente a *prompt injection*: los documentos y resultados web se tratarán como contenido no confiable y no como instrucciones.

### 2.6 Búsqueda de información pública

La búsqueda web se realizará como servicio separado y con fuentes permitidas/configuradas. Inicialmente deberá soportar consultas asistidas, no compras ni reservas automáticas.

Funciones previstas:

- Buscar opciones de vuelo/tren y condiciones relevantes a partir del itinerario.
- Consultar requisitos de entrada, visado, sanidad, conectividad y recomendaciones de seguridad.
- Mostrar enlaces, fecha de consulta, fuente y resumen; el gestor conserva la decisión final.

No se almacenarán ni reutilizarán credenciales de proveedores de viajes en fase 1. Si se incorporan conectores comerciales, deberán pasar por una evaluación legal, de seguridad y de términos de uso.

### 2.7 Recomendaciones de seguridad del viaje

Por cada viaje, el sistema generará recomendaciones vinculadas a destino(s), fecha(s), perfil de viaje y contexto. Deben mostrar fuente, fecha de actualización, nivel de confianza y advertencia de que no sustituyen a fuentes oficiales.

El diseño debe permitir integrar fuentes oficiales configurables (por ejemplo, avisos de viaje gubernamentales, organismos sanitarios y alertas de seguridad), con caché, caducidad y trazabilidad. En fase 1, la publicación final de una recomendación debe ser validable por un gestor.

## 3. Requisitos no funcionales

### 3.1 Arquitectura

- Aplicación modular Flask con patrón *application factory* y paquetes por dominio.
- API REST versionada (`/api/v1`) para frontend y futuras integraciones.
- Renderizado servidor o SPA desacoplada: decisión de implementación abierta; la autorización residirá siempre en backend.
- PostgreSQL como base de datos transaccional; SQLAlchemy + Alembic para ORM y migraciones.
- Almacenamiento de adjuntos fuera de PostgreSQL (S3 compatible/MinIO o volumen cifrado), conservando en base de datos únicamente metadatos, hash y ruta/objeto.
- Cola de trabajos asíncronos (Celery/RQ/Dramatiq con Redis o RabbitMQ) para OCR, antivirus, extracción IA, búsquedas y recalculo de alertas.
- Contenedorización Docker/Compose para desarrollo y despliegue reproducible; configuración por variables de entorno y gestor de secretos.

### 3.2 Seguridad

- TLS obligatorio, cabeceras de seguridad, protección CSRF, validación de entrada, limitación de tasa y gestión segura de sesión.
- Contraseñas locales con Argon2id o bcrypt con parámetros actuales; MFA preparado como evolución.
- RBAC aplicado en cada endpoint y descarga de fichero; comprobación de pertenencia al viaje en servidor.
- Cifrado en tránsito y cifrado de almacenamiento/documentos cuando la plataforma lo soporte.
- Escaneo antimalware previo al procesamiento; lista blanca de tipos y límites configurables de tamaño.
- Prevención de IDOR, XSS, SQL injection, SSRF (especialmente en la búsqueda web) y subida de archivos peligrosos.
- Logs estructurados sin PII ni secretos innecesarios; auditoría inmutable o con controles de integridad.
- Copias de seguridad, pruebas de restauración y política de retención/borrado de documentos y datos personales.
- Cumplimiento de RGPD: base jurídica, minimización, control de acceso, retención, derecho de acceso/supresión conforme a políticas corporativas y registro de tratamientos a validar por la organización.

### 3.3 Identidad: local ahora, AD/LDAP después

Definir una interfaz de autenticación (`IdentityProvider`) desacoplada de la lógica de negocio.

- **Fase 1:** proveedor `LocalIdentityProvider` con tabla de usuarios local.
- **Fase posterior:** proveedor `LDAP/ActiveDirectoryIdentityProvider` con mapeo configurable de atributos (`sAMAccountName`/UPN, email, nombre, grupos).
- Identificador interno inmutable (UUID) para relacionar viajes y usuarios; no usar el nombre de inicio de sesión como clave de negocio.
- Soportar aprovisionamiento JIT o sincronización programada, desactivación de cuentas y mapeo de grupos AD a roles internos.
- Mantener autorización RBAC propia aunque AD autentique, salvo que se decida explícitamente delegarla por grupos.

### 3.4 Rendimiento y disponibilidad

- Objetivo inicial: respuesta p95 inferior a 500 ms para consultas normales sin documentos; procesamiento documental asíncrono con estado visible.
- Paginación, filtrado y búsqueda indexada de viajes, usuarios y alertas.
- Índices PostgreSQL para claves foráneas, estados, fechas, usuarios asignados y búsquedas frecuentes.
- Health checks, métricas, trazas y alertas operativas.
- Diseño preparado para escalar workers de procesamiento horizontalmente.

## 4. Modelo de datos mínimo

| Entidad | Campos principales |
| --- | --- |
| `users` | id UUID, username, email, nombre, password_hash, estado, proveedor_identidad, external_id, last_login_at. |
| `roles` / `user_roles` | roles y asignaciones RBAC. |
| `trips` | id UUID, título, referencia, estado, inicio, fin, gestor_id, finalidad, observaciones. |
| `trip_travelers` | trip_id, user_id, rol_en_viaje, fechas aplicables. |
| `trip_destinations` | trip_id, país, ciudad, inicio, fin, orden, zona_horaria. |
| `travel_segments` | trip_id, traveler_id opcional, tipo, proveedor, origen, destino, salida, llegada, zona_horaria, localizador, datos JSONB. |
| `accommodations` | trip_id, traveler_id opcional, hotel, dirección, check_in/out, localizador, datos JSONB. |
| `vehicle_rentals` | trip_id, traveler_id opcional, proveedor, recogida/devolución, localizador, datos JSONB. |
| `documents` | id UUID, trip_id, tipo, nombre, objeto_storage, hash_sha256, mime, tamaño, estado_proceso, clasificación, subido_por, timestamps. |
| `extractions` | document_id, versión, estado, confianza, payload JSONB, modelo/proveedor, aprobado_por, timestamps. |
| `alerts` | trip_id, tipo, severidad, estado, mensaje, evidencia JSONB, generada_en, resuelta_por. |
| `security_advisories` | trip_id, destino, fuente, fecha_fuente, nivel, contenido, estado_validación, validado_por. |
| `ai_runs` | usuario_id, trip_id opcional, tarea, proveedor, modelo, estado, referencias_fuentes, métricas, timestamps. |
| `audit_events` | actor_id, acción, recurso_tipo/id, resultado, IP, user_agent, metadata minimizada, timestamp. |

Usar `JSONB` sólo para campos variables específicos del proveedor/documento. Los campos que se filtren, validen o relacionen frecuentemente deberán tener columnas tipadas.

## 5. Flujos clave y criterios de aceptación

### 5.1 Alta y asignación de viaje

1. Un gestor crea un viaje y añade destinos/fechas.
2. Asigna uno o varios usuarios existentes.
3. El usuario asignado visualiza el viaje al iniciar sesión.

**Aceptación:** un usuario no asignado recibe `403` al consultar el viaje o descargar su documentación; el gestor y administrador pueden consultarlo.

### 5.2 Carga y extracción de documentos

1. El gestor adjunta un PDF de reserva.
2. El archivo se analiza y procesa en segundo plano.
3. Se muestran campos extraídos y su confianza.
4. El gestor valida o corrige los campos.
5. El itinerario y alertas se recalculan.

**Aceptación:** el original permanece disponible con hash; los campos aprobados quedan vinculados al documento y se registra quién los confirmó.

### 5.3 Alertas de conexión

1. Dos segmentos consecutivos se incorporan al viaje.
2. El motor calcula el intervalo en UTC y respeta las zonas horarias de origen.
3. Si el margen es menor que el umbral, crea una alerta.

**Aceptación:** la alerta muestra los segmentos implicados, el margen calculado, el umbral utilizado y permite marcarla como aceptada/resuelta con comentario.

### 5.4 Consulta IA con control de acceso

1. Usuario o gestor pregunta por un viaje.
2. El backend recupera únicamente información a la que ese actor tiene acceso.
3. La IA genera respuesta con referencias internas y, si aplica, fuentes públicas.

**Aceptación:** no se incluye información de otros viajes ni de otros viajeros; la respuesta muestra la fecha y procedencia de los datos relevantes.

## 6. Integraciones y contratos

- **OCR:** motor local preferente (p. ej. Tesseract/PaddleOCR) con interfaz sustituible por servicio externo autorizado.
- **Antivirus:** ClamAV o servicio corporativo mediante adaptador.
- **IA:** contrato interno con operaciones `extract_document`, `answer_trip_question`, `summarize_trip`, `analyze_risks` y `research_public_info`.
- **Web/recomendaciones:** adaptadores por fuente; timeouts, allowlist de dominios, caché y saneamiento de contenido.
- **Correo/notificaciones:** fuera de alcance inicial, pero eventos de dominio preparados para avisar sobre alertas y cambios.

## 7. API inicial orientativa

- `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/me`
- `GET|POST /api/v1/users`, `PATCH /api/v1/users/{id}` (administración)
- `GET|POST /api/v1/trips`, `GET|PATCH /api/v1/trips/{id}`
- `POST /api/v1/trips/{id}/travelers`, `DELETE /api/v1/trips/{id}/travelers/{user_id}`
- `POST /api/v1/trips/{id}/documents`, `GET /api/v1/documents/{id}`, `POST /api/v1/documents/{id}/review`
- `GET /api/v1/trips/{id}/itinerary`, `GET|PATCH /api/v1/trips/{id}/alerts`
- `POST /api/v1/trips/{id}/ai/query`, `POST /api/v1/trips/{id}/research`
- `GET|POST /api/v1/trips/{id}/security-advisories`
- `GET /api/v1/audit-events` (solo administración, con filtros y paginación)

Todas las respuestas deberán usar un esquema consistente de errores, correlación de peticiones y validación OpenAPI.

## 8. Despliegue y operación

- Entornos independientes: desarrollo, pruebas/preproducción y producción.
- Secretos gestionados fuera del repositorio; rotación de credenciales y API keys.
- Migraciones versionadas y ejecutadas de forma controlada.
- Backups cifrados de PostgreSQL y almacenamiento documental; prueba periódica de restauración.
- Observabilidad: logs centralizados, métricas de API/workers, trazas y alertas de errores, saturación y fallos de procesamiento.
- Pipeline CI/CD con pruebas, análisis estático, escaneo de dependencias/contenedores y despliegue reproducible.

## 9. Plan de entregas recomendado

### Fase 1 — MVP seguro

- Autenticación local, RBAC y auditoría básica.
- CRUD de viajes, asignación de personas viajeras y vista de usuario.
- Carga segura de documentos y almacenamiento de originales.
- Extracción inicial de PDF/texto, revisión manual e itinerario consolidado.
- Alertas básicas de tiempo de conexión y solapamientos.
- Capa IA con Ollama/vLLM y proveedor API opcional, con controles de privacidad.
- Recomendaciones de seguridad con fuente, fecha y validación por gestor.

### Fase 2

- Integración AD/LDAP, sincronización de grupos y MFA/SSO según política corporativa.
- OCR avanzado, más tipos documentales y automatización de reglas.
- Conectores de fuentes oficiales y monitorización de cambios en recomendaciones.
- Notificaciones, informes y paneles de operación.

### Fase 3

- Integraciones con proveedores de viaje aprobados.
- Reglas avanzadas por política corporativa, costes y aprobaciones.
- Evaluación continua de modelos IA, *guardrails* y analítica de calidad.

## 10. Decisiones pendientes para el arranque

1. Países/organización objetivo y requisitos RGPD/retención aplicables.
2. Volumen previsto de usuarios, viajes, documentos y tamaño máximo por documento.
3. Entorno de despliegue (on-premise, nube privada o nube pública) y requisitos de alta disponibilidad.
4. Modelo local y recursos disponibles (CPU/GPU/RAM) para Ollama o vLLM.
5. Fuentes oficiales y fuentes comerciales autorizadas para seguridad e investigación de viajes.
6. Idiomas soportados en OCR, interfaz y modelos IA.
7. Política corporativa de conservación de originales y trazas de IA.

## 11. Fuera de alcance de la fase 1

- Compra o emisión automática de billetes y reservas.
- Pagos, gestión de gastos, conciliación o facturación.
- Sustituir asesoramiento oficial de seguridad, sanitario, migratorio o legal.
- Acceso directo de usuarios a viajes de terceros.

