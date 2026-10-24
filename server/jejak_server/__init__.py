"""Jejak server: the knowledge graph behind authenticated HTTP and MCP."""
import os
import sys

# jejak_common (redaction, topics, the scoring formula) is shared with the
# client hooks and lives in ../../hooks. Install and Docker keep that layout.
_HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hooks")
if os.path.isdir(_HOOKS) and _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)
