import { useState, useEffect, useRef, useCallback } from 'react'
import * as api from './api'
import { useDocuments } from './hooks/useDocuments'
import { useChat } from './hooks/useChat'
import { useAuth } from './hooks/useAuth'

import LoadingScreen from './components/LoadingScreen'
import HeroHeader from './components/HeroHeader'
import SuggestionList from './components/SuggestionList'
import ChatWindow, { Suggestions } from './components/ChatWindow'
import Composer from './components/Composer'
import ProgressToast from './components/ProgressToast'
import DocumentDrawer, { DOC_DRAG_TYPE } from './components/DocumentDrawer'
import HistoryPanel from './components/HistoryPanel'
import IconPopover from './components/IconPopover'
import AuthScreen from './components/AuthScreen'
import AccountChip from './components/AccountChip'

function DocsIcon() {
  return (
    <svg viewBox="0 -960 960 960" fill="currentColor">
      <path d="M320-240h320v-80H320v80Zm0-160h320v-80H320v80ZM240-80q-33 0-56.5-23.5T160-160v-640q0-33 23.5-56.5T240-880h320l240 240v480q0 33-23.5 56.5T720-80H240Zm280-520v-200H240v640h480v-440H520Z" />
    </svg>
  )
}

function HistoryIcon() {
  return (
    <svg viewBox="0 -960 960 960" fill="currentColor">
      <path d="M480-120q-138 0-240.5-91.5T122-440h82q14 104 92.5 172T480-200q117 0 198.5-81.5T760-480q0-117-81.5-198.5T480-760q-69 0-129 32t-101 88h110v80H120v-240h80v94q51-64 124.5-103T480-840q75 0 140.5 28.5t114 77q48.5 48.5 77 114T840-480q0 75-28.5 140.5t-77 114q-48.5 48.5-114 77T480-120Zm112-192L440-464v-216h80v184l128 128-56 56Z" />
    </svg>
  )
}

