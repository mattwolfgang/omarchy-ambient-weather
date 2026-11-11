import QtQuick
import Quickshell.Io

// Runs one fetch.py request and reports its JSON reply exactly once through
// done(data, error): data is the parsed object, or null with an error message
// when the helper failed to start, exited without a usable reply, sent more
// than replyLimit characters, or ran past timeoutMs (then it is stopped).
// A reply that arrives after cancel() or the deadline is ignored.
Process {
  id: helper

  property string script: ""
  property int timeoutMs: 20000
  property int replyLimit: 256 * 1024
  property string input: ""
  property bool waiting: false

  signal done(var data, string error)

  function run(args, stdinText) {
    if (waiting || running || script === "") return false
    input = stdinText || ""
    waiting = true
    command = ["python3", "-I", script].concat(args || [])
    watchdog.restart()
    running = true
    return true
  }

  function cancel() {
    waiting = false
    watchdog.stop()
    settle.stop()
    if (running) running = false
  }

  function finish(data, error) {
    if (!waiting) return
    waiting = false
    watchdog.stop()
    settle.stop()
    helper.done(data, error)
  }

  function deliver(reply) {
    if (!waiting) return
    var raw = String(reply || "")
    if (raw.length > replyLimit) {
      finish(null, "Reply too large")
      return
    }
    var data = null
    try { data = JSON.parse(raw) } catch (e) {}
    if (data === null || typeof data !== "object" || Array.isArray(data)) finish(null, "No reply")
    else finish(data, "")
  }

  stdinEnabled: true
  onStarted: {
    if (input !== "") write(input + "\n")
    input = ""
  }

  stdout: StdioCollector {
    waitForEnd: true
    onStreamFinished: helper.deliver(text)
  }

  // Also covers a helper that never started, which clears running without
  // an exit. Output can land just after running clears; give it a moment.
  onRunningChanged: if (!running && waiting) settle.restart()

  property Timer settle: Timer {
    interval: 300
    onTriggered: helper.finish(null, "The helper stopped without answering")
  }

  property Timer watchdog: Timer {
    interval: helper.timeoutMs
    onTriggered: {
      if (helper.running) helper.running = false
      helper.finish(null, "Timed out")
    }
  }
}
