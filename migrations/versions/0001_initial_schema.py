"""Initial schema: uploaded images, chat history, stream sessions.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "uploaded_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("uploaded_at", sa.Float, nullable=False),
        sa.Column("analysis", sa.JSON, nullable=False),
    )
    op.create_index("ix_uploaded_images_uploaded_at", "uploaded_images", ["uploaded_at"])

    op.create_table(
        "chat_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("image_id", sa.String(36),
                  sa.ForeignKey("uploaded_images.id", ondelete="CASCADE"), nullable=False),
        sa.Column("completion_id", sa.String(64), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("completion_id", name="uq_chat_history_completion_id"),
    )
    op.create_index("ix_chat_history_image_id_id", "chat_history", ["image_id", "id"])

    op.create_table(
        "stream_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("image_id", sa.String(36),
                  sa.ForeignKey("uploaded_images.id", ondelete="CASCADE"), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("history", sa.JSON, nullable=False),
        sa.Column("created", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("ix_stream_sessions_expires_at", "stream_sessions", ["expires_at"])


def downgrade():
    op.drop_table("stream_sessions")
    op.drop_table("chat_history")
    op.drop_table("uploaded_images")
