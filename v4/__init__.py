"""Trading Agent VM V4.

V4 keeps V3 immutable on main and replaces prose/regex routing in Phase 6
with claim-scoped semantic LLM verification plus structured source bindings.
"""

from .runner import PipelineRunnerV4

__all__ = ["PipelineRunnerV4"]
