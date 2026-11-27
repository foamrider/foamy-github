// foamy-kit 0.17.0 3c8589427db724d6a550d8b3918f4aae9e7beb2b604256bfd8e6d18a7ef91ac5 — do not edit; change foamy-kit and run sync
import QtQuick
import qs.Commons as Commons
import qs.Ui
import "FoamyText.js" as FoamyText

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
// line, bodySmall, secondary (`statusError` paints it in the error colour,
// readable on the popup surface); it elides
// and, if `statusTooltip` is set, shows that short hint on hover.
// `statusClickable` makes it a link (statusClicked). The refresh button
// spins while `refreshing` and is inert then; `showRefresh: false` hides it.
// `cursorByKeys: cursor.byKeys` gives it the keys' accent border while it
// has the cursor, also while inert (FoamyAction's `cursorByKeys`; set a
// leading action's own).
// No horizontal padding of its own: place it in the padded body, or set
// `horizontalPadding` when the body runs edge to edge. The row sits
// `spacing` under the hairline and `spacing` above the footer's bottom, so it
// is centred between them; put nothing under it (the popup ends there).
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
  // The keys moved the cursor (FoamyCursor.byKeys): the refresh button's.
  property bool cursorByKeys: false
  property string refreshTooltip: refreshing ? FoamyI18n.t("Refreshing…") : FoamyI18n.t("Refresh")
  property bool divider: true
  property real horizontalPadding: 0
  property alias refreshButton: refreshButton
  default property alias leading: leadingRow.data

  signal refreshClicked
  signal refreshHovered(bool hovered)
  signal statusClicked

  spacing: FoamyStyle.gapLarge
  bottomPadding: spacing

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
      color: root.statusError ? FoamyPalette.onSurface(FoamyPalette.error) : FoamyPalette.secondaryText
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
        onClicked: if (root.statusClickable)
          root.statusClicked()
      }
      PanelToolTip {
        id: statusTip
        visible: statusMouse.containsMouse && root.statusTooltip !== ""
        // The host's tooltip is as wide as its text and wraps only under a cap,
        // so a long label or path would cover the whole popup. Capped through
        // implicitWidth, which the tooltip centers itself by (as the host does).
        implicitWidth: Math.min(implicitContentWidth + leftPadding + rightPadding, Commons.Style.space(320))
        // The caller's text may be app text with no length bound (a path, an
        // error's detail): 3 lines of 200 characters keep an ordinary path whole
        // and the wrapped tooltip a few lines high, with a visible "…" beyond.
        text: FoamyText.cap(root.statusTooltip, 3, 200)
        fontFamily: FoamyStyle.fontFamily

        // The host's text wraps at word boundaries only, so a name typed
        // without spaces would run past the capped width.
        Binding {
          target: statusTip.contentItem
          property: "wrapMode"
          value: Text.Wrap
        }
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
      cursorByKeys: root.cursorByKeys
      onClicked: root.refreshClicked()
      onHoveredChanged: root.refreshHovered(hovered)
    }
  }
}
