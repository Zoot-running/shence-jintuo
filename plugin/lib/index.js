// src/index.ts
import { readFileSync, existsSync } from "node:fs";
import { defineTool } from "@deepseek-ai/dsh-tools";
var name = "shence-jintuo-alerts";
var inject = ["tools"];
function apply(ctx, config = {}) {
  const alertFile = config.alertFile ?? `${process.env["DSH_HOME"] ?? process.env["HOME"] + "/.dsh"}/storages/jintuo-alerts.jsonl`;
  let offset = 0;
  const poll = () => {
    if (!existsSync(alertFile)) return;
    const content = readFileSync(alertFile, "utf8");
    const lines = content.split("\n").filter((l) => l.length > 0);
    while (offset < lines.length) {
      try {
        const alert = JSON.parse(lines[offset]);
        ctx.emit("jintuo/alert", alert);
      } catch {
      }
      offset += 1;
    }
  };
  const timer = setInterval(poll, 2e3);
  ctx.effect(() => () => clearInterval(timer), "shence-jintuo-alerts.poll");
  ctx.tools.register(defineTool({
    name: "jintuo_alerts",
    description: "Read the recent guardian (jintuo) alerts: web crashes, resource pressure, restarts. Use it to inspect the health of the current DSH instance.",
    parameters: {},
    output: {
      schema: { type: "string" },
      render: (_args, value) => [{ type: "text", text: value }]
    },
    isConcurrencySafe: () => true,
    async execute() {
      if (!existsSync(alertFile)) return "(no jintuo alerts yet)";
      const lines = readFileSync(alertFile, "utf8").split("\n").filter((l) => l.length > 0);
      const recent = lines.slice(-20).map((l) => {
        const a = JSON.parse(l);
        return `[${new Date(a.at).toISOString()}] ${a.kind}: ${a.summary}${a.detail ? ` (${a.detail})` : ""}`;
      });
      return recent.length > 0 ? recent.join("\n") : "(no jintuo alerts yet)";
    }
  }));
}
export {
  apply,
  inject,
  name
};
