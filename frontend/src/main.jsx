import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import { locale } from './i18n'
import * as observability from './observability'
import './index.css'

observability.start()
document.documentElement.lang = locale

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A 401 or a 404 will not become a 200 by asking again; a dropped connection or a 503
      // might. Retrying everything three times is how a signed-out session turns into four
      // seconds of spinner before the sign-in screen appears.
      retry: (failureCount, error) => {
        const status = /** @type {{ status?: number }} */ (error).status
        return failureCount < 2 && (!status || status >= 500)
      },
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
)
