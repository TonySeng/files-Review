import { useEffect, useState } from 'react'
import { Button } from 'antd'
import { ArrowUpOutlined } from '@ant-design/icons'

/**
 * 全局「回到顶部」悬浮按钮。
 * 滚动超过阈值后出现在视口右下角，点击平滑滚回页面顶部。
 * 由 App.tsx 统一挂载一次，所有登录后页面表现一致。
 */
export default function BackToTop({ threshold = 320 }: { threshold?: number }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const onScroll = () => setVisible(window.scrollY > threshold)
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
  }, [threshold])

  if (!visible) return null
  return (
    <Button
      className="back-to-top"
      type="primary"
      shape="circle"
      size="large"
      icon={<ArrowUpOutlined />}
      aria-label="回到顶部"
      title="回到顶部"
      onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
    />
  )
}
