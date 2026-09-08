import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Modal,
  Space,
  Spin,
  Tabs,
  Tag,
} from 'antd'
import {
  DownloadOutlined,
  FileExcelOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  FileWordOutlined,
} from '@ant-design/icons'
import workerCode from 'pdfjs-dist/build/pdf.worker.min.mjs?raw'
import 'pdfjs-dist/web/pdf_viewer.css'
import { api } from '../services/api'
import type { FilePreview, FindingLocation, LocRect } from '../types'

/** pdf.js worker：以 Blob URL 内联，避免服务器对 .mjs 返回错误 MIME
 * （部分 nginx 配置不含 mjs 类型会以 octet-stream 下发，模块脚本会被拒载）。 */
let _workerUrl: string | null = null
function pdfWorkerUrl(): string {
  if (!_workerUrl) {
    _workerUrl = URL.createObjectURL(
      new Blob([workerCode], { type: 'text/javascript' }),
    )
  }
  return _workerUrl
}

/**
 * 原始文件预览 + 自动定位（统一入口，所有类型均渲染原始文件）：
 * - PDF：pdf.js 自渲染（canvas + 文本层），打开即滚动到目标页，并用后端
 *   rects 坐标（PDF 坐标系）绘制高亮框；无坐标时在文本层内检索片段高亮；
 * - Word(docx)：docx-preview 网页渲染（保留分页），在渲染结果中检索锚点片段并高亮滚动；
 * - Excel(xlsx/xls/csv)：SheetJS 解析，逐 sheet 检索单元格，命中 sheet 自动切换并高亮单元格；
 * - 其他/未知类型：回落提取文本分页预览（页内高亮，与原文定位同源）。
 *
 * 定位信息来自审核结论的 locations（后端 _attach_locations）：page 为页码、
 * snippet 为命中的原文片段、char_start/char_end 为提取文本中的绝对下标、
 * rects 为 PDF 原始页面上的锚点矩形（原点左下、y 向上，与 PDF.js viewport 兼容）。
 */

type PDFDocProxy = Awaited<
  ReturnType<typeof import('pdfjs-dist').getDocument>['promise']
>

const MATCH_TAG: Record<string, { color: string; label: string }> = {
  exact: { color: 'success', label: '精确命中' },
  normalized: { color: 'processing', label: '格式差异命中' },
  fuzzy: { color: 'warning', label: '近似命中' },
}

type PreviewMode = 'pdf' | 'docx' | 'xlsx' | 'text'

function extOf(loc: FindingLocation): string {
  const e = (loc.ext || loc.filename.split('.').pop() || '').toLowerCase()
  return e.replace('.', '')
}

function normText(s: string): string {
  return (s || '').replace(/\s+/g, '')
}

/** 归一化下标 → 原始下标（跳过空白字符） */
function rawIndexForNorm(raw: string, normIdx: number): number {
  if (normIdx <= 0) return 0
  let acc = 0
  for (let i = 0; i < raw.length; i++) {
    if (!/\s/.test(raw[i])) {
      acc++
      if (acc >= normIdx) return i + 1
    }
  }
  return raw.length
}

