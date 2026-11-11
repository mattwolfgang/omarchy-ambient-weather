import QtQuick
import QtQuick.Controls as Controls
import qs.Ui as Ui

// A text field that never lets Qt read the clipboard itself. Qt's paste reads
// the whole clipboard offer before the field sees it, with no size or time
// limit, inside the shell; another program owning the clipboard could stream
// text into it without end. Here every paste gesture (Ctrl+V and its
// variants, Shift+Insert, the Paste key, the context menu, and middle-click
// for the primary selection) runs `fetch.py --paste` in a child process
// instead, which reads at most 4 KiB within 2 seconds, and only that text is
// inserted. Text dragged onto the field is discarded unread.
//
// A reply is inserted only if nothing happened since the paste was asked
// for: each request gets a number, and typing, moving the cursor, changing
// the selection, or leaving the field moves the number on and stops the
// helper, so a late reply is dropped rather than compared against the text.
Ui.TextField {
  id: root

  // Path to fetch.py.
  property string helperScript: ""
  property string pasteError: ""
  property int pasteRequest: 0
  property int pendingRequest: -1
  property int pasteStart: 0
  property int pasteEnd: 0
  property bool applyingPaste: false
  readonly property bool pasteBusy: pasteHelper.waiting

  maximumLength: 256

  function invalidatePaste() {
    if (applyingPaste) return
    pasteRequest++
    pendingRequest = -1
    if (pasteHelper.waiting) pasteHelper.cancel()
  }

  function requestPaste(primary) {
    if (!enabled || readOnly || pasteBusy || helperScript === "") return
    forceActiveFocus()
    pasteError = ""
    pasteStart = Math.min(selectionStart, selectionEnd)
    pasteEnd = Math.max(selectionStart, selectionEnd)
    pasteRequest++
    pendingRequest = pasteRequest
    if (!pasteHelper.run(["--paste", primary ? "primary" : "clipboard"], "")) {
      pendingRequest = -1
    }
  }

  function failPaste(message) {
    pendingRequest = -1
    pasteError = message
    errorTimer.restart()
  }

  function receivePaste(value, error) {
    var request = pendingRequest
    pendingRequest = -1
    if (request < 0 || request !== pasteRequest || !activeFocus || !enabled || readOnly) return
    if (!value || value.ok !== true || typeof value.text !== "string") {
      failPaste(value && typeof value.error === "string" ? value.error.slice(0, 200)
        : (error === "Timed out" ? "The clipboard didn't answer in time" : "Couldn't paste the clipboard"))
      return
    }
    var room = Math.max(0, maximumLength - (length - (pasteEnd - pasteStart)))
    var incoming = value.text.slice(0, room)
    applyingPaste = true
    remove(pasteStart, pasteEnd)
    insert(pasteStart, incoming)
    cursorPosition = pasteStart + incoming.length
    applyingPaste = false
    pasteRequest++
  }

  onTextEdited: invalidatePaste()
  onCursorPositionChanged: invalidatePaste()
  onSelectionStartChanged: invalidatePaste()
  onSelectionEndChanged: invalidatePaste()
  onActiveFocusChanged: if (!activeFocus) invalidatePaste()
  onEnabledChanged: invalidatePaste()

  Keys.priority: Keys.BeforeItem
  Keys.onPressed: function(event) {
    var ctrl = (event.modifiers & Qt.ControlModifier) !== 0
    var shift = (event.modifiers & Qt.ShiftModifier) !== 0
    if (event.matches(StandardKey.Paste) || event.key === Qt.Key_Paste
        || (ctrl && event.key === Qt.Key_V) || (shift && event.key === Qt.Key_Insert)) {
      event.accepted = true
      root.requestPaste(false)
    }
  }

  // Takes the whole middle-button gesture before the field can paste the
  // primary selection, and the right button for the menu below.
  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.MiddleButton | Qt.RightButton
    cursorShape: Qt.IBeamCursor
    onPressed: function(mouse) {
      root.forceActiveFocus()
      if (mouse.button === Qt.MiddleButton) {
        root.cursorPosition = root.positionAt(mouse.x, mouse.y)
        root.requestPaste(true)
      } else {
        menu.popup()
      }
    }
  }

  // Accepted so nothing else reads a drop, and never read here.
  DropArea {
    anchors.fill: parent
    onEntered: function(drag) { drag.accepted = true }
    onDropped: function(drop) { drop.accepted = true }
  }

  // Replaces the style's menu, whose Paste would use Qt's own reader.
  Controls.ContextMenu.menu: menu
  Controls.Menu {
    id: menu
    Controls.MenuItem { text: qsTr("Cut"); enabled: root.selectedText.length > 0 && !root.readOnly && !root.password; onTriggered: root.cut() }
    Controls.MenuItem { text: qsTr("Copy"); enabled: root.selectedText.length > 0 && !root.password; onTriggered: root.copy() }
    Controls.MenuItem { text: qsTr("Paste"); enabled: !root.readOnly && !root.pasteBusy; onTriggered: root.requestPaste(false) }
    Controls.MenuItem { text: qsTr("Select all"); onTriggered: root.selectAll() }
  }

  // The helper stops itself after 2 seconds; this deadline is the backstop.
  Helper {
    id: pasteHelper
    script: root.helperScript
    timeoutMs: 4000
    replyLimit: 64 * 1024
    onDone: function(data, error) { root.receivePaste(data, error) }
  }

  Timer {
    id: errorTimer
    interval: 6000
    onTriggered: root.pasteError = ""
  }
}
