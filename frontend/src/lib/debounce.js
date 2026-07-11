export function createDebouncedTask(task, delay = 250) {
  let timer = null

  function cancel() {
    if (timer === null) return
    clearTimeout(timer)
    timer = null
  }

  function schedule(...args) {
    cancel()
    timer = setTimeout(() => {
      timer = null
      task(...args)
    }, delay)
  }

  function run(...args) {
    cancel()
    return task(...args)
  }

  return { cancel, run, schedule }
}
