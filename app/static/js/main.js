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
    });

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

    /** Ask before submitting anything marked destructive. */
    function confirmDestructiveForms() {
        document.querySelectorAll('form[data-confirm]').forEach(function (form) {
            form.addEventListener('submit', function (event) {
                if (!window.confirm(form.dataset.confirm)) {
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
