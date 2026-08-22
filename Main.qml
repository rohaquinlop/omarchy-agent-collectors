import QtQuick
import Quickshell
import Quickshell.Io

// Service side of rohaquinlop.agent-collectors. Runs bin/agent-collectors once at
// startup and then on an interval; the engine does the rest. The stock
// omarchy.agents widget picks up whatever records land in the shared usage
// directory, so no display code lives here.
Item {
  id: root

  readonly property string pluginDir: Quickshell.env("HOME") + "/.config/omarchy/plugins/rohaquinlop.agent-collectors"
  readonly property string engine: pluginDir + "/bin/agent-collectors"
  readonly property int refreshIntervalSec: 900

  Process {
    id: collectorProcess
    running: false

    readonly property int maxErrBytes: 1048576

    // SplitParser with an empty splitMarker delivers raw read chunks;
    // StdioCollector would buffer everything until process end, and a
    // newline-splitting SplitParser would buffer one unbounded newline-free
    // line before its onRead check could ever run. We split and cap in QML:
    // the buffer is cut off and the parser detached as soon as it crosses
    // the byte cap, whatever the line lengths.
    stderr: SplitParser {
      id: errParser
      splitMarker: ""
      property string buf: ""

      onRead: function(data) {
        if (collectorProcess.stderr === null) return
        errParser.buf += data
        if (errParser.buf.length > collectorProcess.maxErrBytes) {
          collectorProcess.stderr = null
          console.warn("agent-collectors", "stderr cap exceeded; detached")
          return
        }
        while (true) {
          const idx = errParser.buf.indexOf("\n")
          if (idx < 0) break
          const line = errParser.buf.slice(0, idx)
          errParser.buf = errParser.buf.slice(idx + 1)
          if (line.trim() !== "") console.warn("agent-collectors", line.trim())
        }
      }
    }

    onExited: function(exitCode) {
      if (exitCode !== 0) console.warn("agent-collectors", "engine exited with code", exitCode)
    }
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: false
    onTriggered: root.run()
  }

  // One run shortly after shell start; agents may have been used since the
  // last scheduled run.
  Timer {
    interval: 15000
    running: true
    repeat: false
    onTriggered: root.run()
  }

  function run() {
    if (collectorProcess.running) return
    errParser.buf = ""
    if (collectorProcess.stderr === null) collectorProcess.stderr = errParser
    collectorProcess.command = [root.engine]
    collectorProcess.running = true
  }
}
