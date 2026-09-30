import { useQuery } from '@tanstack/react-query'
import * as api from '../api'

/**
 * What the SPA needs from the server before it can show anything: the backend answering at all,
 * and the retrieval modes it supports.
 *
 * Health is retried forever on purpose -- a cold start loads models and can take a minute, and
 * the honest thing to show meanwhile is the loading screen, not an error the user cannot act on.
 * Everything else in the app retries twice and gives up.
 */
export function useBootstrap(enabled) {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: api.getHealth,
    enabled,
    retry: true,
    retryDelay: 1500,
    staleTime: Infinity,
  })

  const config = useQuery({
    queryKey: ['config'],
    queryFn: api.getConfig,
    enabled: enabled && health.isSuccess,
    staleTime: Infinity,
  })

  return {
    ready: health.isSuccess && config.isSuccess,
    modes: config.data?.retrieval_modes,
    defaultMode: config.data?.default_mode,
  }
}
