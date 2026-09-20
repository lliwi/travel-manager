"""Cache-busting for static assets.

A browser that has already fetched ``/static/js/main.js`` will keep using its
copy until the URL changes: Nginx serves the directory with
``expires 7d; Cache-Control: immutable``, so a deploy that changes a script is
invisible to everyone who visited in the previous week, and the dev server's
revalidation is not reliable enough to catch it either. The failure is silent
and looks like a broken feature rather than a stale file -- a drop zone whose
JavaScript never loaded simply lets the browser open the dropped document.

Appending the file's modification time makes the URL change exactly when the
file does, which is what ``immutable`` promises and what makes a long expiry
safe instead of dangerous.
"""

import os

#: filename -> version token. Static files do not change while the process
#: runs in production; in development the reloader restarts it on every edit,
#: so the cache never outlives the file it describes.
_versions = {}


def version_for(app, filename):
    """Return the version token for a static file, or None if it has none.

    A missing file is not an error here: the URL is built anyway and the
    request 404s as it would without a token, which is a far clearer symptom
    than an exception raised while rendering an unrelated page.
    """
    if filename in _versions:
        return _versions[filename]

    try:
        path = os.path.join(app.static_folder, filename)
        token = str(int(os.stat(path).st_mtime))
    except (OSError, TypeError, ValueError):
        token = None

    _versions[filename] = token
    return token


def register(app):
    """Add a version token to every ``url_for('static', ...)`` the app builds.

    ``url_defaults`` is the hook that sees them all, including the ones
    blueprints and macros build, so no template has to remember to ask.
    """

    @app.url_defaults
    def _add_version(endpoint, values):
        if endpoint != 'static' or 'filename' not in values or 'v' in values:
            return

        token = version_for(app, values['filename'])
        if token:
            values['v'] = token

    # Development edits the files under a running reloader, and a token cached
    # from before the edit would defeat the whole point.
    if app.config.get('DEBUG'):
        @app.before_request
        def _forget_versions():
            _versions.clear()
