"""The base every form of the application inherits from.

It exists for one thing: WTForms' own messages -- «This field is required.»,
«Not a valid integer value.» -- in Spanish, like the rest of the interface.
Flask-WTF would translate them through Flask-Babel, which this application
does not use, so ``WTF_I18N_ENABLED`` is off and WTForms is asked directly
for the translation it ships with.
"""
from flask_wtf import FlaskForm


class Formulario(FlaskForm):
    class Meta:
        locales = ('es_ES', 'es')
