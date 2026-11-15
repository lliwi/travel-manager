"""The hotels the organisation recommends, city by city.

A manager keeps the list. Where a city has any, they are what the planner
offers for lodging *instead of* the search connector's: a hotel with an agreed
rate is the organisation's decision, and a row from Google Hotels is only what
was there that day. Offering both side by side would put the decision back on
whoever is planning, which is what the list exists to take off them.

Two things follow from that:

- Cities are matched by the catalogue's spelling, never by what was typed, so
  a hotel filed under «Londres» is found by somebody planning to «London» or
  «LHR».
- The rate here is a reference the manager typed, not a quote. It is carried
  into the itinerary as that and labelled as that.
"""
import unicodedata
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.catalog import HotelRecomendado
from app.models.enums import AuditResourceType
from app.services import audit_service
from app.utils.errors import ResourceNotFound, ValidationError

CAMPOS = ('nombre', 'ciudad', 'pais', 'zona', 'direccion', 'precio_noche',
          'moneda', 'enlace', 'notas', 'activo')


def clave_de_ciudad(texto):
    """«São Paulo » and «sao paulo» as one key."""
    plano = unicodedata.normalize('NFKD', str(texto or ''))
    plano = ''.join(c for c in plano if not unicodedata.combining(c))
    return ' '.join(plano.lower().split())


def _ciudad_canonica(ciudad, pais=None):
    """The catalogue's spelling of a city, or what was typed if it knows none.

    Stricter than ``ciudad_del_catalogo`` on purpose. That one, failing
    everything else, accepts a place whose *name contains* the text -- right
    for reading «London Heathrow» off a ticket, and wrong here: «Lyon» is
    contained in «Paris Gare de Lyon», so a hotel in Lyon was filed under
    París and offered to whoever travelled there. Only what identifies a city
    counts: its code, its own name, or a known alias of it.
    """
    from app.models.catalog import Location
    from app.services.normalization_service import _por_alias

    texto = ' '.join(str(ciudad or '').split())
    if not texto:
        return texto, pais

    criterios = [Location.ciudad.ilike(texto), _por_alias(texto)]
    if len(texto) <= 5:
        criterios.insert(0, Location.codigo == texto.upper())

    for criterio in criterios:
        if criterio is None:
            continue
        filas = Location.query.filter(
            Location.activo.is_(True), Location.ciudad.isnot(None), criterio,
        ).all()
        ciudades = {f.ciudad for f in filas}
        # Two cities under one spelling is a question for a person; the text
        # is kept as typed rather than settled by whichever row came first.
        if len(ciudades) == 1:
            paises = {f.pais_codigo for f in filas if f.pais_codigo}
            return ciudades.pop(), (pais or (paises.pop() if len(paises) == 1 else None))
    return texto, pais


def listar(incluir_inactivos=True):
    """Every recommended hotel, by city and name."""
    query = HotelRecomendado.query.filter(HotelRecomendado.is_deleted.is_(False))
    if not incluir_inactivos:
        query = query.filter(HotelRecomendado.activo.is_(True))
    return query.order_by(HotelRecomendado.ciudad, HotelRecomendado.nombre).all()


def obtener(hotel_id):
    import uuid

    try:
        hotel = db.session.get(HotelRecomendado, uuid.UUID(str(hotel_id)))
    except (ValueError, TypeError):
        hotel = None
    if hotel is None or hotel.is_deleted:
        raise ResourceNotFound('El hotel indicado no existe.')
    return hotel


def _validar(campos):
    """Clean the fields a form or a caller sent; refuse what makes no sense."""
    limpio = {}
    for campo in ('nombre', 'ciudad', 'zona', 'direccion', 'enlace', 'notas'):
        if campo in campos:
            limpio[campo] = str(campos[campo] or '').strip() or None
    if 'pais' in campos:
        limpio['pais'] = (str(campos['pais'] or '').strip().upper()[:2] or None)
    if 'moneda' in campos:
        limpio['moneda'] = (str(campos['moneda'] or '').strip().upper()[:3] or None)
    if 'activo' in campos:
        limpio['activo'] = bool(campos['activo'])

    if 'precio_noche' in campos:
        crudo = campos['precio_noche']
        if crudo in (None, ''):
            limpio['precio_noche'] = None
        else:
            try:
                precio = Decimal(str(crudo))
            except (InvalidOperation, ValueError) as exc:
                raise ValidationError('El precio por noche no es un número.') from exc
            if precio < 0:
                raise ValidationError('El precio por noche no puede ser negativo.')
            limpio['precio_noche'] = precio

    if 'nombre' in limpio and not limpio['nombre']:
        raise ValidationError('Indique el nombre del hotel.')
    if 'ciudad' in limpio and not limpio['ciudad']:
        raise ValidationError('Indique la ciudad del hotel.')
    enlace = limpio.get('enlace')
    if enlace and not enlace.lower().startswith(('http://', 'https://')):
        # It becomes a link on the planner; «javascript:» must not.
        raise ValidationError('El enlace tiene que empezar por http:// o https://.')
    return limpio