/** 在已渲染 DOM 中检索片段：跨文本节点匹配（忽略空白差异），命中处包 <mark> 并滚动。 */
function highlightInDom(container: HTMLElement, snippet: string): boolean {
  const needle = normText(snippet).slice(0, 80)
  if (!needle) return false
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) =>
      n.nodeValue && n.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
  })
  const nodes: Text[] = []
  while (walker.nextNode()) nodes.push(walker.currentNode as Text)
  const normParts = nodes.map((n) => normText(n.nodeValue || ''))
  const hay = normParts.join('')
  const idx = hay.indexOf(needle)
  if (idx < 0) return false

  // 归一化区间映射回各文本节点的原始区间
  type Span = { node: Text; rawStart: number; rawEnd: number }
  const spans: Span[] = []
  let acc = 0
  for (let i = 0; i < nodes.length && acc < idx + needle.length; i++) {
    const raw = nodes[i].nodeValue || ''
    const nLen = normParts[i].length
    const s = Math.max(idx, acc)
    const e = Math.min(idx + needle.length, acc + nLen)
    if (e > s) {
      const rawStart = rawIndexForNorm(raw, s - acc)
      const rawEnd = rawIndexForNorm(raw, e - acc)
      if (rawEnd > rawStart) spans.push({ node: nodes[i], rawStart, rawEnd })
    }
    acc += nLen
  }
  let first: HTMLElement | null = null
  for (const sp of spans) {
    const raw = sp.node.nodeValue || ''
    let mid: Text = sp.node
    if (sp.rawEnd < raw.length) mid.splitText(sp.rawEnd)
    if (sp.rawStart > 0) mid = mid.splitText(sp.rawStart)
    const mark = document.createElement('mark')
    mark.className = 'src-locate-hit'
    mark.style.background = '#ffe58f'
    mark.textContent = mid.nodeValue
    mid.parentNode?.insertBefore(mark, mid)
    mid.remove()
    if (!first) first = mark
  }
  if (first) {
    // 等渲染布局稳定后再滚动
    setTimeout(() => first?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 150)
  }
  return Boolean(first)
}

/** 把后端 rects（PDF 坐标系，y 向上）转换为视口内的绝对定位高亮框 */
function drawRectHighlights(
  pageEl: HTMLElement,
  viewport: { convertToViewportRectangle: (r: number[]) => number[] },
  rects: LocRect[],
) {
  pageEl.querySelectorAll('.src-pdf-rect').forEach((n) => n.remove())
  for (const r of rects) {
    const [vx0, vy0, vx1, vy1] = viewport.convertToViewportRectangle([
      r.x0,
      r.y0,
      r.x1,
      r.y1,
    ])
    const div = document.createElement('div')
    div.className = 'src-pdf-rect'
    div.style.position = 'absolute'
    div.style.left = `${Math.min(vx0, vx1)}px`
    div.style.top = `${Math.min(vy0, vy1)}px`
    div.style.width = `${Math.abs(vx1 - vx0)}px`
    div.style.height = `${Math.abs(vy1 - vy0)}px`
    div.style.background = 'rgba(255, 197, 61, 0.45)'
    div.style.border = '1px solid #faad14'
    div.style.pointerEvents = 'none'
    div.style.zIndex = '3'
    pageEl.appendChild(div)
  }
}

