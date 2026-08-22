import QtQuick
import Quickshell
import Quickshell.Io

// Service side of roha.agent-collectors. Runs bin/agent-collectors once at
// startup and then on an interval; the engine does the rest. The stock
// omarchy.agents widget picks up whatever records land in the shared usage
// directory, so no display code lives here.
Item {
  id: root

  readonly property string pluginDir: Quickshell.env("HOME") + "/.config/omarchy/plugins/roha.agent-collectors"
  readonly property string engine: pluginDir + "/bin/agent-collectors"
  readonly property int refreshIntervalSec: 900

  Process {
    id: collectorProcess
    running: false

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("agent-collectors", text.trim())
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
    collectorProcess.command = [root.engine]
    collectorProcess.running = true
  }

  Component.onCompleted: {
    if (!Quickshell.env("HOME")) return
  }
}
