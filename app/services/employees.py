"""Reading and updating employees. Every function is scoped to one organisation."""

import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeeStatus

MAX_LISTED = 1000


@dataclass(frozen=True)
class EmployeeCounts:
    total: int  # everyone still in the directory (not removed)
    active: int
    suspended: int
    excluded: int  # active, but left out by the admin

    @property
    def included(self) -> int:
        """Active employees who will receive the signature."""
        return self.active - self.excluded


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_employees(
    session: AsyncSession, org_id: uuid.UUID, search: str = ""
) -> list[Employee]:
    """People currently in the directory: active first, then by name."""
    query = select(Employee).where(
        Employee.org_id == org_id, Employee.status != EmployeeStatus.REMOVED
    )
    search = search.strip()
    if search:
        pattern = f"%{_escape_like(search)}%"
        query = query.where(
            or_(
                Employee.full_name.ilike(pattern, escape="\\"),
                Employee.email.ilike(pattern, escape="\\"),
            )
        )
    query = query.order_by(
        case((Employee.status == EmployeeStatus.ACTIVE, 0), else_=1),
        func.lower(Employee.full_name),
        Employee.email,
    ).limit(MAX_LISTED)
    return list((await session.execute(query)).scalars())


async def count_employees(session: AsyncSession, org_id: uuid.UUID) -> EmployeeCounts:
    active = Employee.status == EmployeeStatus.ACTIVE
    row = (
        await session.execute(
            select(
                func.count().filter(Employee.status != EmployeeStatus.REMOVED),
                func.count().filter(active),
                func.count().filter(Employee.status == EmployeeStatus.SUSPENDED),
                func.count().filter(active, Employee.excluded),
            ).where(Employee.org_id == org_id)
        )
    ).one()
    return EmployeeCounts(*row)


async def set_excluded(
    session: AsyncSession, org_id: uuid.UUID, employee_id: uuid.UUID, excluded: bool
) -> Employee | None:
    """Include or exclude one employee. Returns None if they are not in this organisation."""
    employee = (
        await session.execute(
            select(Employee).where(Employee.id == employee_id, Employee.org_id == org_id)
        )
    ).scalar_one_or_none()
    if employee is None:
        return None
    employee.excluded = excluded
    await session.commit()
    return employee
