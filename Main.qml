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
    property int errBytes: 0

    // SplitParser streams stderr line by line; StdioCollector would buffer
    // everything until process end. A hard byte cap detaches the parser so a
    // pathological engine can never force unbounded allocation here.
    stderr: SplitParser {
      id: errParser
      onRead: function(data) {
        if (collectorProcess.errBytes > collectorProcess.maxErrBytes) {
          collectorProcess.stderr = null
          console.warn("agent-collectors", "stderr cap exceeded; detached")
          return
        }
        collectorProcess.errBytes += data.length
        if (data.trim() !== "") console.warn("agent-collectors", data.trim())
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
    collectorProcess.errBytes = 0
    if (collectorProcess.stderr === null) collectorProcess.stderr = errParser
    collectorProcess.command = [root.engine]
    collectorProcess.running = true
  }
}
