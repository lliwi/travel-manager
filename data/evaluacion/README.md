# Conjunto de evaluación

Documentos cuya extracción correcta alguien escribió una vez, para que cambiar
de modelo deje de ser un acto de fe disfrazado de cambio de configuración.

Cada caso son dos ficheros: el texto del documento y un `.json` que dice qué
debería salir de él. Se ejecutan con:

```bash
docker compose -f docker/docker-compose.yml exec web flask evaluar
docker compose -f docker/docker-compose.yml exec web flask evaluar --modelo gpt-5.6-terra
```

**Los documentos son sintéticos a propósito.** Están calcados de la estructura
de confirmaciones reales —la tabla de ida y vuelta, los asientos por trayecto,
el desglose de precios— pero los nombres, localizadores y direcciones están
inventados. Un conjunto de evaluación vive en el repositorio y se lee en cada
revisión: meter ahí la reserva de alguien sería publicar sus datos para siempre
a cambio de una comodidad.

Para añadir un caso: guarde el texto del documento y un JSON hermano con
`documento`, `clasificacion`, `servicios_esperados` y `esperado`. Ponga en
`esperado` solo los campos cuya respuesta correcta sea indiscutible; un campo
opinable convierte la nota en ruido.

## Tareas que no son extraer

`tareas/<tarea>/*.json` mide la consulta sobre el viaje, el resumen y la
explicación de alertas. Cada caso trae la entrada con la misma forma que la que
genera la aplicación (`datos` del viaje, `pregunta`, `alerta`) y unos
`criterios` que se comprueban literalmente:

```json
"criterios": {
  "debe_mencionar": ["18:05", "VY7607"],
  "no_debe_mencionar": ["07:55"],
  "campos": {"datos_insuficientes": false}
}
```

La nota es el porcentaje de criterios cumplidos. No se pide a otro modelo que
puntúe: la nota sería tan ruidosa como lo que se mide.

Estos mismos casos son los que optimiza el autoajuste:

```bash
docker compose -f docker/docker-compose.yml exec web flask autoajustar --tarea summarize_trip
```
