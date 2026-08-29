"""Role -> allowed-tools table for the Danus gateway.

This is the single source of truth for which MCP tools each runtime role may see.
Unknown roles fail closed to the verifier surface.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

ALL_TOOLS: Tuple[str, ...] = (
    "gm_add",
    "gm_search",
    "fact_submit",
    "fact_search",
    "fact_revoke",
    "implementation_pending_contracts",
    "implementation_submit_contract",
    "implementation_review_contract",
    "plan_pending_changes",
    "plan_propose_change",
    "plan_review_change",
    "plan_commit_change",
    "search_arxiv_theorems",
)

ROLE_TOOLS: Dict[str, Tuple[str, ...]] = {
    # Historical mathematical worker: unchanged.
    "worker": ("gm_add", "gm_search", "fact_submit", "fact_search", "search_arxiv_theorems"),
    # Scientific roles: plan and implementation-review authority is enforced here,
    # not only in prompts.
    "researcher": (
        "gm_add", "gm_search", "fact_submit", "fact_search",
        "implementation_pending_contracts", "implementation_submit_contract",
        "plan_pending_changes", "plan_propose_change", "search_arxiv_theorems",
    ),
    "reviewer": (
        "gm_add", "gm_search", "fact_search",
        "implementation_pending_contracts", "implementation_review_contract",
        "plan_pending_changes", "plan_review_change", "search_arxiv_theorems",
    ),
    "experimenter": ("gm_add", "gm_search", "fact_search"),
    # The current control-plane `main` identity is the Supervisor commit authority.
    "main": (
        "gm_add", "gm_search", "fact_search", "fact_revoke",
        "implementation_pending_contracts",
        "plan_pending_changes", "plan_commit_change", "search_arxiv_theorems",
    ),
    "verifier": ("search_arxiv_theorems",),
    "all": ALL_TOOLS,
}


def tools_for(role: str) -> List[str]:
    """Return the allowed tool names; unknown roles fail closed."""
    return list(ROLE_TOOLS.get(role, ROLE_TOOLS["verifier"]))
