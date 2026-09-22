/*
 * Elegir el modelo de entre los que el endpoint publica, en vez de escribirlo
 * de memoria: un nombre mal tecleado solo se descubre cuando ya falla una
 * extracción, y para entonces el documento lleva un rato en el pipeline.
 *
 * El campo de texto sigue siendo el que se envía. El desplegable se pone
 * delante y escribe en él, de modo que si la consulta falla —el endpoint
 * apagado, un proxy en medio— la pantalla vuelve a ser exactamente la de
 * antes y el ajuste se sigue pudiendo cambiar a mano.
 */
window.SelectorDeModelo = (function () {
    'use strict';

    // Ningún modelo puede llamarse así, de modo que la opción de escape nunca
    // choca con uno real.
    var OTRO = '\u0000otro';

    var cache = {};

    function consultar(url, refrescar) {
        if (refrescar) { delete cache[url]; }
        if (!cache[url]) {
            cache[url] = fetch(url, {headers: {'Accept': 'application/json'}})
                .then(function (r) {
                    if (!r.ok) { throw new Error(r.status); }
                    return r.json();
                })
                .catch(function () {
                    return {modelos: [], error: 'No se pudo consultar el endpoint.'};
                });
        }
        return cache[url];
    }

    function opcion(select, valor, etiqueta) {
        var o = document.createElement('option');
        o.value = valor;
        o.textContent = etiqueta === undefined ? valor : etiqueta;
        select.appendChild(o);
        return o;
    }

    /**
     * conf = {input, url, vacio, estado}
     *   input  — el campo de texto que se envía con el formulario
     *   url    — endpoint JSON {modelos: [...], error: "..."}; vacío = sin lista
     *   vacio  — etiqueta de la opción sin valor
     *   estado — elemento donde contar lo encontrado (opcional)
     *
     * Devuelve {cargar(url, refrescar)} para volver a preguntar cuando el
     * proveedor cambia.
     */
    function enlazar(conf) {
        var input = conf.input;
        if (!input) { return null; }

        var select = document.createElement('select');
        select.className = input.className.replace(/form-control/g, 'form-select');
        select.setAttribute('aria-label', input.getAttribute('aria-label') || 'Modelo');
        // «.form-control» es display:block, y una regla de autor gana al
        // «[hidden]» del navegador: ocultar con el atributo no ocultaría nada.
        select.style.display = 'none';
        input.parentNode.insertBefore(select, input);

        // La etiqueta debe señalar al control que se ve, no al que se esconde.
        var etiqueta = input.id
            ? input.parentNode.querySelector('label[for="' + input.id + '"]')
            : null;
        if (etiqueta) { select.id = input.id + '__opciones'; }

        function mostrar(elemento, visible) {
            elemento.style.display = visible ? '' : 'none';
            if (etiqueta && visible) { etiqueta.setAttribute('for', elemento.id); }
        }

        var estado = conf.estado || null;

        function decir(texto, clase) {
            if (!estado) { return; }
            estado.textContent = texto;
            estado.className = 'form-text' + (clase ? ' ' + clase : '');
        }

        function soloTexto(mensaje, clase) {
            mostrar(select, false);
            mostrar(input, true);
            if (mensaje) { decir(mensaje, clase); } else { decir(''); }
        }

        function pintar(datos) {
            var modelos = datos.modelos || [];
            if (!modelos.length) {
                soloTexto(
                    datos.error || 'El endpoint no ofrece ningún modelo; escriba el nombre.',
                    datos.error ? 'text-danger' : 'text-warning'
                );
                return;
            }

            var actual = input.value;
            select.innerHTML = '';
            opcion(select, '', conf.vacio || '— sin especificar —');
            modelos.forEach(function (n) { opcion(select, n); });

            // Un valor guardado que ya no se publica —un modelo borrado del
            // servidor— se conserva como opción: hacerlo desaparecer al abrir
            // la pantalla cambiaría la configuración sin que nadie lo pidiera.
            if (actual && modelos.indexOf(actual) === -1) {
                opcion(select, actual, actual + ' · ya no está en el endpoint');
            }
            opcion(select, OTRO, 'Otro… (escribirlo a mano)');

            select.value = actual;
            mostrar(input, false);
            mostrar(select, true);

            decir(
                modelos.length === 1
                    ? '1 modelo disponible en el endpoint.'
                    : modelos.length + ' modelos disponibles en el endpoint.',
                'text-success'
            );
        }

        select.addEventListener('change', function () {
            if (select.value === OTRO) {
                mostrar(input, true);
                input.value = '';
                input.focus();
            } else {
                mostrar(input, false);
                input.value = select.value;
            }
        });

        function cargar(url, refrescar) {
            if (!url) {
                soloTexto('');
                return;
            }
            decir('Consultando el endpoint…');
            consultar(url, refrescar).then(pintar);
        }

        cargar(conf.url);
        return {cargar: cargar};
    }

    return {enlazar: enlazar};
})();
