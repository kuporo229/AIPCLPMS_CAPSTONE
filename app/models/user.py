from sqlalchemy.dialects.postgresql import UUID

from app.extensions import db


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    id = db.Column(UUID(as_uuid=True), primary_key=True)
