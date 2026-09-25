import QtQuick
import QtQuick.Dialogs

Window {
  id: root
  readonly property var labels: Qt.application.arguments.slice(-2)
  title: labels[0]
  width: 875
  height: 600
  visible: true
  opacity: 0
  flags: Qt.Dialog
  Component.onCompleted: picker.open()

  FolderDialog {
    id: picker
    title: root.title
    acceptLabel: root.labels[1]
    onAccepted: {
      console.log("GITHUB_FOLDER=" + String(selectedFolder))
      Qt.quit()
    }
    onRejected: Qt.quit()
  }
}
