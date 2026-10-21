---
trigger: always_on
description: Consult the graphify knowledge graph at graphify-out/ when you don't know which file to inspect, or need cross-file impact.
---

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Query the graph on judgement, not as a mandatory first step:
- Reach for `graphify query "<question>"` when you do not yet know which file or symbol to look at, when a grep would return hundreds of hits, or when you need cross-file impact ("what calls this", "what breaks if I change it"). Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts; these return a scoped subgraph, usually smaller than GRAPH_REPORT.md or raw grep output.
- Go straight to grep/glob/read when the target path or symbol is already known or inferable, when the answer is a specific function body or a literal value (the graph indexes symbols and call edges, not literals), or when the repo is small and well-named enough that one targeted grep beats a subgraph.
- Carry that judgement into subagent prompts: tell explorers to query the graph only when they don't know where to look, not unconditionally.

Maintenance:
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.
