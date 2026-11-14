"""Architecture rules, enforced by static analysis of the source tree.

These tests make the layering and the "repositories cannot omit the tenant filter"
requirement mechanical instead of a convention: breaking a rule fails the build.
"""

import ast
from pathlib import Path

from teamboard.db.base import Base, TenantOwnedMixin
from teamboard.repositories.base import TenantScopedRepository

SRC = Path(__file__).resolve().parent.parent / "src" / "teamboard"

# Repositories that are intentionally not bound to a tenant. Each one works on
# identity data or performs a pre-tenant lookup; see their module docstrings.
GLOBAL_REPOSITORIES = {"users.py", "refresh_tokens.py", "tenants.py", "lookups.py"}

STATEMENT_CONSTRUCTORS = {"select", "insert", "update", "delete", "text", "pg_insert"}


def _modules(package: str) -> list[Path]:
    return sorted(p for p in (SRC / package).rglob("*.py") if p.name != "__init__.py")


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imports_any(path: Path, *prefixes: str) -> list[str]:
    return sorted(
        module
        for module in _imported_modules(path)
        if any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)
    )


def _tenant_scoped_repository_files() -> list[Path]:
    return [
        path
        for path in _modules("repositories")
        if path.name not in GLOBAL_REPOSITORIES and path.name != "base.py"
    ]


def test_tenant_scoped_repositories_never_build_statements_themselves() -> None:
    """They must go through the base-class builders, which add the tenant predicate.
    Calling select()/update()/delete()/text() directly is how a filter gets forgotten."""
    offenders: list[str] = []
    for path in _tenant_scoped_repository_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if isinstance(node.func, ast.Name) and name in STATEMENT_CONSTRUCTORS:
                    offenders.append(f"{path.name}:{node.lineno} calls {name}()")
            if isinstance(node, ast.ImportFrom) and node.module and "sqlalchemy" in node.module:
                imported = {alias.name for alias in node.names} & STATEMENT_CONSTRUCTORS
                if imported:
                    offenders.append(f"{path.name}:{node.lineno} imports {sorted(imported)}")
    assert offenders == []


def test_tenant_scoped_repository_modules_only_define_scoped_repositories() -> None:
    for path in _tenant_scoped_repository_files():
        classes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)]
        assert classes, f"{path.name} defines no repository"
        for cls in classes:
            bases = [ast.unparse(base) for base in cls.bases]
            assert any(base.startswith("TenantScopedRepository[") for base in bases), (
                f"{path.name}:{cls.name} must extend TenantScopedRepository"
            )


def test_every_tenant_owned_model_has_exactly_one_scoped_repository() -> None:
    tenant_owned = {
        mapper.class_
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantOwnedMixin)
    }
    covered = [repository.model for repository in TenantScopedRepository.__subclasses__()]

    assert len(tenant_owned) == 8
    assert set(covered) == tenant_owned
    assert len(covered) == len(set(covered))


def test_global_repositories_do_not_query_arbitrary_tenant_owned_tables() -> None:
    """The global repositories may only touch the tables they exist for."""
    allowed = {
        "users.py": {"User"},
        "refresh_tokens.py": {"RefreshToken"},
        "tenants.py": {"Tenant", "Membership"},
        "lookups.py": {"ApiKey", "Invitation"},
    }
    assert set(allowed) == GLOBAL_REPOSITORIES
    for filename, models in allowed.items():
        tree = ast.parse((SRC / "repositories" / filename).read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "teamboard.db.models"
            for alias in node.names
        }
        assert imported == models, filename


def test_rls_settings_are_written_in_exactly_one_place() -> None:
    writers = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "set_config(" in path.read_text()
    ]
    assert writers == ["db/rls.py"]


def test_routers_contain_no_data_access() -> None:
    """Layering: routers -> services -> repositories. A router that imports
    SQLAlchemy, a repository or the ORM is doing a service's job."""
    for path in _modules("api/routers"):
        forbidden = _imports_any(path, "sqlalchemy", "teamboard.repositories", "teamboard.db")
        assert forbidden == [], f"{path.name} imports {forbidden}"


def test_services_do_not_depend_on_the_http_layer() -> None:
    for path in _modules("services"):
        forbidden = _imports_any(path, "fastapi", "starlette", "teamboard.api")
        assert forbidden == [], f"{path.name} imports {forbidden}"


def test_repositories_do_not_depend_on_services_or_http() -> None:
    for path in _modules("repositories"):
        forbidden = _imports_any(
            path, "fastapi", "starlette", "teamboard.api", "teamboard.services"
        )
        assert forbidden == [], f"{path.name} imports {forbidden}"


def test_only_the_access_service_creates_tenant_contexts() -> None:
    """A TenantContext is proof of authorization; it must not be constructible ad hoc."""
    creators = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "TenantContext":
                creators.append(path.relative_to(SRC).as_posix())
    assert set(creators) == {"services/access.py"}
