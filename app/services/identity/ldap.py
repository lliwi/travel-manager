"""Authentication against a corporate directory (Active Directory or LDAP).

This is the phase 2 implementation the seam in :mod:`app.services.identity.base`
was built for. Nothing about the user model, the foreign keys or the
authorisation rules changes: every relationship already hangs off the immutable
``users.id``, and a directory account is a row like any other, distinguished by
``identity_provider`` and ``external_id``.

**It does not replace local accounts.** Both work at the same time, and each
account knows which directory owns it. Turning this on disables nothing: an
administrator who can no longer reach the directory can still log in locally,
which is the difference between a bad afternoon and being locked out of the
application that manages the trips.

**Authorisation stays ours.** The directory says who somebody is; it does not
say what they may do. A provisioned account gets the plain ``usuario`` role and
an administrator grants anything beyond that, exactly as for a local account.
``role_group_mappings`` can automate it later, and while that table is empty a
manually granted role is never taken away by a later login.

The configuration lives in Ajustes rather than the environment, for the reason
the AI provider keys already follow: a credential in an env file is a
credential in plain text, and changing which OU people come from is an
administrative decision, not a redeploy.
"""

import logging

from app.services.identity.base import AuthResult, IdentityProvider, IdentityRecord

logger = logging.getLogger(__name__)

#: What we read about a person. Deliberately short: this application needs a
#: name, a way to write to them and their groups, and nothing else about them
#: belongs in a travel system.
ATRIBUTOS = [
    'cn', 'displayName', 'givenName', 'sn', 'mail', 'telephoneNumber',
    'title', 'department', 'departmentNumber', 'memberOf', 'objectGUID',
    'entryUUID', 'userAccountControl',
]

#: Object classes that mean «this path is a group, not a container».
CLASES_DE_GRUPO = {'group', 'groupofnames', 'groupofuniquenames', 'posixgroup'}

TIMEOUT = 8


class DirectorioNoDisponible(Exception):
    """The directory could not be reached or refused our own credentials."""


def _conf():
    from app.services import settings_service

    return {
        'habilitado': settings_service.get_bool('LDAP_HABILITADO', False),
        'servidor': settings_service.get('LDAP_SERVIDOR', ''),
        'puerto': settings_service.get_int('LDAP_PUERTO', 389),
        'ssl': settings_service.get_bool('LDAP_SSL', False),
        'starttls': settings_service.get_bool('LDAP_STARTTLS', False),
        'usuario': settings_service.get('LDAP_USUARIO', ''),
        'contrasena': settings_service.get('LDAP_CONTRASENA', ''),
        'ruta': (settings_service.get('LDAP_RUTA', '') or '').strip(),
        'atributo': settings_service.get('LDAP_ATRIBUTO_USUARIO', 'sAMAccountName'),
    }


def esta_configurado():
    """True when an administrator switched the directory on and described it.

    All of it, not some: a half-filled form would fail on every login attempt
    with a message about the directory, which is a confusing way to learn that
    somebody saved the page early.
    """
    conf = _conf()
    return bool(
        conf['habilitado'] and conf['servidor'] and conf['ruta']
        and conf['usuario'] and conf['contrasena']
    )


def _raiz_de(dn):
    """The domain root implied by a DN: the ``DC=`` components of it.

    Searching for the members of a group means searching the whole domain and
    filtering by membership, and the domain is not configured separately --
    it is already in the path an administrator typed.
    """
    partes = [p.strip() for p in str(dn).split(',')]
    dominio = [p for p in partes if p.lower().startswith('dc=')]
    return ','.join(dominio) if dominio else str(dn)


