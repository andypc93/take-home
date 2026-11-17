"""Image owner: which API key uploaded an image (stored as a hash of the key).

Revision ID: 0003_image_owner
Revises: 0002_rate_limit_counters
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_image_owner"
down_revision = "0002_rate_limit_counters"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable: existing images, and images uploaded while API keys are off, have no owner.
    op.add_column("uploaded_images", sa.Column("owner", sa.String(64), nullable=True))


def downgrade():
    with op.batch_alter_table("uploaded_images") as batch:
        batch.drop_column("owner")
