import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'
import type {
  LogEntry,
  ReviewMode,
  ReviewTaskDetail,
  ReviewTaskSummary,
  TaskStatus,
} from '../types'

const TERMINAL: TaskStatus[] = ['completed', 'failed', 'cancelled']

export interface StartPayload {
  file_ids: string[]
  mode?: ReviewMode
  ruleset_id?: string
  rule_ids?: string[]
  file_types?: (string | null)[]
  rule_group_ids?: string[]
  auto_match?: boolean
  kb_enabled?: boolean
  kb_id?: string
  web_search_enabled?: boolean
  /** 任务级缓存开关：undefined=跟随全局；true=强制启用；false=强制禁用 */
  cache_enabled?: boolean | null
  extra_instruction?: string
  legal_ruleset_ids?: string[]
  legal_rules_only?: boolean
}

/**
 * 管理后台审核任务：列表、活动任务、轮询刷新与生命周期操作。
 * 审核在后端异步执行，前端以轮询方式获取进度，不阻塞界面。
 */
export function useReviewTasks() {
  const [tasks, setTasks] = useState<ReviewTaskSummary[]>([])
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [activeTask, setActiveTask] = useState<ReviewTaskDetail | null>(null)
  const latestActive = useRef<string | null>(null)

  const loadList = useCallback(async () => {
    try {
      const { tasks: list } = await api.listTasks()
      setTasks(list)
    } catch {
      /* 后端不可用时不打断界面 */
    }
  }, [])

  const fetchActive = useCallback(async (id: string) => {
    try {
      setActiveTask(await api.getTask(id))
    } catch {
      /* 任务可能已删除 */
    }
  }, [])

  const start = useCallback(
    async (payload: StartPayload) => {
      const res = await api.createReviewTask(payload)
      setActiveTaskId(res.task_id)
      latestActive.current = res.task_id
      await loadList()
      await fetchActive(res.task_id)
      return res.task_id
    },
    [loadList, fetchActive],
  )

  const select = useCallback(
    async (id: string) => {
      setActiveTaskId(id)
      latestActive.current = id
      await fetchActive(id)
    },
    [fetchActive],
  )

  const deselect = useCallback(() => {
    setActiveTaskId(null)
    latestActive.current = null
    setActiveTask(null)
  }, [])

  const cancel = useCallback(
    async (id: string) => {
      try {
        await api.cancelTask(id)
      } catch {
        /* ignore */
      }
      await loadList()
      if (latestActive.current === id) await fetchActive(id)
    },
    [loadList, fetchActive],
  )

  const remove = useCallback(
    async (id: string) => {
      // eslint-disable-next-line no-console
      console.log('[remove] start', id, 'latestActive=', latestActive.current, 'activeTaskId=', activeTaskId)
      try {
        await api.deleteTask(id)
      } catch (err) {
        // eslint-disable-next-line no-console
        console.log('[remove] deleteTask error', (err as Error).message)
      }
      // eslint-disable-next-line no-console
      console.log('[remove] before clear latestActive=', latestActive.current, 'activeTaskId=', activeTaskId)
      if (latestActive.current === id) {
        setActiveTaskId(null)
        latestActive.current = null
      }
      // eslint-disable-next-line no-console
      console.log('[remove] after clear activeTaskId=', activeTaskId)
      await loadList()
      // eslint-disable-next-line no-console
      console.log('[remove] done')
    },
    [loadList, activeTaskId],
  )

  const clear = useCallback(async () => {
    try {
      await api.clearTasks()
    } catch {
      /* ignore */
    }
    await loadList()
  }, [loadList])

  // 轮询活动任务详情
  useEffect(() => {
    if (!activeTaskId) {
      setActiveTask(null)
      return
    }
    let alive = true
    const tick = async () => {
      try {
        const detail = await api.getTask(activeTaskId)
        if (!alive || latestActive.current !== activeTaskId) return
        setActiveTask(detail)
        if (TERMINAL.includes(detail.status)) {
          if (timer) clearInterval(timer)
          void loadList()
        }
      } catch {
        /* ignore */
      }
    }
    const timer = setInterval(tick, 1500)
    void tick()
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [activeTaskId, loadList])

  // 有运行中任务时，周期性刷新历史列表以更新进度
  useEffect(() => {
    if (!tasks.some((t) => !TERMINAL.includes(t.status))) return
    const handle = setInterval(loadList, 2500)
    return () => clearInterval(handle)
  }, [tasks, loadList])

  // 注意：列表加载不再在挂载时无条件触发——挂载瞬间登录态尚未建立，
  // 无令牌的请求会 401，导致列表一直为空、用户看不到任何任务及其进度。
  // 改为由 App 在「登录成功 / 会话恢复」后显式调用 loadList()（见 App.tsx）。
  const running = activeTask ? !TERMINAL.includes(activeTask.status) : false

  return {
    tasks,
    activeTaskId,
    activeTask,
    running,
    start,
    select,
    deselect,
    cancel,
    remove,
    clear,
    loadList,
  }
}

export type { LogEntry }