class LDAPIdentityProvider(IdentityProvider):
    """Reads a corporate directory and verifies credentials against it."""

    codigo = 'ldap'
    nombre = 'Directorio corporativo (AD/LDAP)'

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _servidor(self):
        from ldap3 import Server

        conf = _conf()
        if not conf['servidor']:
            raise DirectorioNoDisponible('No hay servidor de directorio configurado.')

        return Server(
            conf['servidor'],
            port=conf['puerto'],
            use_ssl=conf['ssl'],
            get_info='ALL',
            connect_timeout=TIMEOUT,
        )

    def _conectar(self, usuario=None, contrasena=None):
        """Bind, as the service account by default or as somebody being checked.

        Raises rather than returning None: every caller needs a connection, and
        a None that has to be checked at four call sites is a None that will be
        forgotten at one of them.
        """
        from ldap3 import Connection
        from ldap3.core.exceptions import LDAPException

        conf = _conf()
        usuario = usuario if usuario is not None else conf['usuario']
        contrasena = contrasena if contrasena is not None else conf['contrasena']

        try:
            conexion = Connection(
                self._servidor(),
                user=usuario,
                password=contrasena,
                auto_bind=False,
                receive_timeout=TIMEOUT,
            )
            if conf['starttls'] and not conf['ssl']:
                conexion.open()
                conexion.start_tls()
            if not conexion.bind():
                return None
            return conexion
        except LDAPException as exc:
            logger.warning(
                'No se pudo contactar con el directorio: %s', type(exc).__name__
            )
            raise DirectorioNoDisponible(
                'El directorio no responde o rechaza la conexión.'
            ) from None

    # ------------------------------------------------------------------
    # Where people are
    # ------------------------------------------------------------------
    def _alcance(self, conexion):
        """Where to look, and what still has to be checked afterwards.

        An administrator types either an organisational unit or a group and
        should not have to say which: the directory already knows. A container
        is searched inside. A group cannot be, because its members live
        wherever they live -- so the search widens to the domain and membership
        becomes a separate question, answered by :meth:`_es_miembro`.

        Not a ``memberOf`` filter, which is what this did first and why it
        found nobody: ``memberOf`` is an overlay that OpenLDAP does not enable
        by default, and AD's nested-group matching rule is an AD extension no
        other directory implements. Both produce an empty result rather than an
        error, which reads as «that group is empty» and sends somebody looking
        in the wrong place. Reading the group's own members works everywhere.

        Returns:
            ``(base, grupo_dn)``. ``grupo_dn`` is None when the path is a
            container and membership does not need checking.
        """
        conf = _conf()
        ruta = conf['ruta']

        try:
            conexion.search(
                search_base=ruta,
                search_filter='(objectClass=*)',
                search_scope='BASE',
                attributes=['objectClass'],
            )
        except Exception:  # noqa: BLE001 - a bad path is a configuration error
            logger.warning('La ruta del directorio no se pudo leer: %s', ruta)
            return ruta, None

        if not conexion.entries:
            return ruta, None

        clases = {
            str(c).lower()
            for c in (conexion.entries[0].objectClass.values or [])
        }
        if clases & CLASES_DE_GRUPO:
            return _raiz_de(ruta), ruta
        return ruta, None

    def _miembros(self, conexion, grupo_dn):
        """The DNs a group lists, whatever the directory calls that attribute."""
        conexion.search(
            search_base=grupo_dn,
            search_filter='(objectClass=*)',
            search_scope='BASE',
            attributes=['member', 'uniqueMember', 'memberUid'],
        )
        if not conexion.entries:
            return set()

        entrada = conexion.entries[0]
        dns = set()
        for atributo in ('member', 'uniqueMember'):
            valores = getattr(entrada, atributo, None)
            for valor in (valores.values if valores else []) or []:
                dns.add(str(valor).strip().lower())
        return dns

    def _es_miembro(self, conexion, entrada, grupo_dn):
        """Whether this person belongs to the configured group.

        Asked from both ends because directories disagree about which end holds
        the answer: the group lists its members, and where the ``memberOf``
        overlay is enabled the person lists their groups. Either is enough.
        """
        if not grupo_dn:
            return True

        propio = str(entrada.entry_dn).strip().lower()
        objetivo = str(grupo_dn).strip().lower()

        suyos = {
            str(g).strip().lower()
            for g in (getattr(entrada, 'memberOf', None) or [])
        }
        if objetivo in suyos:
            return True

        return propio in self._miembros(conexion, grupo_dn)

    def _filtro(self, conexion, valor, atributo=None):
        """Search base and filter for one person, plus the group to verify."""
        conf = _conf()
        atributo = atributo or conf['atributo']
        base, grupo = self._alcance(conexion)
        return base, f'(&(objectClass=person)({atributo}={_escapar(valor)}))', grupo

    def _atributos(self, conexion):
        """Only the attributes this particular directory actually has.

        The list above is the union of two dialects: Active Directory calls a
        department ``department`` and identifies a row by ``objectGUID``, while
        OpenLDAP calls them ``departmentNumber`` and ``entryUUID``. Asking a
        server for an attribute its schema does not define is not ignored --
        ldap3 refuses the whole search with «invalid attribute type», so one
        name that does not belong costs every result.

        Filtering against the schema is what lets one connector serve both.
        When the schema cannot be read we ask for everything and let the server
        return what it likes, which is worse but never empty.
        """
        # The configured login attribute always goes in, whatever it is called
        # here. Leaving it out is what made a provisioned account's username
        # fall back to the display name: «Ana López» cannot be typed into a
        # login form, and the next login created nothing because the search
        # still matched -- one person, two identities, no error anywhere.
        conf = _conf()
        pedidos = list(ATRIBUTOS)
        if conf['atributo'] and conf['atributo'].lower() not in {
            a.lower() for a in pedidos
        }:
            pedidos.insert(0, conf['atributo'])

        esquema = getattr(conexion.server, 'schema', None)
        conocidos = getattr(esquema, 'attribute_types', None)
        if not conocidos:
            return pedidos

        disponibles = {str(nombre).lower() for nombre in conocidos}
        return [a for a in pedidos if a.lower() in disponibles] or pedidos

    def _registro(self, entrada):
        """Turn a directory entry into an :class:`IdentityRecord`."""
        def _uno(nombre):
            return _texto(entrada, nombre)

        conf = _conf()
        username = _uno(conf['atributo']) or _uno('cn')

        # objectGUID in AD, entryUUID in OpenLDAP, and the DN as the last
        # resort. It has to survive a rename: a person who marries and changes
        # their account name must not become a second account with an empty
        # history.
        external_id = _uno('objectGUID') or _uno('entryUUID') or str(entrada.entry_dn)

        grupos = _lista(entrada, 'memberOf')

        nombre = _uno('givenName')
        apellidos = _uno('sn')
        if not nombre:
            nombre = _uno('displayName') or _uno('cn') or username

        return IdentityRecord(
            external_id=external_id,
            username=username,
            email=_uno('mail'),
            nombre=nombre,
            apellidos=apellidos,
            telefono=_uno('telephoneNumber'),
            puesto=_uno('title'),
            departamento=_uno('department') or _uno('departmentNumber'),
            grupos=tuple(grupos),
            deshabilitado=_deshabilitado(_uno('userAccountControl')),
            atributos={'dn': str(entrada.entry_dn)},
        )

    def _buscar(self, conexion, valor, atributo=None):
        """Find one person inside the configured scope, or None.

        «Inside the scope» includes the membership check, so a caller cannot
        forget it: somebody outside the configured group is not found at all,
        rather than found and then allowed through by a check nobody wrote.
        """
        base, filtro, grupo = self._filtro(conexion, valor, atributo)
        conexion.search(
            search_base=base,
            search_filter=filtro,
            attributes=self._atributos(conexion),
            size_limit=2,
        )
        if not conexion.entries:
            return None

        entrada = conexion.entries[0]
        if not self._es_miembro(conexion, entrada, grupo):
            return None
        return entrada

    # ------------------------------------------------------------------
    # The contract
    # ------------------------------------------------------------------
    def authenticate(self, username, credential):
        """Verify a credential against the directory.

        Two binds, and the second one is the point: we look the person up with
        the service account, then bind *as them* with what they typed. Comparing
        a password ourselves would mean the directory had handed us one, and a
        directory that hands out passwords is not one worth trusting.
        """
        if not esta_configurado():
            return AuthResult.fail('directorio_no_configurado')
        if not username or not credential:
            # An empty password is an anonymous bind, which many directories
            # accept -- and would let anybody in as anybody.
            return AuthResult.fail('credenciales_vacias')

        try:
            servicio = self._conectar()
        except DirectorioNoDisponible:
            return AuthResult.fail('directorio_no_disponible')

        if servicio is None:
            logger.error(
                'El directorio rechazó el usuario de consulta configurado.'
            )
            return AuthResult.fail('bind_de_servicio_rechazado')

        try:
            entrada = self._buscar(servicio, username)
            if entrada is None:
                return AuthResult.fail('desconocido_en_directorio')

            record = self._registro(entrada)
            if record.deshabilitado:
                return AuthResult.fail(
                    'cuenta_deshabilitada_en_directorio',
                    record=record, cuenta_inactiva=True,
                )

            try:
                propia = self._conectar(str(entrada.entry_dn), credential)
            except DirectorioNoDisponible:
                return AuthResult.fail('directorio_no_disponible', record=record)

            if propia is None:
                return AuthResult.fail('credencial_incorrecta', record=record)

            propia.unbind()
            return AuthResult.ok(record)
        finally:
            servicio.unbind()

    def get_user(self, external_id):
        if not esta_configurado():
            return None

        try:
            conexion = self._conectar()
        except DirectorioNoDisponible:
            return None
        if conexion is None:
            return None

        try:
            for atributo in ('objectGUID', 'entryUUID'):
                entrada = self._buscar(conexion, external_id, atributo=atributo)
                if entrada is not None:
                    return self._registro(entrada)
            return None
        finally:
            conexion.unbind()

    def find_by_username(self, username):
        if not esta_configurado():
            return None

        try:
            conexion = self._conectar()
        except DirectorioNoDisponible:
            return None
        if conexion is None:
            return None

        try:
            entrada = self._buscar(conexion, username)
            return self._registro(entrada) if entrada is not None else None
        finally:
            conexion.unbind()

    def search(self, query, limit=25):
        """People in the configured scope whose name or login matches."""
        if not esta_configurado() or not query:
            return []

        try:
            conexion = self._conectar()
        except DirectorioNoDisponible:
            return []
        if conexion is None:
            return []

        try:
            conf = _conf()
            base, grupo = self._alcance(conexion)
            texto = _escapar(query)
            conexion.search(
                search_base=base,
                search_filter=(
                    f'(&(objectClass=person)(|({conf["atributo"]}=*{texto}*)'
                    f'(cn=*{texto}*)(mail=*{texto}*)))'
                ),
                attributes=self._atributos(conexion),
                size_limit=limit,
            )
            return [
                self._registro(e) for e in conexion.entries
                if self._es_miembro(conexion, e, grupo)
            ]
        finally:
            conexion.unbind()

    def supports_provisioning(self):
        """Yes: an account is created on first successful login.

        With the plain ``usuario`` role, which is the whole point. The directory
        proves who somebody is; what they may do here is granted here.
        """
        return True

    def supports_password_change(self):
        """No. A corporate password is changed where the corporation keeps it."""
        return False

    def health_check(self):
        """Report whether the directory answers and the path resolves.

        Used by the «probar conexión» button, so the failure it returns is the
        real reason rather than a generic one: this message goes to an
        administrator fixing their own configuration, not to a stranger at a
        login form.
        """
        if not esta_configurado():
            return False, 'Faltan datos de configuración del directorio.'

        try:
            conexion = self._conectar()
        except DirectorioNoDisponible as exc:
            return False, str(exc)

        if conexion is None:
            return False, 'El directorio rechazó el usuario de consulta.'

        try:
            base, grupo = self._alcance(conexion)
            conexion.search(
                search_base=base,
                search_filter='(objectClass=person)',
                attributes=self._atributos(conexion),
                size_limit=200,
            )
            dentro = [
                e for e in conexion.entries
                if self._es_miembro(conexion, e, grupo)
            ]
            if not dentro:
                return False, (
                    'La conexión funciona, pero no se ve ninguna persona en la '
                    'ruta indicada. Revise la OU o el grupo, y el atributo del '
                    'nombre de usuario.'
                )

            donde = 'en el grupo' if grupo else 'en la ruta'
            return True, (
                f'Conexión correcta. Se ven {len(dentro)} personas {donde}. '
                + _resumen_de_atributos(_conf()['atributo'], dentro[0])
            )
        except Exception as exc:  # noqa: BLE001
            return False, f'La ruta indicada no se pudo leer: {type(exc).__name__}'
        finally:
            conexion.unbind()


