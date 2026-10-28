/* Travel Manager — progressive enhancement only.
   Every page works without JavaScript; this adds convenience. */
(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', function () {
        highlightActiveNav();
        wireSidebarToggle();
        autoDismissFlashes();
        confirmDestructiveForms();
        pollProcessingDocuments();
        enhanceDateRanges();
        enhanceDateInputs();
    });

    /**
     * One calendar for a pair of dates that are a span of days.
     *
     * The start field says it is one (``data-rango-fin`` names its end). A
     * visible box takes its place, where both days are chosen at once, and no
     * hour is asked: the start gets ``data-hora-inicio`` and the end
     * ``data-hora-fin``, so the server receives exactly what the two native
     * fields would have sent. A value already saved is left as it was until
     * somebody changes the days.
     */
    /** flatpickr's year boxes come without a label; a screen reader says nothing. */
    function etiquetarCalendario(_fechas, _texto, selector) {
        selector.calendarContainer.querySelectorAll('.cur-year').forEach(function (anio) {
            anio.setAttribute('aria-label', 'Año');
        });
    }

    function enhanceDateRanges() {
        if (typeof window.flatpickr !== 'function') { return; }
        var es = window.flatpickr.l10ns && window.flatpickr.l10ns.es;

        document.querySelectorAll('input[data-rango-fin]').forEach(function (inicio) {
            var fin = document.getElementById(inicio.getAttribute('data-rango-fin'));
            if (!fin) { return; }
            var horaInicio = inicio.getAttribute('data-hora-inicio') || '00:00';
            var horaFin = inicio.getAttribute('data-hora-fin') || '23:59';

            var abierto = inicio.hasAttribute('data-rango-abierto');
            var dia = function (valor) { return (valor || '').slice(0, 10); };

            var visible = document.createElement('input');
            visible.type = 'text';
            visible.className = inicio.className;
            visible.placeholder = abierto
                ? 'dd/mm/aaaa (ida) – dd/mm/aaaa (vuelta, opcional)'
                : 'dd/mm/aaaa – dd/mm/aaaa';
            visible.id = inicio.id;
            inicio.removeAttribute('id');
            inicio.type = 'hidden';
            fin.type = 'hidden';
            inicio.parentNode.insertBefore(visible, inicio);

            var etiqueta = document.querySelector('label[for="' + visible.id + '"]');
            if (etiqueta && inicio.getAttribute('data-rango-etiqueta')) {
                // Keep the «opcional» mark the macro put inside the label.
                etiqueta.firstChild.textContent = inicio.getAttribute('data-rango-etiqueta') + ' ';
            }
            var campoFin = fin.closest('.campo');
            var etiquetaFin = fin.id && document.querySelector('label[for="' + fin.id + '"]');
            // A label pointing at a hidden input is a label for nothing.
            if (etiquetaFin) { etiquetaFin.removeAttribute('for'); }
            if (campoFin) {
                var columna = campoFin.parentElement;
                var sola = columna && columna.children.length === 1 &&
                    /\bcol-/.test(columna.className);
                (sola ? columna : campoFin).hidden = true;
                // The two halves shared a row; the one box left takes it.
                var columnaInicio = visible.closest('[class*="col-"]');
                if (sola && columnaInicio && columnaInicio.parentElement === columna.parentElement) {
                    columnaInicio.className = 'col-12';
                }
            }

            // As dates, not text: the calendar reads text in its display
            // format, and «2026-10-28» read as dd/mm/aaaa is the 20th of June.
            var comoFecha = function (valor) {
                var partes = dia(valor).split('-');
                return partes.length === 3
                    ? new Date(+partes[0], +partes[1] - 1, +partes[2]) : null;
            };
            var iniciales = [comoFecha(inicio.value), comoFecha(fin.value)].filter(Boolean);
            var anchas = window.matchMedia('(min-width: 768px)').matches;

            window.flatpickr(visible, {
                locale: es || 'default',
                onReady: etiquetarCalendario,
                mode: 'range',
                dateFormat: 'd/m/Y',
                showMonths: anchas ? 2 : 1,
                defaultDate: iniciales,
                onChange: function (fechas, _texto, selector) {
                    if (!fechas.length) {
                        inicio.value = '';
                        fin.value = '';
                        return;
                    }
                    var ultimo = fechas.length > 1 ? fechas[1] : (abierto ? null : fechas[0]);
                    inicio.value = selector.formatDate(fechas[0], 'Y-m-d') + 'T' + horaInicio;
                    // A single day is a span of one day -- or, where the end is
                    // optional (a one-way journey), no end at all.
                    fin.value = ultimo ? selector.formatDate(ultimo, 'Y-m-d') + 'T' + horaFin : '';
                }
            });
        });
    }

    /**
     * Show dates as dd/mm/aaaa whatever language the browser speaks.
     *
     * A native date input is drawn in the browser's own locale, so a browser
     * set to English shows mm/dd/yyyy and nothing the page says changes that.
     * flatpickr takes over the visible box; the field it replaces keeps the
     * ISO value the server has always received, so no form had to change.
     * Without JavaScript the native inputs are still there and still work.
     */
    function enhanceDateInputs() {
        if (typeof window.flatpickr !== 'function') { return; }
        var es = window.flatpickr.l10ns && window.flatpickr.l10ns.es;

        document.querySelectorAll('input[type="date"], input[type="datetime-local"]')
            .forEach(function (input) {
                var conHora = input.type === 'datetime-local';
                input.type = 'text';
                var selector = window.flatpickr(input, {
                    locale: es || 'default',
                    onReady: etiquetarCalendario,
                    enableTime: conHora,
                    time_24hr: true,
                    minuteIncrement: 5,
                    allowInput: true,
                    dateFormat: conHora ? 'Y-m-d\\TH:i' : 'Y-m-d',
                    altInput: true,
                    altFormat: conHora ? 'd/m/Y H:i' : 'd/m/Y'
                });
                if (selector.altInput) {
                    selector.altInput.placeholder = conHora ? 'dd/mm/aaaa hh:mm' : 'dd/mm/aaaa';
                    // The form's <label for> points at the id: it has to be on
                    // the box people type in, not on the hidden one that submits.
                    if (input.id) {
                        selector.altInput.id = input.id;
                        input.removeAttribute('id');
                    }
                }
            });
    }

    /** Mark the sidebar link matching the current path. */
    function highlightActiveNav() {
        var path = window.location.pathname;
        var best = null;
        var bestLength = 0;

        document.querySelectorAll('.sidebar-nav .nav-link').forEach(function (link) {
            var href = link.getAttribute('href');
            if (!href || href === '#') { return; }
            // Longest matching prefix wins, so /trips/new does not light up /trips.
            if (path === href || (path.indexOf(href) === 0 && href.length > bestLength)) {
                best = link;
                bestLength = href.length;
            }
        });

        if (best) { best.classList.add('active'); }
    }

    function wireSidebarToggle() {
        var toggle = document.getElementById('sidebarToggle');
        var sidebar = document.getElementById('sidebar');
        if (!toggle || !sidebar) { return; }

        toggle.addEventListener('click', function () {
            sidebar.classList.toggle('open');
        });

        document.addEventListener('click', function (event) {
            if (window.innerWidth >= 992) { return; }
            if (!sidebar.contains(event.target) && !toggle.contains(event.target)) {
                sidebar.classList.remove('open');
            }
        });
    }

    /** Fade out success and info messages; leave warnings and errors alone. */
    function autoDismissFlashes() {
        document.querySelectorAll('.alert-success, .alert-info').forEach(function (el) {
            if (!el.classList.contains('alert-dismissible')) { return; }
            window.setTimeout(function () {
                var alert = window.bootstrap && window.bootstrap.Alert.getOrCreateInstance(el);
                if (alert) { alert.close(); }
            }, 6000);
        });
    }

    /** Ask before submitting anything marked destructive.
     *  Never an inline «onsubmit»: the CSP refuses handler attributes, so
     *  that confirmation silently disappeared and the action went ahead.
     *  On a button rather than its form when only one of several submit
     *  buttons is destructive -- «Descartar» next to «Aprobar». */
    function confirmDestructiveForms() {
        document.querySelectorAll('form[data-confirm]').forEach(function (form) {
            form.addEventListener('submit', function (event) {
                if (!window.confirm(form.dataset.confirm)) {
                    event.preventDefault();
                }
            });
        });
        document.querySelectorAll('button[data-confirm]').forEach(function (button) {
            button.addEventListener('click', function (event) {
                if (!window.confirm(button.dataset.confirm)) {
                    event.preventDefault();
                }
            });
        });
    }

    /** Refresh the page while a document is still being processed.
     *  Document processing is asynchronous, so a static page would show a
     *  stale state until the user reloaded by hand. */
    function pollProcessingDocuments() {
        var inProgress = document.querySelector('[data-document-processing="true"]');
        if (!inProgress) { return; }
        window.setTimeout(function () { window.location.reload(); }, 8000);
    }

    /** CSRF token for fetch() calls made by page-specific scripts. */
    window.csrfToken = function () {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    };
})();

