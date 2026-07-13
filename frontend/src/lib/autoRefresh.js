import { onBeforeUnmount, onMounted } from 'vue'

const DEFAULT_INTERVAL = 30_000

export function useAutoRefresh(task, { interval = DEFAULT_INTERVAL, immediate = true } = {}) {
  let timer = null
  let pending = null
  let queuedArgs = null

  function refresh(...args) {
    if (pending) {
      queuedArgs = args
      return pending
    }
    pending = Promise.resolve()
      .then(() => task(...args))
      .finally(() => {
        pending = null
        if (queuedArgs) {
          const nextArgs = queuedArgs
          queuedArgs = null
          refresh(...nextArgs)
        }
      })
    return pending
  }

  function start() {
    stop()
    if (immediate) refresh(false)
    timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') refresh(true)
    }, interval)
  }

  function stop() {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
    queuedArgs = null
  }

  onMounted(start)
  onBeforeUnmount(stop)

  return { refresh, start, stop }
}
