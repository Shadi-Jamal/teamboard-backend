"""Business logic.

Conventions
-----------
* A public service method is one use case and owns its transaction: it commits
  before returning, so the HTTP response is only produced after the change is
  durable. Reads do not commit.
* Services receive the session with the row-level-security context already set
  and build tenant-scoped repositories from the authorized ``TenantContext`` -
  never from client input.
* Every state change writes an audit-log entry in the same transaction.
"""
