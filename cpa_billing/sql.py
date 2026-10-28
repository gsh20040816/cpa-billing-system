"""SQL expressions that differ between the supported billing database dialects."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import GenericFunction


class greatest(GenericFunction):
    """Row-wise maximum: GREATEST() on server databases, multi-argument max() on SQLite."""
    inherit_cache = True


class least(GenericFunction):
    """Row-wise minimum: LEAST() on server databases, multi-argument min() on SQLite."""
    inherit_cache = True


@compiles(greatest, "sqlite")
def _sqlite_greatest(element: greatest, compiler: Any, **kw: Any) -> str:
    return f"max({compiler.process(element.clauses, **kw)})"


@compiles(least, "sqlite")
def _sqlite_least(element: least, compiler: Any, **kw: Any) -> str:
    return f"min({compiler.process(element.clauses, **kw)})"
