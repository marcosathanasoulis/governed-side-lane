"""Canonical capability-to-server mapping shared across adapters and run config.

This module exists to avoid circular imports between ``claude.py`` and
``mcp_run_config.py``. Both import from here; neither imports from the other.
"""

from __future__ import annotations

#: Capability -> the ONE exact MCP server name.  Used to determine which servers
#: are in scope when a capability is granted, and to validate that a per-run
#: config does not declare an unmapped server.
CAPABILITY_MCP_SERVERS: dict[str, str] = {
    # cm-services family
    "asana-read": "cm-services",
    "drive-read": "cm-services",
    "gcloud-read": "cm-services",
    "database-read": "cm-services",
    "algolia-read": "cm-services",
    "contentful-read": "cm-services",
    "contentful-master-read": "cm-services",
    "gateway-read": "cm-services",
    # host-native servers (registered by the user in their host config)
    "gitnexus": "gitnexus",
    "codegraph": "codegraph",
    "playwright": "playwright",
    "slack-read": "slack",
    # remote per-run servers
    "aws-read": "aws",
    "omniroute-read": "omniroute",
}

#: Capabilities whose servers are registered in the worker host's user-global MCP
#: config (the controlled HOME).  These are eligible for inclusion in the strict
#: bundle because their registration is a fixed local entry, not a project file.
#: Compare: ``RUN_CONFIG_CAPABILITIES`` are remote per-run registrations.
USER_SCOPE_MCP_CAPABILITIES = frozenset(
    {
        # cm-services family
        "asana-read",
        "drive-read",
        "gcloud-read",
        "database-read",
        "algolia-read",
        "contentful-read",
        "contentful-master-read",
        "gateway-read",
        # host-native servers
        "gitnexus",
        "codegraph",
        "playwright",
        "slack-read",
    }
)

#: Capabilities whose Codex evidence is connector-NAME presence rather than an
#: exact registration.  A Codex lane inherits its configured MCP servers
#: directly, with no ``mcp__<server>__`` allowlist, so the server it actually
#: gets may be registered under a near name (``gitnexus-local``); every other
#: capability's grants embed the exact server name even on Codex, and every
#: other host renders grants that do.  This is the one class
#: ``_graph_connector_evidence``'s ``require_exact`` argument distinguishes, and
#: the staffing promotion in ``cli._recommendation_host_snapshot`` reads the
#: same class so the two decide a Codex graph capability identically.
CODEX_CONNECTOR_NAME_CAPABILITIES = frozenset({"gitnexus", "codegraph"})

#: Subset of ``USER_SCOPE_MCP_CAPABILITIES`` whose servers live at the
#: cm-services entry in the user-global config.  Excluded from project-approval
#: logic (``_approved_mcp_servers``) because those servers are user-global, not
#: project-registered.  Also excluded from per-run config validation so a run
#: config can never declare cm-services (it is a HOME-level registration).
CM_SERVICES_CAPABILITIES = frozenset(
    {
        "asana-read",
        "drive-read",
        "gcloud-read",
        "database-read",
        "algolia-read",
        "contentful-read",
        "contentful-master-read",
        "gateway-read",
    }
)

#: Capabilities whose servers are delivered as validated per-run remote
#: streamable-HTTP registrations (not from user config).
RUN_CONFIG_CAPABILITIES = frozenset({"aws-read", "omniroute-read"})
