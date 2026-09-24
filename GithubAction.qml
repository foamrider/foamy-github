import QtQuick
import qs.Commons
import qs.Ui

Rectangle {
  id: root
  property string iconName: "settings"
  property string label: ""
  property string tooltipText: ""
  property real iconSize: Style.space(15)
  property Item keyTarget: null
  property color foreground: Color.foreground
  property string fontFamily: "sans-serif"
  property bool actionEnabled: true
  signal clicked()
  implicitWidth: label ? content.implicitWidth + Style.space(12) : Style.space(26)
  implicitHeight: Style.space(28)
  radius: Style.space(6)
  opacity: enabled && actionEnabled ? 1 : 0.4
  color: actionEnabled && (mouse.containsMouse || activeFocus) ? Qt.rgba(foreground.r, foreground.g, foreground.b, 0.08) : "transparent"
  activeFocusOnTab: actionEnabled
  border.width: activeFocus ? 1 : 0
  border.color: Color.accent
  Accessible.role: Accessible.Button
  Accessible.name: tooltipText || label
  Accessible.onPressAction: if (enabled && actionEnabled) clicked()
  Keys.onReturnPressed: if (enabled && actionEnabled) clicked()
  Keys.onEnterPressed: if (enabled && actionEnabled) clicked()
  Keys.onSpacePressed: if (enabled && actionEnabled) clicked()
  Keys.forwardTo: keyTarget ? [keyTarget] : []
  Row {
    id: content
    anchors.centerIn: parent
    spacing: Style.space(7)
    GithubIcon { width: root.iconSize; height: width; color: root.foreground; name: root.iconName; anchors.verticalCenter: parent.verticalCenter }
    Text { visible: root.label !== ""; text: root.label; textFormat: Text.PlainText; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.space(11) }
  }
  MouseArea { id: mouse; anchors.fill: parent; hoverEnabled: true; cursorShape: root.actionEnabled ? Qt.PointingHandCursor : Qt.ArrowCursor; onClicked: if (root.actionEnabled) root.clicked() }
  PanelToolTip {
    visible: mouse.containsMouse && root.tooltipText !== ""
    text: root.tooltipText
    fontFamily: root.fontFamily
  }
}
