"""banners and click tracking

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('tracked_links',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('token', sa.String(length=10), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('ref', sa.String(length=64), nullable=False),
    sa.Column('target_url', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('banner', 'social', 'website')", name=op.f('ck_tracked_links_kind_valid')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_tracked_links_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tracked_links')),
    sa.UniqueConstraint('org_id', 'kind', 'ref', 'target_url', name='uq_tracked_links_slot'),
    sa.UniqueConstraint('token', name=op.f('uq_tracked_links_token'))
    )
    op.create_index(op.f('ix_tracked_links_org_id'), 'tracked_links', ['org_id'], unique=False)
    op.create_table('banners',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('asset_id', sa.Uuid(), nullable=False),
    sa.Column('target_url', sa.Text(), nullable=False),
    sa.Column('alt_text', sa.String(length=160), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_banners_asset_id_assets'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_banners_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_banners'))
    )
    op.create_index(op.f('ix_banners_org_id'), 'banners', ['org_id'], unique=False)
    op.create_index('uq_banners_one_active', 'banners', ['org_id'], unique=True, postgresql_where='is_active')
    op.create_table('click_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('link_id', sa.Uuid(), nullable=False),
    sa.Column('employee_id', sa.Uuid(), nullable=True),
    sa.Column('clicked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('is_bot', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], name=op.f('fk_click_events_employee_id_employees'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['link_id'], ['tracked_links.id'], name=op.f('fk_click_events_link_id_tracked_links'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_click_events_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_click_events'))
    )
    op.create_index('ix_click_events_link_clicked', 'click_events', ['link_id', 'clicked_at'], unique=False)
    op.create_index('ix_click_events_org_clicked', 'click_events', ['org_id', 'clicked_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_click_events_org_clicked', table_name='click_events')
    op.drop_index('ix_click_events_link_clicked', table_name='click_events')
    op.drop_table('click_events')
    op.drop_index('uq_banners_one_active', table_name='banners', postgresql_where='is_active')
    op.drop_index(op.f('ix_banners_org_id'), table_name='banners')
    op.drop_table('banners')
    op.drop_index(op.f('ix_tracked_links_org_id'), table_name='tracked_links')
    op.drop_table('tracked_links')