export default function App() {
  const [ready, setReady] = useState(false)       // backend health confirmed
  const [bgStyle, setBgStyle] = useState({})
  const [modes, setModes] = useState(['hybrid', 'dense', 'hybrid_rerank'])
  const [mode, setMode] = useState('hybrid_rerank')
  const [docsOpen, setDocsOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [activeSource, setActiveSource] = useState(null) // { msgIndex, sourceId }

  const docHook = useDocuments()
  const chatHook = useChat()
  const auth = useAuth()

  const chatBottomRef = useRef(null)
  const composerRef = useRef(null)

  // ── Bootstrap ──────────────────────────────────────────────────────────────
  useEffect(() => {
    setBgStyle({ '--bg-image': `url("/background.jpg")` })
  }, [])

  // The corpus is per-tenant, so it is loaded after sign-in and dropped on sign-out.
  useEffect(() => {
    if (!auth.user) {
      setReady(false)
      return
    }
    let cancelled = false
    async function bootstrap() {
      while (!cancelled) {
        try {
          await api.getHealth()
          break
        } catch {
          await new Promise((r) => setTimeout(r, 1500))
        }
      }
      if (cancelled) return
      const cfg = await api.getConfig().catch(() => null)
      if (cfg) {
        setModes(cfg.retrieval_modes)
        setMode(cfg.default_mode)
      }
      await docHook.loadFromServer()
      if (!cancelled) setReady(true)
    }
    bootstrap()
    return () => { cancelled = true }
  }, [auth.user])

  // ── Auto-scroll ────────────────────────────────────────────────────────────
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatHook.messages, chatHook.busy])

  // ── Auto-select top source when a new answer arrives ──────────────────────
  useEffect(() => {
    const msgs = chatHook.messages
    for (let i = msgs.length - 1; i >= 0; i--) {
      const msg = msgs[i]
      if (msg.role === 'assistant' && msg.answer?.status === 'answered') {
        const sources = msg.answer.sources || []
        if (sources.length > 0) setActiveSource({ msgIndex: i, sourceId: sources[0].id })
        return
      }
    }
  }, [chatHook.messages])

  const handleCite = useCallback((msgIndex, sourceId) => {
    setActiveSource({ msgIndex, sourceId })
  }, [])

  // ── Submit question ────────────────────────────────────────────────────────
  const handleSubmit = useCallback((question) => {
    chatHook.submit(question, docHook.effectiveIds, mode)
  }, [chatHook, docHook.effectiveIds, mode])

  const handleRetry = useCallback((question) => {
    chatHook.retry(question, docHook.effectiveIds, mode)
  }, [chatHook, docHook.effectiveIds, mode])

  const showHero = chatHook.messages.length === 0 && !chatHook.busy

  const lastAnswer = (() => {
    const msgs = chatHook.messages
    const last = msgs[msgs.length - 1]
    return last?.role === 'assistant' && last.answer?.status === 'answered' ? last.answer : null
  })()

  const handleDocDrop = useCallback((e) => {
    const docId = e.dataTransfer.getData(DOC_DRAG_TYPE)
    if (docId) {
      e.preventDefault()
      docHook.setScope(docId, true)
    }
  }, [docHook])

  if (!auth.user) {
    return (
      <div className="app-bg" style={bgStyle}>
        {auth.checking ? (
          <LoadingScreen loaded={false} />
        ) : (
          <AuthScreen error={auth.error} onSignIn={auth.signIn} onSignUp={auth.signUp} />
        )}
      </div>
    )
  }

  return (
    <div className="app-bg" style={ready ? bgStyle : undefined}>
      {!ready ? (
        <LoadingScreen loaded={false} />
      ) : (
        <div
          className={`main-container${showHero ? ' main-container-centered' : ''}`}
          onDragOver={(e) => e.preventDefault()}
          onDrop={handleDocDrop}
        >
          <AccountChip user={auth.user} onSignOut={auth.signOut} />

          {showHero && <HeroHeader />}

          <ChatWindow
            messages={chatHook.messages}
            activeSource={activeSource}
            onRetry={handleRetry}
            onCite={handleCite}
          />

          <div ref={chatBottomRef} />

          <Composer
            ref={composerRef}
            busy={chatHook.busy}
            hasReady={docHook.hasReady}
            docs={docHook.docs}
            scopedIds={docHook.scopedIds}
            allSelected={docHook.allSelected}
            modes={modes}
            mode={mode}
            onModeChange={setMode}
            onSubmit={handleSubmit}
            canUpload={auth.canUpload}
            onUpload={docHook.uploadFile}
            onToggleScope={docHook.toggleScope}
          />

          {!chatHook.busy && lastAnswer && (
            <Suggestions answer={lastAnswer} onSubmit={handleSubmit} />
          )}

          <div className={`below-composer-icons${showHero ? '' : ' below-composer-icons-dock'}`}>
            <IconPopover
              icon={<DocsIcon />}
              label="Documents"
              open={docsOpen}
              onToggle={() => setDocsOpen((o) => !o)}
              onClose={() => setDocsOpen(false)}
            >
              <DocumentDrawer
                docs={docHook.docs}
                scopedIds={docHook.scopedIds}
                allSelected={docHook.allSelected}
                busy={chatHook.busy}
                canRemove={auth.canUpload}
                onClose={() => setDocsOpen(false)}
                onRemove={docHook.removeDoc}
                onToggleScope={docHook.toggleScope}
                onSelectAll={docHook.selectAll}
                onClearScope={docHook.clearScope}
              />
            </IconPopover>

            <IconPopover
              icon={<HistoryIcon />}
              label="History"
              open={historyOpen}
              onToggle={() => setHistoryOpen((o) => !o)}
              onClose={() => setHistoryOpen(false)}
            >
              <HistoryPanel
                messages={chatHook.messages}
                onClose={() => setHistoryOpen(false)}
              />
            </IconPopover>
          </div>

          {showHero && (
            <SuggestionList
              onAttach={() => composerRef.current?.openAttach()}
              onFocusInput={() => composerRef.current?.focusInput()}
              onOpenMode={() => composerRef.current?.openModeMenu()}
            />
          )}
        </div>
      )}

      <ProgressToast uploads={docHook.uploads} />
    </div>
  )
}
