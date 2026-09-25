"""Full backups and restores, made on demand from the panel.

A job row is the only thing a backup does not contain and a restore does not
replace: it is how the restore reports on itself, and a restore that wiped its
own progress would finish in silence.
"""
from app.extensions import db
from app.models.base import GUID, BaseModel, JSONBType, enum_check, enum_column
from app.models.enums import BackupJobState, BackupJobType


class BackupJob(BaseModel):
    __tablename__ = 'backup_jobs'
    __table_args__ = (
        enum_check('tipo', BackupJobType, 'backup_jobs'),
        enum_check('estado', BackupJobState, 'backup_jobs'),
    )

    tipo = enum_column(BackupJobType, nullable=False, index=True)
    estado = enum_column(BackupJobState, nullable=False,
                         default=BackupJobState.PENDIENTE, index=True)

    #: Where the ZIP sits in the document store while it can be downloaded,
    #: or where an uploaded one waits to be restored.
    objeto = db.Column(db.String(300))
    nombre_fichero = db.Column(db.String(200))
    tamano = db.Column(db.BigInteger)
    sha256 = db.Column(db.String(64))

    #: Rows per table, documents, application version -- what the manifest says.
    resumen = db.Column(JSONBType())
    progreso = db.Column(db.String(200))
    error = db.Column(db.String(500))

    iniciado_en = db.Column(db.DateTime(timezone=True))
    terminado_en = db.Column(db.DateTime(timezone=True))
    descargado_en = db.Column(db.DateTime(timezone=True))

    #: SET NULL: a restore replaces the users table, and the person who
    #: launched it may not exist in the copy being restored.
    lanzado_por_id = db.Column(
        GUID(), db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
    )
    #: Kept as text for the same reason: the name must survive the restore.
    lanzado_por_nombre = db.Column(db.String(200))

    lanzado_por = db.relationship('User', lazy='select')

    def __repr__(self):
        return f'<BackupJob {self.tipo} {self.estado}>'

    @property
    def activo(self):
        return self.estado in (BackupJobState.PENDIENTE, BackupJobState.EN_CURSO)

    @property
    def descargable(self):
        return (self.tipo is BackupJobType.COPIA
                and self.estado is BackupJobState.COMPLETADO and bool(self.objeto))

    def to_dict(self):
        return {
            'id': str(self.id),
            'tipo': str(self.tipo),
            'estado': str(self.estado),
            'estado_label': self.estado.label if self.estado else None,
            'activo': self.activo,
            'progreso': self.progreso,
            'error': self.error,
            'tamano': self.tamano,
            'resumen': self.resumen or {},
        }
