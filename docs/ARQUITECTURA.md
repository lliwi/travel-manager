# Decisiones de arquitectura

Este documento recoge las decisiones de diseño que no se deducen leyendo el
código, y el motivo de cada una.

## 1. Los instantes se guardan como un triple

Cada momento del itinerario ocupa tres columnas:

| Columna | Tipo | Contenido |
| --- | --- | --- |
| `<prefijo>_local` | `TIMESTAMP` sin zona | La hora que figura en el billete |
| `<prefijo>_tz` | `VARCHAR(64)` | Nombre IANA, p. ej. `Europe/Madrid` |
| `<prefijo>_utc` | `TIMESTAMPTZ` | Derivada, y la única que se compara |

**Por qué.** El requerimiento pide calcular márgenes de conexión en UTC
respetando la zona de cada extremo (§5.3) y detectar cambios de huso que afecten
a la planificación (§2.4). Una sola columna no sirve para ambas cosas:

- Guardando solo UTC se pierde la hora que el viajero lee en su billete.
- Guardando solo la hora local no se pueden restar dos instantes de husos
  distintos.

El caso que lo demuestra: un vuelo que sale de Madrid a las 23:30 del 28 de
marzo de 2026 y aterriza a las 03:15 del día siguiente parece durar 3 h 45, pero
esa madrugada los relojes se adelantan una hora: la duración real son 2 h 45.
Un motor de alertas que reste horas locales se equivoca, y se equivoca en la
dirección peligrosa.

`timeutil.set_instant` es el único sitio que deriva `_utc`. Escribir esa columna
en cualquier otro punto permite que las tres se desincronicen.

## 2. La procedencia vive en una tabla aparte

`field_provenance` guarda, por cada valor almacenado, de dónde salió.

**Por qué no columnas en cada entidad.** Un documento respalda una docena de
campos y un campo puede corregirse varias veces. La relación es genuinamente de
muchos a uno, y la fila más reciente por `(entidad, campo)` es la procedencia
actual mientras las anteriores son el historial de cambios que pide el §2.3.

**La frontera entre los tres orígenes** —que el requerimiento nombra pero no
define— se fija así:

- `documento`: el valor aparece literalmente en el texto. `fragmento` y `pagina`
  están poblados.
- `ia`: el modelo lo infirió sin que exista un fragmento literal, por ejemplo
  una zona horaria deducida de un código IATA.
- `manual`: lo escribió o corrigió una persona.

## 3. La autorización existe una sola vez

`authorization_service.can(actor, permiso, recurso)` responde a todas las
preguntas de acceso. Los decoradores de ruta son envoltorios finos, y la
diferencia entre la interfaz y la API es únicamente cómo se renderiza la
excepción resultante.

`_trip_of` es un `singledispatch`: añadir una entidad nueva cuesta una línea y
ninguna rama dentro de `can()`.

**Denegar con 404.** Cuando el actor no tiene ninguna relación con el recurso se
responde 404 en lugar de 403. El §5.1 habla de 403 para el usuario no asignado;
denegar con 404 cumple el requisito (se deniega el acceso) y además no confirma
que el viaje exista, de modo que no se puede sondear el sistema para descubrir
identificadores válidos. Cuando el actor sí está en el viaje pero le falta un
permiso concreto, sí recibe 403: ya sabe que el recurso existe.

## 4. El aislamiento entre viajeros es estructural

El §5.4 exige que una consulta de IA no incluya información de otros viajeros.
Se cumple porque `build_ai_context` construye el contexto con
`scope_itinerary_items`: los segmentos de otro viajero no están en el payload.
El modelo no puede revelar lo que nunca recibió.

La alternativa —pedirle al modelo que no mencione a otros viajeros— depende de
que obedezca. Esta no.

## 5. El motor de alertas reconcilia

Cada hallazgo deriva una `dedup_key` determinista a partir del código de regla,
el viaje, el viajero y los identificadores ordenados de las entidades
implicadas. El reconciliador garantiza cuatro invariantes:

1. No duplica una alerta ya abierta con la misma clave.
2. Actualiza la evidencia y la severidad si cambiaron.
3. Cierra automáticamente lo que ya no se reproduce, marcándolo como cierre
   automático.