#: Which directory attribute fills which field of an account, in the order
#: somebody reads them. The login attribute is separate: it is configured.
CAMPOS = (
    ('nombre', ('givenName', 'displayName', 'cn')),
    ('apellidos', ('sn',)),
    ('correo', ('mail',)),
    ('teléfono', ('telephoneNumber',)),
    ('puesto', ('title',)),
    ('departamento', ('department', 'departmentNumber')),
    ('grupos', ('memberOf',)),
)


def _texto(entrada, nombre):
    """One attribute as text, or None when the directory has no value for it.

    Read through ``.value`` rather than ``str()`` on the attribute: an
    attribute that was asked for and does not exist still comes back as an
    empty one, and stringifying that gives «[]» -- which is not empty, so it
    passes every «is there a value here» check and ends up stored as somebody's
    email address.
    """
    bruto = getattr(entrada, nombre, None)
    if bruto is None:
        return None

    valor = getattr(bruto, 'value', bruto)
    if valor is None:
        return None
    if isinstance(valor, (list, tuple)):
        valor = valor[0] if valor else None
        if valor is None:
            return None

    texto = str(valor).strip()
    return texto or None


def _lista(entrada, nombre):
    """One multi-valued attribute as a list of strings."""
    bruto = getattr(entrada, nombre, None)
    valores = getattr(bruto, 'values', None) if bruto is not None else None
    return [str(v).strip() for v in (valores or []) if str(v).strip()]


