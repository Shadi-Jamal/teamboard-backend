"""Data access.

Two kinds of repositories exist, and the split is enforced by a test
(``tests/test_architecture.py``):

* **Tenant-scoped** repositories extend ``TenantScopedRepository``. They are
  constructed with a tenant ID and may only build statements through the base
  class, which adds the tenant predicate. They never call ``select()`` directly.
* **Global** repositories (``users``, ``refresh_tokens``, ``tenants``,
  ``lookups``) work on identity data or perform the few lookups that must happen
  before a tenant is known. They are short, listed explicitly, and still run
  under row-level security.
"""