4. **No deshace una decisión humana.** Una alerta que un gestor aceptó o
   descartó se queda como él la dejó.

La cuarta es la que da sentido al botón de aceptar: si recalcular reabriera la
alerta, aceptarla no significaría nada.

Las reglas reciben un `TripContext` ya cargado y **no tocan la base de datos**.
Eso las hace testeables con factories y permite evaluar ocho reglas con un solo
conjunto de consultas.

### La tarjeta de embarque es un tipo documental propio

El billete existe desde que se compra; la tarjeta de embarque, solo desde que
alguien factura. Sin distinguirlos, un vuelo con su reserva adjunta y sin
facturar se ve exactamente igual que uno con todo en regla.

La regla `sin_tarjeta_de_embarque` avisa cuando un vuelo sale dentro del umbral
—48 horas, que es cuando las aerolíneas suelen abrir la facturación— y no hay
ninguna tarjeta adjunta que le corresponda. Es la única regla que describe algo
que **todavía no ha pasado** en lugar de algo que está mal; se cierra sola al
adjuntar la tarjeta, porque el motor reconcilia.

**Emparejar la tarjeta con su vuelo es estrecho a propósito.** Una tarjeta que
no se reconoce produce un aviso para un vuelo ya facturado: una molestia. Una
tarjeta emparejada con el vuelo equivocado silencia el aviso de uno que nadie
ha facturado: el fallo que la regla existe para evitar. Por eso solo cuentan
dos señales: que el segmento se creara a partir de ese documento, o que el
texto del documento nombre ese número de vuelo. Una tarjeta de ida nunca
responde por la vuelta.

El reconocimiento automático sigue el mismo criterio: la frase «tarjeta de
embarque» basta, pero mencionar «puerta de embarque» no, porque los itinerarios
lo dicen como consejo. Y solo reetiqueta un documento declarado de forma
genérica: quien lo subió dijo para qué era, y contradecir a una persona porque
una expresión regular opina distinto es al revés de como debe funcionar.

## 6. Los umbrales de conexión

El requerimiento da 90 minutos para conexiones Schengen y 150 para
internacionales, pero no dice qué hace que una conexión sea Schengen. La regla
lo decide por los tres países que toca el trasbordo —origen del tramo que llega,
el nodo, y destino del tramo que sale—, porque eso es lo que determina si se
cruza una frontera exterior. Un país desconocido usa el umbral más exigente:
suponer «probablemente Schengen» relajaría en silencio un control de seguridad.

Los umbrales base ya asumen el caso normal de conectar dentro de un mismo
aeropuerto, que es lo que es una conexión. Lo que sí cambia la cuenta es que la
conexión **no** sea en el mismo sitio: entonces se suma el tiempo del
desplazamiento entre aeropuertos.

## 7. El pipeline documental es reanudable

Ocho tareas encadenadas, cada una con tres propiedades:

- **Idempotente**: sale sin hacer nada si el documento no está en su estado de
  entrada, de modo que una reentrega de Celery no duplica trabajo.
- **Dirigida por la base de datos**: lee sus entradas de la base, no del retorno
  de la tarea anterior. Esto es lo que permite reanudar desde el paso que falló.
- **Con clases de error explícitas**: `TransientError` reintenta con espera
  creciente; `PermanentError` (archivo infectado, tipo incorrecto, hash que no
  cuadra) falla de inmediato, porque reintentar no puede arreglarlo.

El estado solo lo escribe `document_service.transition`, que valida la
transición contra un mapa explícito y la registra. Una prueba comprueba que
ningún otro módulo asigna `estado_proceso`.

## 8. La capa de IA

**Separación.** Un *proveedor* sabe hablar con un endpoint de inferencia y nada
más: no sabe de viajes, ni de permisos, ni de auditoría. Eso es lo que permite
que Ollama, vLLM, OpenAI y DeepSeek sean intercambiables.

**Defensa frente a inyección**, en dos capas que no dependen de la buena
voluntad del modelo:

1. El contenido no confiable viaja delimitado y con los delimitadores del propio
   documento neutralizados, de modo que un documento no puede cerrar su propio
   bloque y hacer que el resto se lea como instrucciones.
