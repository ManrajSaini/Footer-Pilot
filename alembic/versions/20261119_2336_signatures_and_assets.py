"""signatures and assets

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('assets',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('storage_key', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('content_type', sa.String(length=50), nullable=False),
    sa.Column('bytes', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('logo', 'banner')", name=op.f('ck_assets_kind_valid')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_assets_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assets'))
    )
    op.create_index(op.f('ix_assets_org_id'), 'assets', ['org_id'], unique=False)
    op.create_table('signatures',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('draft_design', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('published_design', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_signatures_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_signatures')),
    sa.UniqueConstraint('org_id', name=op.f('uq_signatures_org_id'))
    )


def downgrade() -> None:
    op.drop_table('signatures')
    op.drop_index(op.f('ix_assets_org_id'), table_name='assets')
    op.drop_table('assets')
