import QtQuick
import QtQuick.Dialogs

Window {
  title: "Choose a repository folder"
  width: 875
  height: 600
  visible: true
  opacity: 0
  flags: Qt.Dialog
  Component.onCompleted: picker.open()

  FolderDialog {
    id: picker
    title: "Choose a repository folder"
    acceptLabel: "Choose"
    onAccepted: {
      console.log("GITHUB_FOLDER=" + String(selectedFolder))
      Qt.quit()
    }
    onRejected: Qt.quit()
  }
}
