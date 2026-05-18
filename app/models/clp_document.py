from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.extensions import db
from app.models.user import User  # noqa: F401


class CLPDocument(db.Model):
    __tablename__ = "clp_documents"

    id = db.Column(db.BigInteger, primary_key=True)
    owner_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    department = db.Column(db.Text, nullable=False)
    title = db.Column(db.Text, nullable=False)
    status = db.Column(db.Text, nullable=False, default="draft")
    editor_json = db.Column(JSONB, nullable=False, default=dict)
    tiptap_json = db.Column(JSONB, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )
