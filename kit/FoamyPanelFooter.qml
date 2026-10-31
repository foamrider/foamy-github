// foamy-kit 0.4.2 62abe59f3f9762b3d8a1526be20572aa7400d58b883ad85d24199eef069532fb — do not edit; change foamy-kit and run sync
import QtQuick
import qs.Commons as Commons
import qs.Ui

// The popup footer: a hairline, then the source or a leading action on the
// reading start and, on the end, the freshness line and the refresh button
// (foamy-clock's "Calendar · Updated 3 minutes ago ⟳" is the reference).
//
//   FoamyPanelFooter {
//     width: parent.width                       // inside the padded body column
//     source: FoamyI18n.t("MET Norway")         // or a leading action:
//     // FoamyAction { iconName: "calendar"; label: FoamyI18n.t("Calendar"); foreground: FoamyPalette.secondaryText; onClicked: … }
//     status: FoamyI18n.tn("Updated %1 minutes ago", minutes)
//     refreshing: service.loading
//     refreshHasCursor: cursor.at(refreshStop)
//     onRefreshHovered: function(hovered) { if (hovered) cursor.hover(refreshStop) }
//     onRefreshClicked: service.refresh()
//   }
//
// Leading items (the default property) replace `source`. The status is one
// line, bodySmall, secondary (`statusError` paints it urgent); it elides
// and, if `statusTooltip` is set, shows that short hint on hover.
// `statusClickable` makes it a link (statusClicked). The refresh button
// spins while `refreshing` and is inert then; `showRefresh: false` hides it.
// No horizontal padding of its own: place it in the padded body, or set
// `horizontalPadding` when the body runs edge to edge.
Column {
  id: root

  property string source: ""
  property string status: ""
  property bool statusError: false
  property string statusTooltip: ""
  property bool statusClickable: false
  property bool showRefresh: true
  property bool refreshing: false
  property bool refreshEnabled: true
  property bool refreshHasCursor: false
  property string refreshTooltip: refreshing ? FoamyI18n.t("Refreshing…") : FoamyI18n.t("Refresh")
  property bool divider: true
  property real horizontalPadding: 0
  property alias refreshButton: refreshButton
  default property alias leading: leadingRow.data

  signal refreshClicked()
  signal refreshHovered(bool hovered)
  signal statusClicked()

  spacing: FoamyStyle.gapLarge
  bottomPadding: FoamyStyle.gapSmall

  Rectangle {
    visible: root.divider
    x: root.horizontalPadding
    width: root.width - root.horizontalPadding * 2
    height: 1
    color: FoamyPalette.divider
  }

  Item {
    id: line
    x: root.horizontalPadding
    width: root.width - root.horizontalPadding * 2
    height: FoamyStyle.actionSize

    Row {
      id: leadingRow
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      spacing: FoamyStyle.gap
    }
    Text {
      id: sourceText
      visible: leadingRow.children.length === 0 && root.source !== ""
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Math.min(implicitWidth, line.width / 2)
      text: root.source
      textFormat: Text.PlainText
      elide: Text.ElideRight
      horizontalAlignment: Text.AlignLeft
      color: FoamyPalette.secondaryText
      font.family: FoamyStyle.fontFamily
      font.pixelSize: FoamyStyle.font.bodySmall
    }
    Text {
      id: statusText
      objectName: "foamyFooterStatus"
      anchors.left: leadingRow.children.length > 0 ? leadingRow.right : (sourceText.visible ? sourceText.right : parent.left)
      anchors.leftMargin: FoamyStyle.gapLarge
      anchors.right: refreshButton.visible ? refreshButton.left : parent.right
      anchors.rightMargin: refreshButton.visible ? FoamyStyle.gapSmall : 0
      anchors.verticalCenter: parent.verticalCenter
      text: root.status
      textFormat: Text.PlainText
      elide: Text.ElideRight
      horizontalAlignment: Text.AlignRight
      color: root.statusError ? Commons.Color.urgent : FoamyPalette.secondaryText
      font.family: FoamyStyle.fontFamily
      font.pixelSize: FoamyStyle.font.bodySmall
      font.underline: root.statusClickable && statusMouse.containsMouse
      Accessible.role: root.statusError ? Accessible.AlertMessage : Accessible.StaticText
      Accessible.name: root.status
      MouseArea {
        id: statusMouse
        anchors.fill: parent
        hoverEnabled: root.statusClickable || root.statusTooltip !== ""
        cursorShape: root.statusClickable ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: if (root.statusClickable) root.statusClicked()
      }
      PanelToolTip {
        visible: statusMouse.containsMouse && root.statusTooltip !== ""
        text: root.statusTooltip
        fontFamily: FoamyStyle.fontFamily
      }
    }
    FoamyAction {
      id: refreshButton
      objectName: "foamyFooterRefresh"
      visible: root.showRefresh
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      iconName: "refresh-cw"
      foreground: FoamyPalette.secondaryText
      tooltipText: root.refreshTooltip
      actionEnabled: root.refreshEnabled && !root.refreshing
      spinning: root.refreshing
      hasCursor: root.refreshHasCursor
      onClicked: root.refreshClicked()
      onHoveredChanged: root.refreshHovered(hovered)
    }
  }
}
