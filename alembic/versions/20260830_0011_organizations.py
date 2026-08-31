"""Add school, class and invitation organization models."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0011"
down_revision = "20260830_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schools",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="schools_code_key"),
    )
    op.create_index("ix_schools_code", "schools", ["code"], unique=True)
    op.create_index("ix_schools_status", "schools", ["status"])
    op.create_index("ix_schools_created_by", "schools", ["created_by"])
    op.create_table(
        "school_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("school_id", sa.String(36), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("school_id", "user_id", name="uq_school_membership"),
    )
    for name in ("school_id", "user_id", "role", "status"):
        op.create_index(f"ix_school_memberships_{name}", "school_memberships", [name])
    op.create_table(
        "school_classes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("school_id", sa.String(36), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("grade", sa.String(40), nullable=True),
        sa.Column("academic_year", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("school_id", "name", "academic_year", name="uq_school_class_name_year"),
    )
    for name in ("school_id", "grade", "academic_year", "status", "created_by"):
        op.create_index(f"ix_school_classes_{name}", "school_classes", [name])
    op.create_table(
        "class_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("class_id", sa.String(36), sa.ForeignKey("school_classes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("class_id", "user_id", name="uq_class_membership"),
    )
    for name in ("class_id", "user_id", "role", "status"):
        op.create_index(f"ix_class_memberships_{name}", "class_memberships", [name])
    op.create_table(
        "organization_invites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("school_id", sa.String(36), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("class_id", sa.String(36), sa.ForeignKey("school_classes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("member_role", sa.String(20), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code_hash", name="organization_invites_code_hash_key"),
    )
    for name in ("school_id", "class_id", "code_hash", "member_role", "expires_at", "is_active", "created_by"):
        op.create_index(
            f"ix_organization_invites_{name}",
            "organization_invites",
            [name],
            unique=name == "code_hash",
        )


def downgrade() -> None:
    op.drop_table("organization_invites")
    op.drop_table("class_memberships")
    op.drop_table("school_classes")
    op.drop_table("school_memberships")
    op.drop_table("schools")
