"""Initial schema

Revision ID: 001
Revises: 
Create Date: 2025-02-09

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'services',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('agent_name', sa.String(100), nullable=False),
        sa.Column('agent_secret_hash', sa.String(64), nullable=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('category', sa.String(20), server_default='digital'),
        sa.Column('location', sa.String(200), nullable=True),
        sa.Column('shipping_available', sa.Boolean(), server_default='0'),
        sa.Column('tags', sa.String(500), nullable=True),
        sa.Column('acp_agent_wallet', sa.String(42), nullable=True),
        sa.Column('acp_job_offering', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
    )
    op.create_index('ix_services_id', 'services', ['id'])

    op.create_table(
        'bounties',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('poster_name', sa.String(100), nullable=False),
        sa.Column('poster_callback_url', sa.String(500), nullable=True),
        sa.Column('poster_secret_hash', sa.String(64), nullable=True),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('requirements', sa.Text(), nullable=True),
        sa.Column('budget', sa.Float(), nullable=False),
        sa.Column('category', sa.String(20), server_default='digital'),
        sa.Column('tags', sa.String(500), nullable=True),
        sa.Column('status', sa.String(20), server_default='open'),
        sa.Column('claimed_by', sa.String(100), nullable=True),
        sa.Column('claimer_callback_url', sa.String(500), nullable=True),
        sa.Column('claimer_secret_hash', sa.String(64), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('matched_service_id', sa.Integer(), nullable=True),
        sa.Column('matched_acp_agent', sa.String(42), nullable=True),
        sa.Column('matched_acp_job', sa.String(200), nullable=True),
        sa.Column('matched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('acp_job_id', sa.String(100), nullable=True),
        sa.Column('fulfilled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_bounties_id', 'bounties', ['id'])


def downgrade() -> None:
    op.drop_table('bounties')
    op.drop_table('services')