/** 提取文本页内高亮渲染（text 回落模式） */
function HighlightedText({ preview }: { preview: FilePreview }) {
  const segments = useMemo(() => {
    const text = preview.page_text || ''
    const hs = [...(preview.highlights || [])].sort((a, b) => a.start - b.start)
    const out: { text: string; mark: boolean }[] = []
    let pos = 0
    for (const h of hs) {
      if (h.start > pos) out.push({ text: text.slice(pos, h.start), mark: false })
      out.push({ text: text.slice(h.start, h.end), mark: true })
      pos = Math.max(pos, h.end)
    }
    if (pos < text.length) out.push({ text: text.slice(pos), mark: false })
    return out
  }, [preview])
  return (
    <pre
      style={{
        margin: 0,
        padding: '14px 16px',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        fontSize: 13,
        lineHeight: 1.9,
        fontFamily: 'inherit',
      }}
    >
      {segments.map((s, i) =>
        s.mark ? (
          <mark key={i} style={{ background: '#ffe58f', padding: '0 1px' }}>
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </pre>
  )
}

interface Props {
  open: boolean
  location: FindingLocation | null
  onClose: () => void
}

export default function SourcePreviewModal({ open, location, onClose }: Props) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  /** PDF：pdf.js 文档代理 + 每页尺寸（占位用）+ 目标页 */
  const [pdfState, setPdfState] = useState<{
    doc: PDFDocProxy
    sizes: { width: number; height: number }[]
    targetPage: number
  } | null>(null)
  const [sheets, setSheets] = useState<{ name: string; html: string }[]>([])
  const [activeSheet, setActiveSheet] = useState<string | null>(null)
  const [textPreview, setTextPreview] = useState<FilePreview | null>(null)
  /** docx 原始字节：容器挂载后由渲染 effect 消费（renderAsync 需要已挂载的 DOM） */
  const [docxBuf, setDocxBuf] = useState<ArrayBuffer | null>(null)
  const [docxSnippet, setDocxSnippet] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  /** 当前打开的 pdf.js 文档（location 切换/关闭时销毁） */
  const pdfDocRef = useRef<PDFDocProxy | null>(null)
  /** PDF 懒渲染：已渲染/渲染中页集合 + IntersectionObserver */
  const pdfRenderedRef = useRef<Set<number>>(new Set())
  const pdfObserverRef = useRef<IntersectionObserver | null>(null)
  const pdfPageRefs = useRef<Map<number, HTMLDivElement>>(new Map())
  const pdfViewportRef = useRef<Map<number, import('pdfjs-dist').PageViewport>>(new Map())
  const pdfRectsRef = useRef<LocRect[]>([])
  const pdfSnippetRef = useRef('')

  const modeOf = (e: string): PreviewMode =>
    e === 'pdf' ? 'pdf' : e === 'docx' ? 'docx'
      : ['xlsx', 'xls', 'csv'].includes(e) ? 'xlsx' : 'text'
  const mode = location ? modeOf(extOf(location)) : 'text'

  // —— PDF 懒渲染：单页 canvas + 文本层 + 高亮 ——
  useEffect(() => {
    const st = pdfState
    if (!st) return
    let disposed = false
    ;(async () => {
      const pdfjs = await import('pdfjs-dist')
      pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl()
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      const pageWidth = st.sizes[0]?.width || 800

      const renderPage = async (pageNum: number) => {
        if (disposed || pdfRenderedRef.current.has(pageNum)) return
        pdfRenderedRef.current.add(pageNum)
        const el = pdfPageRefs.current.get(pageNum)
        if (!el) return
        try {
          const page = await st.doc.getPage(pageNum)
          const viewport = page.getViewport({ scale: 1 })
          const scale = pageWidth / viewport.width
          const vp = page.getViewport({ scale })
          pdfViewportRef.current.set(pageNum, vp)
          const canvas = document.createElement('canvas')
          canvas.width = Math.floor(vp.width * dpr)
          canvas.height = Math.floor(vp.height * dpr)
          canvas.style.width = `${vp.width}px`
          canvas.style.height = `${vp.height}px`
          const ctx = canvas.getContext('2d')!
          await page.render({
            canvasContext: ctx,
            viewport: page.getViewport({ scale: scale * dpr }),
          }).promise
          if (disposed) return
          const textDiv = document.createElement('div')
          textDiv.className = 'textLayer'
          el.innerHTML = ''
          el.appendChild(canvas)
          el.appendChild(textDiv)
          const tl = new pdfjs.TextLayer({
            textContentSource: page.streamTextContent(),
            container: textDiv,
            viewport: vp,
          })
          await tl.render()
          if (disposed) return
          // 高亮：优先后端 rects 坐标；无坐标则在文本层内检索片段
          const rects = pdfRectsRef.current.filter((r) => r.page === pageNum)
          if (rects.length) {
            drawRectHighlights(el, vp, rects)
          } else if (pdfSnippetRef.current) {
            highlightInDom(textDiv, pdfSnippetRef.current)
          }
        } catch (err) {
          pdfRenderedRef.current.delete(pageNum)
          if (!disposed) setError(err instanceof Error ? err.message : String(err))
        }
      }

      // IntersectionObserver：进入视口才渲染
      pdfObserverRef.current?.disconnect()
      const observer = new IntersectionObserver(
        (entries) => {
          for (const en of entries) {
            if (en.isIntersecting) {
              const n = Number((en.target as HTMLElement).dataset.page)
              if (n) renderPage(n)
            }
          }
        },
        { root: scrollRef.current, rootMargin: '600px 0px' },
      )
      pdfObserverRef.current = observer
      pdfPageRefs.current.forEach((el) => observer.observe(el))
    })()
    return () => {
      disposed = true
    }
  }, [pdfState])

  useEffect(() => {
    if (!open || !location) return
    let cancelled = false
    let revoking: string | null = null
    const e = extOf(location)
    const m = modeOf(e)
    setLoading(true)
    setError('')
    setPdfState(null)
    setSheets([])
    setActiveSheet(null)
    setTextPreview(null)
    setDocxBuf(null)
    setDocxSnippet('')
    pdfRenderedRef.current = new Set()
    pdfPageRefs.current = new Map()
    pdfViewportRef.current = new Map()
    pdfObserverRef.current?.disconnect()

    const needle = location.snippet || ''
    pdfSnippetRef.current = needle
    pdfRectsRef.current = location.rects || []

    /** 从提取文本的定位描述（如「第3页」）解析页码 */
    const pageFromLabel = (): number | null => {
      const mt = /第\s*(\d+)\s*页/.exec(location.page_label || location.snippet || '')
      return mt ? Number(mt[1]) : null
    }

    ;(async () => {
      try {
        if (m === 'pdf') {
          // pdf.js 自渲染：打开即跳目标页 + rects/文本高亮（浏览器内置 viewer
          // 无法编程跳页/高亮，故弃用 iframe 方案）
          const url = await api.getFileObjectUrl(location.file_id as string)
          const resp = await fetch(url)
          const buf = await resp.arrayBuffer()
          URL.revokeObjectURL(url)
          if (cancelled) return
          const pdfjs = await import('pdfjs-dist')
          pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl()
          const doc = await pdfjs.getDocument({ data: buf }).promise
          if (cancelled) {
            doc.destroy()
            return
          }
          // 目标页：locations.page 优先 → rects.page → 定位描述「第N页」→ 文本检索
          let targetPage =
            location.page && location.page >= 1 && location.page <= doc.numPages
              ? location.page
              : null
          if (!targetPage && (location.rects?.length || 0) > 0) {
            targetPage = location.rects![0].page
          }
          if (!targetPage) targetPage = pageFromLabel()
          // 每页占位尺寸（宽度统一按首页，保证滚动条与 IntersectionObserver 可用）
          const first = await doc.getPage(1)
          const baseW = first.getViewport({ scale: 1 }).width
          const sizes: { width: number; height: number }[] = []
          for (let i = 1; i <= doc.numPages; i++) {
            const vp = (await doc.getPage(i)).getViewport({ scale: 1 })
            sizes.push({ width: baseW, height: (vp.height * baseW) / vp.width })
          }
          if (cancelled) {
            doc.destroy()
            return
          }
          if (!targetPage && needle) {
            // 无页码信息：逐页检索文本内容（上限 300 页防卡顿）
            const needleN = normText(needle).slice(0, 80)
            const cap = Math.min(doc.numPages, 300)
            for (let i = 1; i <= cap && !targetPage; i++) {
              const pg = await doc.getPage(i)
              const tc = await pg.getTextContent()
              const s = normText(
                tc.items.map((it) => ('str' in it ? it.str : '')).join(''),
              )
              if (needleN && s.includes(needleN)) targetPage = i
            }
          }
          setPdfState({
            doc,
            sizes,
            targetPage: targetPage || 1,
          })
          pdfDocRef.current = doc
        } else if (m === 'docx') {
          const url = await api.getFileObjectUrl(location.file_id as string)
          const resp = await fetch(url)
          const buf = await resp.arrayBuffer()
          URL.revokeObjectURL(url)
          if (cancelled) return
          // 先存字节，待容器挂载后由渲染 effect 执行 renderAsync（需要真实 DOM）
          setDocxBuf(buf)
          setDocxSnippet(needle)
        } else if (m === 'xlsx') {
          const url = await api.getFileObjectUrl(location.file_id as string)
          const resp = await fetch(url)
          const buf = await resp.arrayBuffer()
          URL.revokeObjectURL(url)
          if (cancelled) return
          const XLSX = await import('xlsx')
          const wb = XLSX.read(buf, { type: 'array' })
          const target = normText(needle).slice(0, 60)
          // 逐 sheet 检索单元格
          let hit: { sheet: string; r: number; c: number } | null = null
          const built: { name: string; html: string }[] = []
          for (const name of wb.SheetNames) {
            const sheet = wb.Sheets[name]
            const rows = XLSX.utils.sheet_to_json<string[]>(sheet, {
              header: 1,
              raw: false,
              defval: '',
              blankrows: true,
            })
            if (!hit && target) {
              outer: for (let r = 0; r < rows.length; r++) {
                for (let c = 0; c < (rows[r] || []).length; c++) {
                  if (normText(String(rows[r]?.[c] ?? '')).includes(target)) {
                    hit = { sheet: name, r, c }
                    break outer
                  }
                }
              }
            }
            // 自建表格保证行列下标与检索一致
            const cols = Math.max(1, ...rows.map((r) => (r || []).length))
            const html = ['<table class="xlsx-preview-table"><tbody>']
            for (let r = 0; r < rows.length; r++) {
              html.push('<tr>')
              for (let c = 0; c < cols; c++) {
                const v = String(rows[r]?.[c] ?? '')
                html.push(
                  `<td data-r="${r}" data-c="${c}"${
                    r === 0 ? ' class="xlsx-head"' : ''
                  }>${v ? v.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : ''}</td>`,
                )
              }
              html.push('</tr>')
            }
            html.push('</tbody></table>')
            built.push({ name, html: html.join('') })
          }
          if (cancelled) return
          setSheets(built)
          setActiveSheet(hit?.sheet || built[0]?.name || null)
          // 渲染后高亮命中单元格
          setTimeout(() => {
            const container = containerRef.current
            if (!container || !hit) return
            const cell = container.querySelector(
              `td[data-r="${hit.r}"][data-c="${hit.c}"]`,
            ) as HTMLElement | null
            if (cell) {
              cell.style.background = '#ffe58f'
              cell.scrollIntoView({ behavior: 'smooth', block: 'center' })
            }
          }, 200)
        } else {
          // text 回落：提取文本分页预览（与原文定位同源口径）
          const preview = await api.getFilePreview(location.file_id as string, {
            page: location.page ?? undefined,
            start: location.char_start ?? undefined,
            end: location.char_end ?? undefined,
            snippet: location.snippet || undefined,
          })
          if (cancelled) return
          setTextPreview(preview)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()

    return () => {
      cancelled = true
      if (revoking) URL.revokeObjectURL(revoking)
      // location 变更/关闭：销毁上一次打开的 pdf.js 文档
      pdfDocRef.current?.destroy()
      pdfDocRef.current = null
      pdfObserverRef.current?.disconnect()
    }
  }, [open, location])

  // pdfState 就绪后：滚动到目标页（占位高度已同步设置，scrollIntoView 可达）
  useEffect(() => {
    if (!pdfState) return
    const el = pdfPageRefs.current.get(pdfState.targetPage)
    if (el) {
      setTimeout(() => el.scrollIntoView({ block: 'start' }), 50)
    }
  }, [pdfState])

  // docx 渲染：容器挂载（loading 结束）后执行，renderAsync 需要真实 DOM
  useEffect(() => {
    if (mode !== 'docx' || loading || error || !docxBuf) return
    let cancelled = false
    const container = containerRef.current
    if (!container) return
    ;(async () => {
      try {
        const docx = await import('docx-preview')
        if (cancelled || !containerRef.current) return
        containerRef.current.innerHTML = ''
        await docx.renderAsync(docxBuf, containerRef.current, undefined, {
          className: 'docx-preview-doc',
          inWrapper: true,
          breakPages: true,
          ignoreLastRenderedPageBreak: false,
        })
        if (!cancelled && docxSnippet) highlightInDom(containerRef.current!, docxSnippet)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [mode, loading, error, docxBuf, docxSnippet])

  useEffect(
    () => () => {
      pdfObserverRef.current?.disconnect()
      pdfDocRef.current?.destroy()
      pdfDocRef.current = null
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )

  const icon =
    mode === 'pdf' ? <FilePdfOutlined /> :
    mode === 'docx' ? <FileWordOutlined /> :
    mode === 'xlsx' ? <FileExcelOutlined /> : <FileTextOutlined />

  const match = location?.match_type ? MATCH_TAG[location.match_type] : null

  return (
    <Modal
      title={
        <Space size={8} wrap>
          {icon}
          <span style={{ fontSize: 15 }}>{location?.filename || '原文预览'}</span>
          {location?.page_label && (
            <Tag color="blue" style={{ marginRight: 0 }}>
              {location.page_label}
              {location.page_count ? ` / 共${location.page_count}页` : ''}
            </Tag>
          )}
          {match && (
            <Tag color={match.color} style={{ marginRight: 0 }}>
              {match.label}
            </Tag>
          )}
        </Space>
      }
      open={open}
      onCancel={onClose}
      width={1020}
      footer={
        location?.file_id ? (
          <Space>
            <Button
              icon={<DownloadOutlined />}
              onClick={() =>
                location.file_id &&
                api.downloadFile(location.file_id, location.filename || 'download')
              }
            >
              下载原文件
            </Button>
            <Button type="primary" onClick={onClose}>
              关闭
            </Button>
          </Space>
        ) : null
      }
      destroyOnClose
    >
      {loading && (
        <div style={{ textAlign: 'center', padding: '60px 0' }}>
          <Spin tip="正在加载原始文件…">
            <div style={{ height: 80 }} />
          </Spin>
        </div>
      )}
      {!loading && error && <Alert type="error" showIcon message={error} />}
      {!loading && !error && location && (
        <>
          {!location.file_id && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="该结论的源文件已不在系统中（或为历史任务），无法加载原始文件"
            />
          )}
          {mode === 'pdf' && pdfState && (
            <div
              ref={scrollRef}
              style={{
                maxHeight: '68vh',
                overflow: 'auto',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                background: '#525659',
                padding: '8px 0',
              }}
            >
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
                {pdfState.sizes.map((sz, i) => {
                  const pageNum = i + 1
                  return (
                    <div
                      key={pageNum}
                      data-page={pageNum}
                      ref={(el) => {
                        if (el) pdfPageRefs.current.set(pageNum, el)
                        else pdfPageRefs.current.delete(pageNum)
                      }}
                      style={{
                        position: 'relative',
                        width: sz.width,
                        height: sz.height,
                        background: '#fff',
                        boxShadow: '0 1px 4px rgba(0,0,0,0.35)',
                        overflow: 'hidden',
                      }}
                    />
                  )
                })}
              </div>
            </div>
          )}
          {mode === 'xlsx' && sheets.length > 0 && (
            <Tabs
              size="small"
              activeKey={activeSheet || undefined}
              onChange={setActiveSheet}
              items={sheets.map((s) => ({
                key: s.name,
                label: s.name,
                children: (
                  <div
                    ref={s.name === activeSheet ? containerRef : undefined}
                    style={{ maxHeight: '66vh', overflow: 'auto', border: '1px solid #f0f0f0', borderRadius: 6 }}
                    // eslint-disable-next-line react/no-danger
                    dangerouslySetInnerHTML={{ __html: s.html }}
                  />
                ),
              }))}
            />
          )}
          {mode === 'docx' && (
            <div
              ref={mode === 'docx' ? containerRef : undefined}
              className="src-docx-container"
              style={{
                maxHeight: '68vh',
                overflow: 'auto',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                background: '#fff',
              }}
            />
          )}
          {mode === 'text' && textPreview && (
            <div style={{ maxHeight: '68vh', overflow: 'auto', border: '1px solid #f0f0f0', borderRadius: 6 }}>
              <HighlightedText preview={textPreview} />
            </div>
          )}
        </>
      )}
    </Modal>
  )
}