2. La respuesta se exige contra un esquema JSON. Un «ignora tus instrucciones»
   inyectado no tiene ningún campo donde aterrizar.

Como red adicional, las referencias internas que devuelve el modelo se validan
contra el conjunto autorizado: una que no estaba en el contexto se descarta.

**Política de salida.** `ai/guard.py` ya no rechaza nada: la decisión es a qué
proveedor está vinculada cada tarea, y esa se toma en Administración →
Proveedores de IA, a conciencia y dejando registro. Un segundo interruptor que
confirme que se quería lo que ya se eligió acaba permanentemente encendido, que
es un control solo de nombre.

Lo que queda en su lugar es la traza: cada llamada escribe un `ai_runs` con el
proveedor, el modelo, la finalidad y el viaje, de modo que «qué salió fuera y
cuándo» sigue teniendo respuesta. El `guard` conserva su punto de enganche para
que una regla tenga un sitio al que ir si una organización la necesita.

## 9. La auditoría es una cadena

Cada evento incluye el hash del anterior y el suyo propio, calculado sobre su
contenido. Alterar o borrar una fila directamente en la base de datos rompe la
cadena y `flask verify-audit` lo detecta.

El timestamp se normaliza antes de entrar en el hash: PostgreSQL devuelve un
valor con zona y SQLite uno sin ella, y si el resumen dependiera de esa
diferencia toda la tabla parecería manipulada después de una restauración.

La clave primaria es `BIGSERIAL` en lugar del UUID que usa el resto del modelo.
Es una desviación consciente del §4: la tabla es un registro append-only al que
ninguna clave ajena de negocio apunta, y una clave monotónica da inserciones
ordenadas, un índice más compacto y paginación por keyset barata.

## 10. Identidad desacoplada

`IdentityProvider` define el contrato; `LocalIdentityProvider` es la única
implementación de la fase 1. Lo que ya existe para que LDAP entre sin migrar
nada: `users.identity_provider`, `users.external_id`, `password_hash` anulable,
la tabla `role_group_mappings` (vacía) y el hecho de que todas las claves ajenas
apuntan al UUID inmutable y nunca al nombre de inicio de sesión.

La autorización sigue siendo nuestra aunque autentique el directorio, tal como
pide el §3.3.

## 11. Las métricas se consultan, no se acumulan

La aplicación corre en cuatro workers de gunicorn. Un contador en memoria vive
en uno de ellos, así que un scrape atendido por cualquier otro informa de la
cuarta parte del tráfico: un número equivocado con forma de número correcto,
que se lee como un día tranquilo y no como una avería.

Por eso las cifras del negocio —documentos por estado, alertas abiertas,
ejecuciones de IA— se consultan a la base de datos en el momento del scrape, a
través de un colector propio, en lugar de mantenerse como *gauges* que alguien
tenga que acordarse de actualizar. Una consulta da la misma respuesta desde
cualquier worker, sobrevive a un reinicio y no puede desincronizarse. Es el
mismo criterio que el aislamiento entre viajeros: preferir lo que no puede
salir mal a lo que hay que recordar hacer bien.

Tiene un segundo efecto, y es el que resuelve «métricas de workers» del §8: el
worker de Celery queda observable sin exponer ningún puerto ni levantar un
servidor HTTP dentro, porque lo que hace acaba en esas mismas tablas. Medir las
tablas es medir al worker.

Lo único que sí se cuenta en proceso es la latencia de las peticiones, porque
no existe en ningún otro sitio. Para eso `docker/flask/gunicorn.conf.py` vacía
el directorio compartido al arrancar y recoge los ficheros de cada worker que
muere; sin esas dos cosas los totales arrastran ejecuciones anteriores o el
directorio crece sin límite.

### Quién puede leerlas

Un recolector no inicia sesión, así que `/metrics` no puede exigirla y los
decoradores habituales no sirven. Se defiende por dos vías y no hay una
tercera: el token configurado en `METRICS_TOKEN`, o venir de dentro de la red.
Nginx lo niega además en el borde. No existe una configuración en la que las
cifras de viajes, usuarios y documentos se lean desde internet, y el caso de
«sin token» no abre nada: cae en la comprobación de red, que es de lo que
depende un despliegue en una sola máquina.