/* Zona de arrastre para los campos de archivo.
 *
 * Mejora progresiva: el input sigue ahí y sigue siendo quien envía. Si esto no
 * se ejecuta —JavaScript desactivado, un navegador sin DataTransfer— el campo
 * se comporta como cualquier otro campo de archivo. Lo que añade es un blanco
 * más grande y la lista de lo que se va a mandar, que es lo que de verdad se
 * echa en falta: un input de archivos enseña un nombre y miente sobre el resto.
 */
(function () {
    'use strict';

    function formatearTamano(bytes) {
        if (bytes < 1024) { return bytes + ' B'; }
        if (bytes < 1024 * 1024) { return (bytes / 1024).toFixed(0) + ' KB'; }
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function pintarLista(zona, input) {
        var lista = zona.querySelector('.dropzone-list');
        if (!lista) { return; }

        lista.innerHTML = '';
        Array.prototype.forEach.call(input.files || [], function (archivo) {
            var fila = document.createElement('li');
            var nombre = document.createElement('span');
            nombre.className = 'text-truncate';
            nombre.textContent = archivo.name;
            var tamano = document.createElement('span');
            tamano.className = 'dropzone-size';
            tamano.textContent = formatearTamano(archivo.size);
            fila.appendChild(nombre);
            fila.appendChild(tamano);
            lista.appendChild(fila);
        });
    }

    function conectar(zona) {
        var input = document.getElementById(zona.dataset.input);
        if (!input) { return; }

        input.addEventListener('change', function () { pintarLista(zona, input); });

        ['dragenter', 'dragover'].forEach(function (evento) {
            zona.addEventListener(evento, function (e) {
                e.preventDefault();
                zona.classList.add('is-dragging');
            });
        });

        ['dragleave', 'drop'].forEach(function (evento) {
            zona.addEventListener(evento, function (e) {
                e.preventDefault();
                // «dragleave» salta también al pasar sobre un hijo; sin esto la
                // zona parpadea mientras se arrastra por encima.
                if (evento === 'dragleave' && zona.contains(e.relatedTarget)) { return; }
                zona.classList.remove('is-dragging');
            });
        });

        zona.addEventListener('drop', function (e) {
            var soltados = e.dataTransfer && e.dataTransfer.files;
            if (!soltados || !soltados.length || typeof DataTransfer === 'undefined') {
                return;
            }

            var destino = new DataTransfer();
            var admiteVarios = input.multiple;

            // Lo ya elegido se conserva: arrastrar un segundo documento sobre la
            // zona es añadirlo, no empezar de nuevo.
            if (admiteVarios) {
                Array.prototype.forEach.call(input.files || [], function (archivo) {
                    destino.items.add(archivo);
                });
            }

            Array.prototype.forEach.call(soltados, function (archivo) {
                if (admiteVarios || destino.items.length === 0) {
                    destino.items.add(archivo);
                }
            });

            input.files = destino.files;
            pintarLista(zona, input);
            // Para que cualquier validación enganchada al campo se entere.
            input.dispatchEvent(new Event('change', {bubbles: true}));
        });

        pintarLista(zona, input);
    }

    /* Un fichero soltado fuera de la zona lo abre el navegador, que abandona
       la página y se lleva por delante lo que hubiera escrito en el formulario.
       Al apuntar cerca pero fallar, el precio de errar no puede ser perder el
       trabajo: fuera de la zona, soltar no hace nada. */
    ['dragover', 'drop'].forEach(function (evento) {
        document.addEventListener(evento, function (e) {
            if (!e.target.closest || !e.target.closest('.js-dropzone')) {
                e.preventDefault();
            }
        });
    });

    /* Mientras se arrastra algo por la ventana, las zonas se marcan solas: el
       blanco al que hay que apuntar se ve antes de soltar, no después. */
    var profundidad = 0;
    document.addEventListener('dragenter', function (e) {
        if (!e.dataTransfer || !contieneArchivos(e.dataTransfer)) { return; }
        profundidad += 1;
        document.body.classList.add('hay-arrastre');
    });
    ['dragleave', 'drop'].forEach(function (evento) {
        document.addEventListener(evento, function () {
            profundidad = evento === 'drop' ? 0 : Math.max(0, profundidad - 1);
            if (profundidad === 0) { document.body.classList.remove('hay-arrastre'); }
        });
    });

    function contieneArchivos(dt) {
        return Array.prototype.indexOf.call(dt.types || [], 'Files') !== -1;
    }

    document.querySelectorAll('.js-dropzone').forEach(conectar);
})();


/* El aviso de un campo obligatorio lo dibuja el servidor y se queda quieto: se
   envía el formulario en blanco, salen los mensajes, se rellenan los campos y
   los mensajes siguen ahí hasta el siguiente envío. Leído desde fuera eso dice
   que lo que acabas de escribir tampoco vale.

   Así que el aviso se retira en cuanto el campo deja de estar vacío. No es
   validar en el navegador —quien decide sigue siendo el servidor al enviar—,
   es dejar de afirmar algo que ya no es cierto. */
(function () {
    'use strict';

    function tieneValor(campo) {
        if (campo.type === 'checkbox' || campo.type === 'radio') {
            return campo.checked;
        }
        if (campo.type === 'file') {
            return campo.files && campo.files.length > 0;
        }
        return (campo.value || '').trim() !== '';
    }

    function repasar(campo) {
        if (!campo.classList || !campo.classList.contains('is-invalid')) { return; }
        if (!tieneValor(campo)) { return; }

        campo.classList.remove('is-invalid');
        // Si no, un lector de pantalla seguiría anunciándolo como erróneo.
        campo.removeAttribute('aria-invalid');

        /* Quitar «is-invalid» basta para los mensajes normales, que Bootstrap
           solo muestra junto a un campo inválido. No para los de la zona de
           arrastre: llevan «d-block», que es «display: block !important», y
           contra eso ni el atributo «hidden» ni un estilo en línea sin
           «important» pueden nada. */
        var padre = campo.parentNode;
        if (!padre) { return; }
        Array.prototype.forEach.call(
            padre.querySelectorAll('.invalid-feedback'),
            function (aviso) {
                aviso.style.setProperty('display', 'none', 'important');
            },
        );
    }

    /* Delegado en el documento: los formularios se dibujan enteros en el
       servidor, pero la zona de arrastre añade campos después, y engancharlos
       uno a uno dejaría fuera los que aún no existen. */
    ['input', 'change'].forEach(function (evento) {
        document.addEventListener(evento, function (e) {
            if (e.target) { repasar(e.target); }
        }, true);
    });
})();