def _resumen_de_atributos(atributo_login, ejemplo):
    """Say which attributes are being used, and which came back empty.

    The count alone says the connection works, which is the easy half. What an
    administrator actually needs to know is whether they picked the right login
    attribute -- ``sAMAccountName`` and ``uid`` are both plausible and only one
    of them is right -- and what will be missing from every account before
    anybody logs in and finds out. An empty «correo» is a trip nobody can be
    notified about.
    """
    def _valor(nombres):
        for nombre in nombres:
            texto = _texto(ejemplo, nombre)
            if texto:
                return nombre, texto
        return None, None

    login = _texto(ejemplo, atributo_login) or ''
    partes = [
        f'Se identifican por «{atributo_login}»'
        + (f' (por ejemplo, «{login}»).' if login else
           ', que no trae valor en esta ruta: revíselo.')
    ]

    usados, vacios = [], []
    for etiqueta, nombres in CAMPOS:
        nombre, valor = _valor(nombres)
        if valor:
            usados.append(f'{etiqueta} ({nombre})')
        else:
            vacios.append(etiqueta)

    if usados:
        partes.append('Se leen además: ' + ', '.join(usados) + '.')
    if vacios:
        partes.append(
            'Sin valor en la primera persona encontrada: ' + ', '.join(vacios) + '.'
        )
    return ' '.join(partes)


def _escapar(valor):
    """Escape a value going into a search filter (RFC 4515).

    Without this, a username of ``*`` matches everybody and the first person in
    the directory gets looked up instead -- the LDAP shape of an injection.
    """
    texto = str(valor or '')
    for crudo, escapado in (
        ('\\', '\\5c'), ('*', '\\2a'), ('(', '\\28'), (')', '\\29'), ('\0', '\\00'),
    ):
        texto = texto.replace(crudo, escapado)
    return texto


def _deshabilitado(user_account_control):
    """Whether AD's flags say the account is disabled (bit 2)."""
    if not user_account_control:
        return False
    try:
        return bool(int(user_account_control) & 0x0002)
    except (TypeError, ValueError):
        return False
