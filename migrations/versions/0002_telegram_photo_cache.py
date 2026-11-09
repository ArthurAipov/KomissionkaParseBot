"""Persist Telegram photo references, scoped to the bot and original image URL."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_photos",
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("bot_id", "image_url"),
    )


def downgrade() -> None:
    op.drop_table("telegram_photos")
