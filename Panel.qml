import QtQuick
import QtQuick.Controls
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "Preferences.js" as Preferences

Panel {
  id: root
  moduleName: "foamy.github"
  ipcTarget: "foamy.github"
  manageIpc: false

  readonly property string language: Preferences.language(Preferences.languageSetting(settings), Qt.locale().name)
  function tr(label, values) { return Preferences.text(label, language, values) }

  readonly property real controlRadius: Style.space(6)
  property int repoIndex: 0
  property bool cursorActive: false
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color barTextColor: bar ? bar.barForeground : Color.foreground
  readonly property color accent: Color.accent
  readonly property bool lightTheme: Color.popups.background.r + Color.popups.background.g + Color.popups.background.b > 1.5
  readonly property color success: lightTheme ? "#3b6b30" : "#a6e3a1"
  readonly property color warning: lightTheme ? "#886000" : "#f9e2af"
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.tint(Color.popups.background, Qt.rgba(foreground.r, foreground.g, foreground.b, 0.76))
  readonly property color outlineColor: Qt.tint(Color.popups.background, Qt.rgba(foreground.r, foreground.g, foreground.b, 0.22))
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var displayedRepos: github.affectedRepos
  readonly property bool hasFailures: github.sync.failures.length > 0
  readonly property string healthText: {
    if (github.syncing) return root.tr("Synchronizing repositories")
    if (github.lastError !== "") return root.tr("Repository check needs attention")
    if (github.lastChecked === 0) return root.tr("Checking repositories…")
    if (github.status.repoCount === 0) return root.tr("No repositories found")
    if (github.status.affectedCount === 0 && !github.sync.stale && !root.hasFailures) return root.tr("Everything is in sync")
    if (github.status.affectedCount === 0) return root.tr("Working trees are clean")
    return root.tr(github.status.affectedCount === 1 ? "%1 repository needs attention" : "%1 repositories need attention", [github.status.affectedCount])
  }
  readonly property string lastSyncText: root.tr("Last remote sync %1", [Model.relativeTime(github.sync.lastSuccess, undefined, root.tr)])
  readonly property string tooltipText: healthText + "\n" + root.tr("Left: details · Middle: refresh")
  readonly property string updatedText: Model.updatedText(github.lastChecked, footerClock.date.getTime(), root.tr)
  SystemClock { id: footerClock; precision: SystemClock.Minutes }

  property bool editingSettings: false
  property string settingsError: ""
  property string widgetId: ""
  property bool foldersDirty: false
  property var pendingPreferences: ({})
  property string savingPreference: ""
  property int browsingRow: -1
  property bool returningFromBrowser: false
  property int progressStep: 0
  readonly property string statusTitle: github.actionStatus !== "" ? github.actionStatus : root.healthText
  readonly property bool animateTitle: github.syncing || github.repositoryActionRunning
    || (github.refreshing && github.lastChecked === 0)
  readonly property string displayTitle: animateTitle
    ? statusTitle.replace(/(?:…|\.{1,3})$/, "") + [".", "..", "..."][progressStep]
    : statusTitle

  function openSettings() {
    var folders = Model.repositoryFolders(root.settings)
    var result = Model.parseFolderRows(folders)
    foldersDirty = false
    folderRows.clear()
    if (!result.error) result.folders.forEach(function(folder) { folderRows.append(folder) })
    ensureFolderRow()
    settingsError = result.error || ""
    editingSettings = true
    panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  ListModel { id: folderRows }

  function ensureFolderRow() {
    if (folderRows.count === 0) folderRows.append({path: "", depth: 2})
  }

  function browseFolder(index) {
    if (folderPicker.running) return
    browsingRow = index
    returningFromBrowser = true
    // Release the popup's keyboard focus while the separate chooser is open.
    root.close()
    folderPicker.running = true
  }

  function finishFolderBrowser(exitCode, output) {
    var match = String(output).match(/(?:GITHUB_FOLDER=)?(file:\/\/[^\r\n]*)/)
    if (exitCode !== 0) settingsError = "Could not open the folder browser. Enter a path manually."
    else if (match && browsingRow >= 0 && browsingRow < folderRows.count) {
      folderRows.setProperty(browsingRow, "path", decodeURIComponent(match[1].replace(/^file:\/\//, "")))
      foldersDirty = true
      saveFolders()
    }
    root.open()
    Qt.callLater(function() {
      returningFromBrowser = false
      var row = folderRepeater.itemAt(browsingRow)
      if (row) row.focusPath()
      browsingRow = -1
    })
  }

  Process {
    id: folderPicker
    command: ["bash", decodeURIComponent(Qt.resolvedUrl("folder-picker.sh").toString().replace(/^file:\/\//, "")), root.tr("Choose a repository folder"), root.tr("Choose")]
    stdout: StdioCollector { id: folderPickerOutput; waitForEnd: true }
    onExited: function(exitCode) { root.finishFolderBrowser(exitCode, folderPickerOutput.text) }
  }

  function showFolderRow(row) {
    // Newly added rows and keyboard focus can move below the scroll viewport.
    Qt.callLater(function() {
      if (!row) return
      var top = row.mapToItem(panelFlick.contentItem, 0, 0).y
      var bottom = top + row.height
      if (top < panelFlick.contentY) panelFlick.contentY = top
      else if (bottom > panelFlick.contentY + panelFlick.height)
        panelFlick.contentY = bottom - panelFlick.height
    })
  }

  function addFolder() {
    if (folderRows.count >= 32) return
    folderRows.append({path: "", depth: 2})
    settingsError = ""
    Qt.callLater(function() {
      var row = folderRepeater.itemAt(folderRows.count - 1)
      if (row) row.focusPath()
    })
  }

  function closeSettings() {
    if (foldersDirty) saveFolders()
    editingSettings = false
    panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function savePreference(key, value) {
    settingsError = ""
    // Keep the newest edit for each setting while an earlier IPC save finishes.
    pendingPreferences[key] = JSON.stringify(value)
    flushPreferences()
  }

  function flushPreferences() {
    if (preferencesSave.running) return
    var keys = Object.keys(pendingPreferences)
    if (keys.length === 0) return
    if (!widgetId) { settingsError = "Could not read the installed plugin ID."; return }
    savingPreference = keys[0]
    var value = pendingPreferences[savingPreference]
    delete pendingPreferences[savingPreference]
    // Omarchy owns both live settings and shell.json; the space prevents CLI array expansion.
    preferencesSave.command = ["omarchy-shell", "shell", "setBarWidget", widgetId, savingPreference, " " + value, "{}"]
    preferencesSave.running = true
  }

  function saveFolders() {
    var rows = []
    // Blank editor rows are placeholders, not configured directories.
    for (var i = 0; i < folderRows.count; i++) {
      var row = folderRows.get(i)
      if (row.path.trim() !== "") rows.push({path: row.path, depth: row.depth})
    }
    var result = Model.parseFolderRows(rows)
    if (result.error) { settingsError = result.error; return }
    foldersDirty = false
    root.savePreference("repositoryFolders", result.folders)
  }

  Timer {
    interval: 400
    repeat: true
    running: root.opened && root.animateTitle
    onRunningChanged: if (!running) root.progressStep = 0
    onTriggered: root.progressStep = (root.progressStep + 1) % 3
  }

  FileView {
    path: decodeURIComponent(Qt.resolvedUrl("manifest.json").toString().replace(/^file:\/\//, ""))
    onLoaded: {
      try { root.widgetId = String(JSON.parse(text()).id || ""); root.flushPreferences() }
      catch (error) { root.settingsError = "Could not read the installed plugin ID." }
    }
    onLoadFailed: root.settingsError = "Could not read the installed plugin ID."
  }

  Process {
    id: preferencesSave
    stdout: StdioCollector { id: preferencesOutput; waitForEnd: true }
    stderr: StdioCollector { id: preferencesError; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0 || preferencesOutput.text.trim() !== "ok") {
        root.settingsError = String(preferencesError.text || preferencesOutput.text || "Could not save settings. Try again.").trim()
        if (root.savingPreference === "repositoryFolders") root.foldersDirty = true
      }
      Qt.callLater(root.flushPreferences)
    }
  }

  function setRepoCursor(index) {
    if (displayedRepos.length === 0) return
    cursorActive = true
    repoIndex = Math.max(0, Math.min(displayedRepos.length - 1, index))
    scrollCursorIntoView()
  }

  function moveCursor(delta) {
    if (displayedRepos.length === 0 || delta === 0) return
    setRepoCursor(repoIndex + delta)
  }

  function selectedRepo() {
    return displayedRepos.length > 0 ? displayedRepos[Math.max(0, Math.min(repoIndex, displayedRepos.length - 1))] : null
  }

  function viewRepository(repo) {
    if (!repo) return
    root.close()
    github.openRepository(repo)
  }

  function scrollCursorIntoView() {
    if (!repoColumn || repoIndex < 0 || repoIndex >= repoColumn.children.length) return
    Qt.callLater(function() {
      var item = repoColumn.children[root.repoIndex]
      if (!item) return
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var margin = Style.space(6)
      if (top < panelFlick.contentY + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > panelFlick.contentY + panelFlick.height - margin)
        panelFlick.contentY = Math.min(panelFlick.contentHeight - panelFlick.height, bottom + margin - panelFlick.height)
    })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (!opened && editingSettings && foldersDirty) saveFolders()
    if (opened && !returningFromBrowser) {
      editingSettings = false
      cursorActive = false
      repoIndex = 0
      panelFlick.contentY = 0
      github.refresh()
    }
  }
  onDisplayedReposChanged: if (repoIndex >= displayedRepos.length) repoIndex = Math.max(0, displayedRepos.length - 1)

  Service {
    id: github
    settings: root.settings
    language: root.language
  }

  IpcHandler {
    target: root.ipcTarget
    function open() { root.open() }
    function close() { root.close() }
    function show() { root.open() }
    function hide() { root.close() }
    function toggle() { root.toggle() }
    function refresh() { github.fetch(); return "ok" }
    function status() { return root.healthText }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    fontSize: Style.font.body
    labelVisible: false
    hasVisualContent: true
    fixedWidth: button.vertical ? -1 : Math.max(Style.bar.iconSlot,
      statusRow.implicitWidth + Style.bar.iconSlot - Style.bar.iconCanvas)
    tooltipText: root.tooltipText
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) { if (!github.busy) github.fetch() }
      else root.toggle()
    }

    Row {
      id: statusRow
      anchors.centerIn: parent
      spacing: Style.space(1)

      OpticalGlyph {
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        text: github.syncing ? "\uf021" : "\uf09b"
        color: github.syncing ? root.accent : button.foreground
        fontFamily: button.fontFamily
        fontSize: button.fontSize
      }

      Text {
        textFormat: Text.PlainText
        visible: !button.vertical && !github.syncing && github.totals.aheadRepos > 0
        text: github.totals.aheadRepos + "↑"
        color: root.success
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }

      Text {
        textFormat: Text.PlainText
        visible: !button.vertical && !github.syncing && github.totals.behindRepos > 0
        text: github.totals.behindRepos + "↓"
        color: root.accent
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }

      Text {
        textFormat: Text.PlainText
        visible: !button.vertical && !github.syncing && github.totals.dirtyRepos > 0
        text: github.totals.dirtyRepos + "*"
        color: root.warning
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }

      Text {
        textFormat: Text.PlainText
        visible: !button.vertical && !github.syncing && github.totals.failedRepos > 0
        text: github.totals.failedRepos + "!"
        color: root.urgent
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }

      Text {
        textFormat: Text.PlainText
        visible: !button.vertical && !github.syncing && github.sync.stale && github.totals.failedRepos === 0
        text: "?"
        color: root.urgent
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }
    }
  }

  component GithubLabel: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: "sans-serif"
    font.pixelSize: Style.space(13)
  }

  GithubPopup {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    padding: 0
    borderSpec: Border.flat(root.outlineColor, 1)
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editingSettings
      Keys.onEscapePressed: root.editingSettings ? root.closeSettings() : root.close()
      Keys.onTabPressed: function(event) {
        if (root.editingSettings && keyCatcher.activeFocus) settingsBack.forceActiveFocus()
        else event.accepted = false
      }
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dy !== 0 ? dy : dx)
      }
      onActivateRequested: if (root.cursorActive) root.viewRepository(root.selectedRepo())
      onCloseRequested: root.editingSettings ? root.closeSettings() : root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "s" || text === "S") { root.openSettings(); return }
        if ((text === "r" || text === "R") && !github.busy) github.fetch()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        // A successful refresh may shorten the list while it is scrolled.
        onContentHeightChanged: contentY = Math.max(0, Math.min(contentY, contentHeight - height))
        onHeightChanged: contentY = Math.max(0, Math.min(contentY, contentHeight - height))
        ScrollBar.vertical: ScrollBar {
          visible: panelFlick.interactive
          width: Style.space(4)
          padding: 0
          contentItem: Rectangle { implicitWidth: Style.space(4); radius: width / 2; color: root.dim }
          background: Item {}
        }

        Column {
          id: contentColumn
          width: panelFlick.width

          Item {
            width: parent.width
            height: heroContent.implicitHeight + Style.space(42)

            // Use the same static, theme-derived header wash as clock and weather.
            Canvas {
              id: headerBackground
              anchors.fill: parent
              onWidthChanged: requestPaint()
              onHeightChanged: requestPaint()
              onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                var radius = Style.space(13)
                ctx.beginPath()
                ctx.moveTo(radius, 0); ctx.lineTo(width - radius, 0)
                ctx.quadraticCurveTo(width, 0, width, radius)
                ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.lineTo(0, radius)
                ctx.quadraticCurveTo(0, 0, radius, 0); ctx.closePath(); ctx.clip()
                var gradient = ctx.createLinearGradient(0, 0, width * 0.5, height)
                gradient.addColorStop(0, Qt.tint(Color.popups.background, Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.08)))
                gradient.addColorStop(1, Color.popups.background)
                ctx.fillStyle = gradient; ctx.fillRect(0, 0, width, height)
                var glow = ctx.createRadialGradient(width * 0.9, 0, 0, width * 0.9, 0, width * 0.85)
                glow.addColorStop(0, Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.17))
                glow.addColorStop(1, "transparent")
                ctx.fillStyle = glow; ctx.fillRect(0, 0, width, height)
              }
              Connections {
                target: Color
                function onAccentChanged() { headerBackground.requestPaint() }
                function onShellValuesChanged() { headerBackground.requestPaint() }
                function onBackgroundChanged() { headerBackground.requestPaint() }
              }
            }

            Column {
              id: heroContent
              anchors.centerIn: parent
              width: parent.width - Style.space(40)
              spacing: Style.space(18)

              RowLayout {
                width: parent.width
                spacing: Style.space(8)
                Text {
                  text: "\uf09b"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.space(17)
                }
                GithubLabel { text: "GitHub"; Layout.fillWidth: true }
                GithubAction {
                  visible: !root.editingSettings
                  iconName: "settings"
                  tooltipText: root.tr("Settings (S)")
                  foreground: root.dim
                  implicitWidth: Style.space(32)
                  implicitHeight: Style.space(32)
                  radius: Style.space(7)
                  iconSize: Style.space(16)
                  keyTarget: keyCatcher
                  onClicked: root.openSettings()
                }
              }
              GithubLabel {
                id: titleLabel
                width: parent.width
                text: root.displayTitle
                font.pixelSize: Style.space(21)
                wrapMode: Text.WordWrap
              }
              Flow {
                width: parent.width
                spacing: Style.space(18)
                SummaryCount { value: github.totals.aheadRepos; label: root.tr("ahead"); symbol: "↑"; tint: root.success }
                SummaryCount { value: github.totals.behindRepos; label: root.tr("behind"); symbol: "↓"; tint: root.accent }
                SummaryCount { value: github.totals.dirtyRepos; label: root.tr("changed"); symbol: "•"; tint: root.warning }
                SummaryCount { visible: value > 0; value: github.totals.failedRepos; label: root.tr("failed"); symbol: "!"; tint: root.urgent }
              }
            }
          }

          Column {
            visible: root.editingSettings
            width: parent.width - Style.space(40)
            anchors.horizontalCenter: parent.horizontalCenter
            topPadding: Style.space(16)
            bottomPadding: Style.space(20)
            spacing: Style.space(12)
            Row {
              spacing: Style.space(10)
              GithubAction {
                id: settingsBack
                iconName: "arrow-left"
                tooltipText: root.tr("Back")
                foreground: root.dim
                onClicked: root.closeSettings()
              }
              GithubLabel { text: root.tr("Settings"); color: root.dim; anchors.verticalCenter: parent.verticalCenter }
            }
            GithubDropdown {
              cornerRadius: root.controlRadius
              width: parent.width
              label: root.tr("Language")
              fontFamily: "sans-serif"
              value: Preferences.languageSetting(root.settings)
              options: [{value: "system", label: root.tr("Default (system language)")},
                {value: "en", label: "English"}, {value: "nb", label: "Norsk bokmål"}]
              onChanged: function(value) { root.savePreference("language", value) }
              Keys.onEscapePressed: root.closeSettings()
            }
            RowLayout {
              width: parent.width
              GithubLabel { text: root.tr("Repository folders"); Layout.fillWidth: true }
              GithubAction {
                id: addFolderButton
                iconName: "plus"
                tooltipText: folderRows.count >= 32 ? root.tr("You can add up to 32 directories.") : root.tr("Add folder")
                foreground: root.dim
                actionEnabled: folderRows.count < 32 && !folderPicker.running
                onClicked: root.addFolder()
                Keys.onEscapePressed: root.closeSettings()
              }
            }
            Column {
              width: parent.width
              spacing: Style.space(8)
              Repeater {
                id: folderRepeater
                model: folderRows
                RowLayout {
                  id: folderRow
                  required property int index
                  required property string path
                  required property int depth
                  width: parent.width
                  spacing: Style.space(8)
                  enabled: !folderPicker.running
                  function focusPath() { folderPath.forceActiveFocus() }
                  GithubAction {
                    iconName: "folder"
                    tooltipText: root.tr("Browse for folder")
                    foreground: root.dim
                    onClicked: root.browseFolder(folderRow.index)
                    Keys.onEscapePressed: root.closeSettings()
                  }
                  Controls.TextField {
                    id: folderPath
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    implicitHeight: Style.spacing.controlHeight
                    text: folderRow.path
                    color: root.foreground
                    font.family: "sans-serif"
                    font.pixelSize: Style.space(12)
                    padding: Style.space(8)
                    selectByMouse: true
                    Accessible.name: root.tr("Repository folder %1", [folderRow.index + 1])
                    placeholderText: "~/Projects"
                    placeholderTextColor: root.dim
                    background: Rectangle {
                      radius: root.controlRadius
                      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.055)
                      border.width: 1
                      border.color: folderPath.activeFocus ? root.accent : root.outlineColor
                    }
                    onTextEdited: {
                      folderRows.setProperty(folderRow.index, "path", text)
                      root.foldersDirty = true
                      root.settingsError = ""
                    }
                    onEditingFinished: if (root.foldersDirty) root.saveFolders()
                    onActiveFocusChanged: if (activeFocus) root.showFolderRow(folderRow)
                    Keys.onEscapePressed: root.closeSettings()
                  }
                  GithubDropdown {
                    cornerRadius: root.controlRadius
                    Layout.preferredWidth: Style.space(96)
                    showLabel: false
                    label: root.tr("Scan levels for %1", [folderRow.path])
                    fontFamily: "sans-serif"
                    value: String(folderRow.depth)
                    options: [0,1,2,3,4,5].map(function(depth) {
                      return {value: String(depth), label: root.tr(depth === 1 ? "%1 level" : "%1 levels", [depth])}
                    })
                    onChanged: function(value) {
                      folderRows.setProperty(folderRow.index, "depth", Number(value))
                      root.foldersDirty = true
                      root.saveFolders()
                    }
                    Keys.onEscapePressed: root.closeSettings()
                  }
                  GithubAction {
                    iconName: "trash"
                    tooltipText: root.tr("Remove folder")
                    foreground: root.dim
                    onClicked: {
                      folderRows.remove(folderRow.index)
                      root.ensureFolderRow()
                      root.foldersDirty = true
                      root.saveFolders()
                      addFolderButton.forceActiveFocus()
                    }
                    Keys.onEscapePressed: root.closeSettings()
                  }
                }
              }
            }
            GithubLabel {
              visible: root.settingsError !== ""
              width: parent.width
              text: root.tr(root.settingsError)
              color: root.dim
              wrapMode: Text.WordWrap
              Accessible.role: Accessible.AlertMessage
            }
            Toggle {
              width: parent.width
              implicitHeight: Style.space(36)
              color: "transparent"
              borderSpec: activeFocus ? Border.flat(root.accent, 1) : Border.none()
              radius: root.controlRadius
              fontFamily: "sans-serif"
              titleSize: Style.space(12)
              label: root.tr("Detect changes automatically (Recommended)")
              checked: github.inotifyEnabled
              onClicked: root.savePreference("inotifyEnabled", !checked)
              Keys.onEscapePressed: root.closeSettings()
            }
            GithubDropdown {
              cornerRadius: root.controlRadius
              id: localInterval
              width: parent.width
              label: root.tr(github.inotifyEnabled ? "Fallback interval" : "Interval")
              fontFamily: "sans-serif"
              value: String(github.refreshIntervalSec)
              options: [{value:"0",label:root.tr("Disabled")},{value:"10",label:root.tr("Every 10 seconds")},{value:"30",label:root.tr("Every 30 seconds")},{value:"60",label:root.tr("Every minute")},{value:"300",label:root.tr("Every 5 minutes")}]
              onChanged: function(value) { root.savePreference("refreshIntervalSec", Number(value)) }
              Keys.onEscapePressed: root.closeSettings()
            }
            GithubDropdown {
              cornerRadius: root.controlRadius
              id: remoteInterval
              width: parent.width
              label: root.tr("Remote fetch")
              fontFamily: "sans-serif"
              value: String(github.fetchIntervalSec)
              options: [{value:"300",label:root.tr("Every 5 minutes")},{value:"900",label:root.tr("Every 15 minutes")},{value:"1800",label:root.tr("Every 30 minutes")},{value:"3600",label:root.tr("Every hour")}]
              onChanged: function(value) { root.savePreference("fetchIntervalSec", Number(value)) }
              Keys.onEscapePressed: root.closeSettings()
            }

          }

          Column {
            visible: !root.editingSettings
            width: parent.width - Style.space(40)
            anchors.horizontalCenter: parent.horizontalCenter
            topPadding: Style.space(8)
            bottomPadding: Style.space(16)
            spacing: Style.space(12)

            GithubLabel {
              visible: github.lastError !== "" || github.sync.stale
              width: parent.width
              text: github.lastError !== "" ? root.tr(github.lastError)
                : root.lastSyncText + " · " + root.tr("remote state may be stale")
              color: github.lastError !== "" ? root.urgent : github.sync.stale ? root.warning : root.dim
              font.pixelSize: Style.space(12)
              wrapMode: Text.WordWrap
            }

            Column {
              id: repoColumn
              visible: root.displayedRepos.length > 0
              width: parent.width
              Repeater {
                model: root.displayedRepos
                RepositoryRow {
                  required property var modelData
                  required property int index
                  width: repoColumn.width
                  repo: modelData
                  rowIndex: index
                }
              }
            }

            Column {
              visible: root.displayedRepos.length === 0
              width: parent.width
              topPadding: Style.space(18)
              bottomPadding: Style.space(18)
              spacing: Style.space(8)
              Text {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: github.lastError === "" && github.lastChecked > 0 && github.status.repoCount > 0
                text: "\uf00c"
                color: root.success
                font.family: root.fontFamily
                font.pixelSize: Style.space(24)
              }
              GithubLabel {
                width: parent.width
                text: github.lastError !== "" ? root.tr("Repository status is unavailable. Try Refresh again.")
                  : github.lastChecked === 0 ? root.tr("Checking local repositories…")
                  : github.status.repoCount === 0 ? root.tr("No local repositories are being tracked.")
                  : root.tr("All local repositories are clean and aligned with their cached upstream state.")
                color: root.dim
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
              }
            }

            Column {
              visible: root.hasFailures
              width: parent.width
              spacing: Style.space(8)
              GithubLabel { text: root.tr("Last sync issues"); color: root.urgent }
              Repeater {
                model: github.sync.failures.slice(0, 4)
                GithubLabel {
                  required property var modelData
                  width: parent.width
                  text: String(modelData.label || modelData.name || root.tr("Repository")) + "\n"
                    + root.tr(String(modelData.message || "Synchronization failed"))
                  color: root.urgent
                  font.pixelSize: Style.space(12)
                  wrapMode: Text.WordWrap
                }
              }
            }

            Rectangle { width: parent.width; height: 1; color: root.outlineColor }
            RowLayout {
              width: parent.width
              spacing: Style.space(8)
              GithubLabel {
                text: root.tr(github.status.repoCount === 1 ? "%1 repository" : "%1 repositories", [github.status.repoCount])
                color: root.dim
                font.pixelSize: Style.space(12)
                Layout.fillWidth: true
                elide: Text.ElideRight
              }
              GithubLabel {
                text: root.updatedText
                color: root.dim
                font.pixelSize: Style.space(12)
              }
              PanelActionButton {
                id: refreshButton
                tooltipText: github.syncing ? root.tr("Fetching repositories…")
                  : github.refreshing ? root.tr("Refreshing local status")
                  : root.tr("Fetch remotes and refresh status (R)") + "\n" + root.lastSyncText
                enabled: !github.busy
                foreground: root.dim
                fontFamily: root.fontFamily
                fontSize: Style.space(14)
                size: Style.space(26)
                onClicked: github.fetch()
                // Keep the hover surface and hit area fixed while only the glyph spins.
                Text {
                  id: refreshGlyph
                  anchors.centerIn: parent
                  text: "󰑐"
                  color: refreshButton.enabled ? root.dim : Qt.darker(root.dim, 2.0)
                  font.family: root.fontFamily
                  font.pixelSize: Style.space(14)
                  RotationAnimation on rotation {
                    running: root.opened && (github.syncing || github.refreshing)
                    from: 0; to: 360; duration: 800; loops: Animation.Infinite
                    onStopped: refreshGlyph.rotation = 0
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  component SummaryCount: Row {
    property int value: 0
    property string label: ""
    property string symbol: ""
    property color tint: root.foreground
    spacing: Style.space(5)
    GithubLabel { id: countValue; text: parent.symbol + " " + parent.value; color: parent.tint; font.pixelSize: Style.space(16) }
    GithubLabel { text: parent.label; color: root.dim; font.pixelSize: Style.space(12); anchors.baseline: countValue.baseline }
  }

  component RepositoryRow: Item {
    id: repoRow
    property var repo: null
    property int rowIndex: 0
    readonly property color stateColor: repo && repo.dirtyCount > 0 ? root.warning
      : (repo && repo.behind > 0 ? root.accent : root.success)

    implicitHeight: repoContent.implicitHeight + Style.space(24)
    HoverHandler { onHoveredChanged: if (hovered) root.setRepoCursor(repoRow.rowIndex) }

    Rectangle {
      visible: repoRow.rowIndex < root.displayedRepos.length - 1
      anchors.bottom: parent.bottom
      width: parent.width
      height: 1
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
    }
    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(2)
      anchors.rightMargin: Style.space(2)
      spacing: Style.space(12)
      Rectangle { width: Style.space(2); height: Style.space(32); radius: width / 2; color: repoRow.stateColor }
      ColumnLayout {
        id: repoContent
        Layout.fillWidth: true
        spacing: Style.space(3)
        GithubLabel {
          Layout.fillWidth: true
          text: String(repoRow.repo ? repoRow.repo.label : root.tr("Repository"))
          font.pixelSize: Style.space(15)
          elide: Text.ElideRight
        }
        GithubLabel {
          Layout.fillWidth: true
          text: Model.repositoryMeta(repoRow.repo, root.tr)
          color: root.dim
          font.pixelSize: Style.space(12)
          elide: Text.ElideRight
        }
      }
      RowLayout {
        spacing: Style.space(2)
        PanelActionButton {
          iconText: "\uf062"
          tooltipText: root.tr("Push %1", [String(repoRow.repo ? repoRow.repo.label : root.tr("repository"))])
          enabled: !github.busy && repoRow.repo && repoRow.repo.ahead > 0
          foreground: root.dim
          hoverColor: root.success
          fontFamily: root.fontFamily
          fontSize: Style.space(12)
          size: Style.space(26)
          onClicked: github.pushRepository(repoRow.repo)
        }
        PanelActionButton {
          iconText: "\uf063"
          tooltipText: root.tr("Pull %1", [String(repoRow.repo ? repoRow.repo.label : root.tr("repository"))])
          enabled: !github.busy && repoRow.repo && repoRow.repo.behind > 0
          foreground: root.dim
          hoverColor: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.space(12)
          size: Style.space(26)
          onClicked: github.pullRepository(repoRow.repo)
        }
        PanelActionButton {
          iconText: "\uf06e"
          tooltipText: root.tr("View in lazygit")
          enabled: repoRow.repo !== null
          foreground: root.dim
          fontFamily: root.fontFamily
          fontSize: Style.space(12)
          size: Style.space(26)
          onClicked: root.viewRepository(repoRow.repo)
        }
      }
    }
  }
}
