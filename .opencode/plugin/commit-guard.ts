import type { Plugin } from "@opencode-ai/plugin"
import { existsSync, rmSync } from "node:fs"
import { join } from "node:path"

type ToolOutput = {
  args?: { command?: string; bash?: string; cmd?: string }
}

export default (async ({ directory }) => {
  const marker = join(directory, ".rev-ok")
  return {
    "tool.execute.before": async (_input: unknown, output: ToolOutput) => {
      const args = output?.args
      const command = args?.command ?? args?.bash ?? args?.cmd
      if (typeof command !== "string") return
      if (!/\bgit\s+commit\b/.test(command)) return
      if (!existsSync(marker)) {
        throw new Error(
          "workflows: commit blocked. Run /rev first; it runs `make quality` and creates the .rev-ok marker on pass."
        )
      }
      rmSync(marker, { force: true })
    },
  }
}) satisfies Plugin
