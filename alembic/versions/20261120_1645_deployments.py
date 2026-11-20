"""deployments

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('deployments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('trigger', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('succeeded', sa.Integer(), nullable=False),
    sa.Column('failed', sa.Integer(), nullable=False),
    sa.Column('skipped', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed')", name=op.f('ck_deployments_status_valid')),
    sa.CheckConstraint("trigger IN ('manual', 'test', 'sync', 'enforce', 'plan_change')", name=op.f('ck_deployments_trigger_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_deployments_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_deployments_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployments'))
    )
    op.create_index(op.f('ix_deployments_org_id'), 'deployments', ['org_id'], unique=False)
    op.create_index('uq_deployments_one_manual_in_progress', 'deployments', ['org_id'], unique=True, postgresql_where="trigger = 'manual' AND status IN ('queued', 'running')")


def downgrade() -> None:
    op.drop_index('uq_deployments_one_manual_in_progress', table_name='deployments', postgresql_where="trigger = 'manual' AND status IN ('queued', 'running')")
    op.drop_index(op.f('ix_deployments_org_id'), table_name='deployments')
    op.drop_table('deployments')