def _aplicar(hotel, limpio, actor):
    for campo, valor in limpio.items():
        setattr(hotel, campo, valor)
    if 'ciudad' in limpio or 'pais' in limpio:
        hotel.ciudad, hotel.pais = _ciudad_canonica(hotel.ciudad, hotel.pais)
        hotel.ciudad_clave = clave_de_ciudad(hotel.ciudad)
    # A price in no currency is a number nobody can add to anything.
    if hotel.precio_noche is not None and not hotel.moneda:
        raise ValidationError('Indique la moneda del precio por noche.')
    hotel.actualizado_por_id = getattr(actor, 'id', None)


def crear(actor, **campos):
    limpio = _validar({'activo': True, **campos})
    for obligatorio in ('nombre', 'ciudad'):
        if not limpio.get(obligatorio):
            raise ValidationError(f'Indique {"el nombre" if obligatorio == "nombre" else "la ciudad"} del hotel.')

    hotel = HotelRecomendado(nombre=limpio['nombre'], ciudad=limpio['ciudad'],
                             ciudad_clave=clave_de_ciudad(limpio['ciudad']))
    _aplicar(hotel, limpio, actor)
    db.session.add(hotel)
    db.session.commit()
    _auditar('recommended_hotel.created', hotel, actor)
    return hotel


def actualizar(actor, hotel, **campos):
    _aplicar(hotel, _validar(campos), actor)
    db.session.commit()
    _auditar('recommended_hotel.updated', hotel, actor)
    return hotel


def eliminar(actor, hotel):
    hotel.soft_delete(actor)
    db.session.commit()
    _auditar('recommended_hotel.deleted', hotel, actor)
    return hotel


def _auditar(accion, hotel, actor):
    audit_service.record(
        accion,
        recurso_tipo=AuditResourceType.CONFIGURACION,
        recurso_id=str(hotel.id),
        actor=actor,
        metadatos={'nombre': hotel.nombre, 'ciudad': hotel.ciudad},
    )


# ======================================================================
# What the planner asks
# ======================================================================
def en(destino):
    """The active recommended hotels in the place somebody typed."""
    if not destino:
        return []
    ciudad, _pais = _ciudad_canonica(destino)
    claves = {clave_de_ciudad(ciudad), clave_de_ciudad(destino)} - {''}
    if not claves:
        return []
    return (
        HotelRecomendado.query.filter(
            HotelRecomendado.is_deleted.is_(False),
            HotelRecomendado.activo.is_(True),
            HotelRecomendado.ciudad_clave.in_(claves),
        )
        .order_by(HotelRecomendado.nombre).all()
    )


def como_opciones(hoteles, entrada=None, salida=None):
    """Recommended hotels in the shape the planner already draws and saves.

    The same keys as a connector row, so the screen, the selection that is
    posted back and ``trip_from_search_service`` need no second code path --
    plus ``recomendado``, which is what lets each of them say where the row
    and its price came from.
    """
    noches = None
    if entrada and salida:
        try:
            noches = (salida - entrada).days
        except TypeError:
            noches = None
        if noches is not None and noches <= 0:
            noches = None

    opciones = []
    for hotel in hoteles:
        precio = hotel.precio_noche
        total = (precio * noches) if (precio is not None and noches) else None
        opciones.append({
            'nombre': hotel.nombre,
            'ciudad': hotel.ciudad,
            'pais': hotel.pais,
            'zona': hotel.zona,
            'direccion': hotel.direccion,
            'notas': hotel.notas,
            'precio_noche': _importe(precio, hotel.moneda),
            'precio_total': _importe(total, hotel.moneda),
            'precio_total_valor': float(total) if total is not None else None,
            'moneda': hotel.moneda,
            'enlace': hotel.enlace,
            'recomendado': True,
        })
    return opciones


def _importe(valor, moneda):
    if valor is None:
        return None
    cifra = f'{valor:,.2f}'.replace(',', ' ').replace('.', ',').replace(' ', '.')
    if cifra.endswith(',00'):
        cifra = cifra[:-3]
    return f'{cifra} {moneda or ""}'.strip()
