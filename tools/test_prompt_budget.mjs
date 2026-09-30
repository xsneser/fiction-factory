import assert from "node:assert/strict";
import { inputBudget } from "../vendor/dsh-ne/events-runner.mjs";

const budget = inputBudget({
  system: "writer",
  messages: [{ role: "user", content: "plot" }],
  tools: [{ name: "prepare_plot_run", description: "prepare" }],
});

assert.equal(typeof budget.system_chars, "number");
assert.equal(typeof budget.message_chars, "number");
assert.equal(typeof budget.tool_schema_chars, "number");
assert.equal(budget.total_chars,
  budget.system_chars + budget.message_chars + budget.tool_schema_chars);
assert.equal(budget.estimated_tokens, Math.ceil(budget.total_chars / 2));
console.log("[OK] canonical model-input budget snapshot");
