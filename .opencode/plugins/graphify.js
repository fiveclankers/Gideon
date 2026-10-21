// graphify OpenCode plugin
// 1. Injects a knowledge graph reminder before the first shell command of a
//    session when the graph exists.
// 2. Gates the session's first exploratory grep/glob when the graph exists, so
//    the agent at least considers `graphify query` before scanning raw files.
//
// One file for both runtimes. OpenCode 2 reads `id` and `setup()` from the
// default export; OpenCode 1 (1.3.4 and later) reads `server()` from it. Each
// ignores the other's function. Nothing is imported from an opencode package:
// a plain file in .opencode/plugins/ cannot resolve one.
//
// IMPORTANT: keep the reminder string free of backticks and $(...) constructs.
// The hook prepends `echo "<reminder>" ; <cmd>` to the user's shell command;
// backticks inside the double-quoted echo trigger command substitution,
// which both corrupts tool output and silently executes the very graphify
// command we are only suggesting. Plain words render fine in opencode's TUI.
import { existsSync } from "fs";
import { join } from "path";

// ';' not '&&' — Windows PowerShell 5.1 rejects '&&' as a statement
// separator, breaking the first shell command of the session (#1646).
const REMINDER =
  'echo "[graphify] knowledge graph at graphify-out/. For focused questions, run graphify query with your question (scoped subgraph, usually much smaller than GRAPH_REPORT.md) instead of grepping raw files. Read GRAPH_REPORT.md only for broad architecture context." ; ';

const GATE_MESSAGE =
  "[graphify] This project has a knowledge graph at graphify-out/. Run `graphify query \"<your question>\"` first " +
  "(it returns a scoped subgraph with file and line references, usually far smaller than grep output), then search " +
  "the raw files it points at. This gate fires once per session: if the graph is not the right tool for this search, " +
  "repeat the exact same call and it will run.";

const hasGraph = (directory) => existsSync(join(directory, "graphify-out", "graph.json"));

// Only gate searches that are clearly exploratory: regex syntax, quoting,
// whitespace, or a wildcard glob. Exact identifiers and literal paths
// ("SignalBus", "gideon.py", "src/app.ts") always run — those are the cases
// where grep is already the right tool.
const EXPLORATORY = /[\s*+?[\](){}|^$\\]/;
const isExploratory = (value) =>
  typeof value === "string" && value.length > 0 && EXPLORATORY.test(value);

// A project and a directory above it (often ~) can each hold a copy of this
// file, and OpenCode loads both. OpenCode 2 fails the second plugin with the
// same id ("Duplicate plugin ID"), so the id carries this file's location. The
// copies share one record of who was reminded, so a command gets one reminder.
const location = [...import.meta.url].reduce((hash, ch) => (Math.imul(hash, 31) + ch.charCodeAt(0)) >>> 0, 7);
const reminded = (globalThis.__graphifyReminded ??= new Set());
const gated = (globalThis.__graphifyGated ??= new Set());

// Throw (which OpenCode surfaces as a failed tool call) on the first
// exploratory search of the session. The key is recorded before throwing, so
// retrying the same search always proceeds.
const gate = (directory, key, tool, pattern) => {
  if (tool !== "grep" && tool !== "glob") return;
  if (gated.has(key)) return;
  if (!hasGraph(directory)) return;
  if (!isExploratory(pattern)) return;

  gated.add(key);
  throw new Error(GATE_MESSAGE);
};

export default {
  id: `graphify-${location.toString(16)}`,

  // OpenCode 2: the command tool is "shell", and one server outlives many
  // sessions, so the reminder is tracked per session.
  async setup(ctx) {
    await ctx.tool.hook("execute.before", (event) => {
      if (event.tool !== "shell" || typeof event.input?.command !== "string") return;
      if (reminded.has(event.sessionID)) return;
      if (!hasGraph(ctx.location.directory)) return;

      event.input = { ...event.input, command: REMINDER + event.input.command };
      reminded.add(event.sessionID);
    });

    await ctx.tool.hook("execute.before", (event) => {
      const key = event.sessionID ?? ctx.location.directory;
      gate(ctx.location.directory, key, event.tool, event.input?.pattern);
    });
  },

  // OpenCode 1: the command tool is "bash", and the server lives as long as
  // the session, so the reminder is tracked per project directory.
  async server({ directory }) {
    return {
      "tool.execute.before": async (input, output) => {
        const tool = input.tool;
        const key = input.sessionID ?? directory;

        gate(directory, key, tool, output.args?.pattern);

        if (tool !== "bash" || typeof output.args?.command !== "string") return;
        if (reminded.has(directory)) return;
        if (!hasGraph(directory)) return;

        output.args.command = REMINDER + output.args.command;
        reminded.add(directory);
      },
    };
  },
};
