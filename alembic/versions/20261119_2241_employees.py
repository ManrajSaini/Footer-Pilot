"""employees

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    op.create_table('employees',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('google_user_id', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('first_name', sa.String(length=255), nullable=False),
    sa.Column('last_name', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=False),
    sa.Column('job_title', sa.String(length=255), nullable=False),
    sa.Column('department', sa.String(length=255), nullable=False),
    sa.Column('phone_work', sa.String(length=64), nullable=False),
    sa.Column('phone_mobile', sa.String(length=64), nullable=False),
    sa.Column('has_mailbox', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('photo_url', sa.Text(), nullable=True),
    sa.Column('photo_etag', sa.String(length=255), nullable=True),
    sa.Column('excluded', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('short_id', sa.String(length=8), nullable=False),
    sa.Column('applied_hash', sa.String(length=64), nullable=True),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('apply_status', sa.String(length=20), nullable=False),
    sa.Column('apply_error', sa.Text(), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("apply_status IN ('never', 'applied', 'failed', 'skipped', 'over_limit')", name=op.f('ck_employees_apply_status_valid')),
    sa.CheckConstraint("status IN ('active', 'suspended', 'removed')", name=op.f('ck_employees_status_valid')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_employees_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_employees')),
    sa.UniqueConstraint('org_id', 'google_user_id', name='uq_employees_org_google_user'),
    sa.UniqueConstraint('short_id', name=op.f('uq_employees_short_id'))
    )
    op.create_index(op.f('ix_employees_org_id'), 'employees', ['org_id'], unique=False)
    op.add_column('organizations', sa.Column('sync_requested_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:

    op.drop_column('organizations', 'sync_requested_at')
    op.drop_index(op.f('ix_employees_org_id'), table_name='employees')
    op.drop_table('employees')
