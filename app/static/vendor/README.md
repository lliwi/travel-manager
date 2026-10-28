# Recursos de terceros servidos desde la aplicación

Bootstrap, sus iconos y la fuente Inter se sirven desde aquí y no desde un CDN.
Detrás de un proxy corporativo (Zscaler, en la instalación donde se vio) lo que
viene de `cdn.jsdelivr.net` o de Google Fonts llega bloqueado, y la aplicación
se mostraba sin estilos. La política de contenido (CSP) solo admite el propio
servidor, así que cualquier recurso externo que se añada en el futuro aparecerá
bloqueado de inmediato en lugar de depender de la red de cada cual.

| Recurso | Versión | Origen | Licencia |
| --- | --- | --- | --- |
| Bootstrap (CSS y JS) | 5.3.3 | npm `bootstrap` | MIT |
| Bootstrap Icons | 1.11.3 | npm `bootstrap-icons` | MIT |
| Inter (variable) | 5.3.0 | npm `@fontsource-variable/inter` | SIL OFL 1.1 |
| flatpickr (y su traducción `es`) | 4.6.13 | npm `flatpickr` | MIT |

Se han quitado las referencias `sourceMappingURL` de los ficheros de Bootstrap,
que harían pedir al navegador mapas que no están.

flatpickr sustituye el selector de fecha del navegador: el nativo se dibuja en
el idioma del navegador, y uno configurado en inglés mostraba mm/dd/aaaa por
mucho que la aplicación esté en español.

Para actualizar uno: descargar la nueva versión en su carpeta, actualizar esta
tabla y las sumas de abajo, y probar la interfaz.

## Sumas SHA-256

```
073254afbfc06331b8b548b7fc0532b4ffe2cfdd588368dcc338e7abd50810e1  ./bootstrap/bootstrap.bundle.min.js
26db49828d6701fcfce37a96da6ec3f0ed481abae49c8c9969a575b064413cad  ./bootstrap/bootstrap.min.css
f643d6fe7e679f9de3e16311600c5ef5cd6b098f7a3a8828fcc29255d2b33e62  ./bootstrap-icons/bootstrap-icons.min.css
bb1de989b83970f6f4e54de1cd974c5cba55b73582da5e1b225a6d0edf029483  ./bootstrap-icons/fonts/bootstrap-icons.woff
476adf42b40325098fcfa8b36ab3e769186bb4f6ce6a249753e2e1a9c22bf99e  ./bootstrap-icons/fonts/bootstrap-icons.woff2
0fb3e11bd57e896c5a512afd64864d28a37de45d19835016c87ca1ad19ead969  ./bootstrap-icons/LICENSE
8c14611ae41ac6fd543c13349f22188eb12c69b3e59105c5eca3925a8e4eca3e  ./bootstrap/LICENSE
af29d75c4f7e336a9a7e06d2c14630a1521ebbd2aa478a6fa6ec06ae0fcc400e  ./inter/inter.css
34b9c504cab7a73e37b746343a449132e56cf7b5481af2cb81dc74dcff25c956  ./inter/inter-latin-ext-wght-normal.woff2
3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62  ./inter/inter-latin-wght-normal.woff2
3b0a5fca3d17942cde889069889dedbbbd075e9b599968c82a95f4d944e9b345  ./inter/LICENSE
1eeab1cb779471a0b0aaa93dd91c2eb1aa537d696f01ab05ea9dabc55e8525a1  ./flatpickr/flatpickr.min.js
1b34a42552c96f10e4dfaaa4a367276b03868aacff63c1ac42ffe331352bc754  ./flatpickr/flatpickr.min.css
1b96fff57937da386abf4186e9970d6a53d0fa587f00d1062c7615e812e4b08c  ./flatpickr/es.js
5fd57afc3905651175947ae44818e91ffcf5b605e4c88fac42d9700243bd9095  ./flatpickr/LICENSE
```
