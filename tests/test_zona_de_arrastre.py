"""Drag and drop wherever a document can be attached.

Progressive enhancement, and the word matters: the file input is still there
and is still what submits. With JavaScript off, in a browser without
DataTransfer, or for somebody driving the page from the keyboard, the field
behaves like any other file field. What the script adds is a bigger target and
the list of what is about to be sent -- the part people actually miss, because
a file input shows one name and then says nothing about the rest.
"""
import io

import pytest

from app.models.document import Document


@pytest.mark.integration
class TestDondeSePuedenAdjuntar:
    def _upload(self, client, trip):
        return client.get(f'/documents/viaje/{trip.id}/subir').get_data(as_text=True)

    def test_la_pantalla_de_subir_tiene_zona(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = self._upload(client, trip)

        assert 'js-dropzone' in html
        assert 'Arrastre el documento aquí' in html

    def test_el_alta_de_viaje_tambien(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)

        assert 'js-dropzone' in html
        assert 'Arrastre los documentos aquí' in html

    def test_donde_se_admiten_varios_lo_dice(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)

        assert 'multiple' in html


@pytest.mark.integration
class TestElCampoSigueSiendoElCampo:
    """The enhancement must not become the mechanism.

    If the drop zone replaced the input rather than wrapping it, the form would
    stop working for anybody without the script -- and nobody would find out
    until somebody could not attach a booking.
    """

    def test_el_input_de_archivo_sigue_dentro(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                f'/documents/viaje/{trip.id}/subir'
            ).get_data(as_text=True)

        assert 'type="file"' in html
        assert 'name="archivo"' in html

    def test_conserva_el_nombre_del_campo(self, as_user, gestor, seeded):
        """Renaming it would break the form silently: the POST would arrive
        with nothing attached and the error would be «seleccione un archivo»."""
        with as_user(gestor) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)

        assert 'name="documentos"' in html

    def test_se_sigue_pudiendo_subir(self, as_user, gestor, trip, booking_pdf):
        with as_user(gestor) as client:
            respuesta = client.post(
                f'/documents/viaje/{trip.id}/subir',
                data={
                    'tipo': 'reserva',
                    'trip_traveler_id': '',
                    'archivo': (io.BytesIO(booking_pdf), 'arrastrado.pdf'),
                },
                content_type='multipart/form-data',
                follow_redirects=True,
            )

        assert respuesta.status_code == 200
        assert Document.query.filter_by(nombre_original='arrastrado.pdf').first()

    def test_un_error_de_validacion_se_sigue_viendo(self, as_user, gestor, trip):
        """The message has to survive the wrapper, or a rejected file looks
        like a file that uploaded."""
        with as_user(gestor) as client:
            html = client.post(
                f'/documents/viaje/{trip.id}/subir',
                data={
                    'tipo': 'reserva', 'trip_traveler_id': '',
                    'archivo': (io.BytesIO(b'x'), 'prohibido.exe'),
                },
                content_type='multipart/form-data',
            ).get_data(as_text=True)

        assert 'invalid-feedback' in html
        assert 'Formato no admitido' in html


@pytest.mark.unit
class TestLoQueTraeLaMejora:
    def test_hay_estilos_para_la_zona(self):
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent
               / 'app' / 'static' / 'css' / 'main.css').read_text()

        assert '.dropzone' in css
        assert '.dropzone.is-dragging' in css

    def test_el_script_conserva_lo_ya_elegido(self):
        """Dropping a second document is adding it, not starting again."""
        from pathlib import Path

        js = (Path(__file__).resolve().parent.parent
              / 'app' / 'static' / 'js' / 'main.js').read_text()

        assert 'js-dropzone' in js
        assert 'input.files' in js
        assert 'DataTransfer' in js

    def test_el_script_no_hace_nada_sin_datatransfer(self):
        """An older browser keeps a working file field instead of a broken zone."""
        from pathlib import Path

        js = (Path(__file__).resolve().parent.parent
              / 'app' / 'static' / 'js' / 'main.js').read_text()

        assert "typeof DataTransfer === 'undefined'" in js


@pytest.mark.unit
class TestSoltarFueraDeLaZona:
    """A near miss must be harmless.

    Dropping a file anywhere a page does not handle makes the browser navigate
    to it, abandoning the form and everything typed into it. The zone is a
    modest target inside a long form, so missing it is the common case, not the
    exotic one.
    """

    def _js(self):
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent
                / 'app' / 'static' / 'js' / 'main.js').read_text()

    def test_el_documento_no_abre_al_fallar_el_blanco(self):
        js = self._js()

        assert "document.addEventListener(evento, function (e) {" in js
        assert "closest('.js-dropzone')" in js

    def test_se_ve_adonde_apuntar_antes_de_soltar(self):
        from pathlib import Path

        js = self._js()
        css = (Path(__file__).resolve().parent.parent
               / 'app' / 'static' / 'css' / 'main.css').read_text()

        assert 'hay-arrastre' in js
        assert '.hay-arrastre .dropzone' in css


@pytest.mark.integration
class TestElNavegadorRecibeElScriptNuevo:
    """The bug this guards against had no visible symptom in the code.

    The drop zone was correct and still opened the document, because browsers
    were running the ``main.js`` they had cached from before it existed. Nginx
    serves /static/ with ``immutable`` and a seven day expiry, so without a
    token in the URL a change to a script reaches nobody who visited recently.
    """

    def test_el_js_lleva_version_en_la_url(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                f'/documents/viaje/{trip.id}/subir'
            ).get_data(as_text=True)

        assert 'js/main.js?v=' in html

    def test_el_css_tambien(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                f'/documents/viaje/{trip.id}/subir'
            ).get_data(as_text=True)

        assert 'css/main.css?v=' in html

    def test_la_version_cambia_cuando_cambia_el_fichero(self, app, tmp_path):
        """Otherwise the token is decoration and the stale copy stays."""
        import os

        from app.utils import assets

        fichero = tmp_path / 'probe.js'
        fichero.write_text('uno')

        original = app.static_folder
        app.static_folder = str(tmp_path)
        try:
            assets._versions.clear()
            antes = assets.version_for(app, 'probe.js')

            fichero.write_text('dos')
            os.utime(fichero, (0, 0))
            assets._versions.clear()
            despues = assets.version_for(app, 'probe.js')
        finally:
            app.static_folder = original
            assets._versions.clear()

        assert antes and despues
        assert antes != despues

    def test_un_estatico_inexistente_no_rompe_la_pagina(self, app):
        """A missing file must 404 like any other, not raise while rendering."""
        from flask import url_for

        from app.utils import assets

        assets._versions.clear()
        with app.test_request_context():
            url = url_for('static', filename='no/existe.js')

        assert 'no/existe.js' in url
        assert '?v=' not in url
