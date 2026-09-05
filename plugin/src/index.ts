/**
 * 金柝告警读取插件：尾随 jintuo-alerts.jsonl（金柝启动器写入），
 * 向会话投递 `jintuo/alert` 事件，并注册 jintuo_alerts 工具供模型查询。
 * @module @shence/jintuo-alerts
 */

import type { Context } from '@deepseek-ai/cordis'
import { readFileSync, existsSync } from 'node:fs'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'shence-jintuo-alerts'
export const inject = ['tools']

export interface Config {
  /** 告警文件路径（默认 $DSH_HOME/storages/jintuo-alerts.jsonl）。 */
  alertFile?: string
}

export interface JintuoAlert {
  at: number
  kind: string
  summary: string
  detail?: string
}

declare module '@deepseek-ai/cordis' {
  interface Events {
    'jintuo/alert'(alert: JintuoAlert): void
  }
}

export function apply(ctx: Context, config: Config = {}): void {
  const alertFile = config.alertFile
    ?? `${process.env['DSH_HOME'] ?? process.env['HOME'] + '/.dsh'}/storages/jintuo-alerts.jsonl`

  // 尾随：每 2s 读一次，新行投递事件（只告警不代决策）。
  let offset = 0
  const poll = (): void => {
    if (!existsSync(alertFile)) return
    const content = readFileSync(alertFile, 'utf8')
    const lines = content.split('\n').filter(l => l.length > 0)
    while (offset < lines.length) {
      try {
        const alert = JSON.parse(lines[offset]!) as JintuoAlert
        ctx.emit('jintuo/alert', alert)
      } catch {
        // 半行（写入中）忽略，下次轮询再读。
      }
      offset += 1
    }
  }
  const timer = setInterval(poll, 2000)
  ctx.effect(() => () => clearInterval(timer), 'shence-jintuo-alerts.poll')

  ctx.tools.register(defineTool({
    name: 'jintuo_alerts',
    description: 'Read the recent guardian (jintuo) alerts: web crashes, resource pressure, restarts. Use it to inspect the health of the current DSH instance.',
    parameters: {},
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    isConcurrencySafe: () => true,
    async execute() {
      if (!existsSync(alertFile)) return '(no jintuo alerts yet)'
      const lines = readFileSync(alertFile, 'utf8').split('\n').filter(l => l.length > 0)
      const recent = lines.slice(-20).map(l => {
        const a = JSON.parse(l) as JintuoAlert
        return `[${new Date(a.at).toISOString()}] ${a.kind}: ${a.summary}${a.detail ? ` (${a.detail})` : ''}`
      })
      return recent.length > 0 ? recent.join('\n') : '(no jintuo alerts yet)'
    },
  }))
}
