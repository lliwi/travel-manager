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
