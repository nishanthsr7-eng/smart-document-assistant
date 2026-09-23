export default function LoadingScreen({ loaded }) {
  return (
    <div className="loading-screen">
      <div className="hero">
        <h1 className="hero-title">Smart Document Assistant</h1>
        <p className="hero-sub">Ask · Cite · Verify</p>
      </div>

      {loaded ? (
        <div className="loaded-badge">
          <span className="loaded-check">✓</span>
          <span>Ready</span>
        </div>
      ) : (
        <>
          <div className="loading-orb">
            <div className="orb-ring" />
            <div className="orb-ring" />
          </div>
          <span className="loading-text">Loading models…</span>
        </>
      )}
    </div>
  )
}
