pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "kit"
import "Model.js" as Model
import "Preferences.js" as Schema

// Foamy GitHub bar widget: one per bar (4.5 draws a bar per monitor). State
// and Git helpers live in the Service singleton, once per shell; the
// FoamyMember leader feeds it this widget's settings and owns the IPC target.
Panel {
  id: root

  moduleName: "foamy.github"
  ipcTarget: "foamy.github"
  manageIpc: false

  readonly property bool editingSettings: settingsState.editing
  // Local errors (folder validation, paste, folder browser); save errors come from prefs.
  property string settingsError: ""
  property int browsingRow: -1
  property bool returningFromBrowser: false
  readonly property alias popup: popup
  readonly property alias prefs: prefs
  readonly property var view: popup.bodyItem

  readonly property var totals: Service.totals
  readonly property string tooltipText: (view ? view.healthText : FoamyI18n.tn("%1 repositories", Service.status.repoCount))
    + "\n" + FoamyI18n.t("Left: details · Middle: fetch")

  FoamyMember { id: member; scope: "github"; demanding: root.opened }
  Binding {
    target: Service
    property: "settings"
    value: root.settings
    when: member.isLeader
    restoreMode: Binding.RestoreNone
  }

  FoamyIpc {
    target: "foamy.github"
    enabled: member.isLeader
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function settings(): void { root.openSettings() }
    function refresh(): string { Service.fetch(); return "ok" }
    function status(): string { return JSON.stringify({ repositories: Service.status.repoCount, affected: Service.status.affectedCount, totals: Service.totals, syncing: Service.syncing, monitoring: Service.monitoring }) }
  }

  onOpenedChanged: if (opened && !returningFromBrowser) Service.popupOpened()

  // Settings open from the cog, `s` or IPC (also on a closed popup) and reset
  // only when the popup closes; the folder browser round trip keeps them.
  FoamySettingsState {
    id: settingsState
    panel: root
    holdOnClose: root.returningFromBrowser
    onShown: root.settingsError = ""
  }
  function openSettings() { settingsState.show() }
  function closeSettings() { settingsState.hide() }

  // repositoryFolders: a list of {path, depth}, validated by Model.parseFolderRows.
  readonly property var fields: Schema.fields.concat([{
    key: "repositoryFolders", type: "array", maxItems: 32, defaultValue: [],
    validate: function(value) { var parsed = Model.parseFolderRows(value); return parsed.error ? parsed.error : true }
  }])
  FoamyPrefsQueue {
    id: prefs
    bar: root.bar
    moduleName: root.moduleName
    fields: root.fields
    settings: root.settings
  }
  readonly property string pageError: settingsError !== "" ? settingsError : prefs.error !== "" ? FoamyI18n.t(prefs.error) : ""

  function save(key, value) {
    settingsError = ""
    prefs.save(key, value)
  }

  function saveFolders(rows) {
    var parsed = Model.parseFolderRows(rows)
    if (parsed.error) { settingsError = FoamyI18n.t(parsed.error); return }
    settingsError = ""
    prefs.save("repositoryFolders", parsed.folders)
  }

  // The folder chooser is a separate window: release the popup while it is open.
  function browseFolder(index) {
    if (folderPicker.running) return
    browsingRow = index
    returningFromBrowser = true
    root.close()
    folderPicker.running = true
  }

  Process {
    id: folderPicker
    command: ["bash", decodeURIComponent(Qt.resolvedUrl("folder-picker.sh").toString().replace(/^file:\/\//, "")),
      FoamyI18n.t("Choose a repository folder"), FoamyI18n.t("Choose")]
    stdout: StdioCollector { id: folderPickerOutput; waitForEnd: true }
    onExited: function(exitCode) {
      var match = String(folderPickerOutput.text).match(/(?:GITHUB_FOLDER=)?(file:\/\/[^\r\n]*)/)
      root.open()
      if (exitCode !== 0) root.settingsError = FoamyI18n.t("Couldn't open the folder browser. Enter a path instead.")
      else if (match && root.view) root.view.setFolderPath(root.browsingRow, decodeURIComponent(match[1].replace(/^file:\/\//, "")))
      Qt.callLater(function() { root.returningFromBrowser = false; root.browsingRow = -1 })
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

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
      if (buttonCode === Qt.MiddleButton) { if (!Service.busy) Service.fetch() }
      else root.toggle()
    }

    Row {
      id: statusRow
      anchors.centerIn: parent
      spacing: Style.space(1)

      OpticalGlyph {
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        text: Service.syncing ? "\u{f021}" : "\u{f09b}"
        color: Service.syncing ? Commons.Color.accent : button.foreground
        fontFamily: button.fontFamily
        fontSize: button.fontSize
      }
      BarCount { shown: root.totals.aheadRepos > 0; text: root.totals.aheadRepos + "↑"; color: FoamyPalette.success }
      BarCount { shown: root.totals.behindRepos > 0; text: root.totals.behindRepos + "↓"; color: FoamyPalette.info }
      BarCount { shown: root.totals.dirtyRepos > 0; text: root.totals.dirtyRepos + "*"; color: FoamyPalette.warning }
      BarCount { shown: root.totals.failedRepos > 0; text: root.totals.failedRepos + "!"; color: FoamyPalette.error }
      BarCount {
        shown: root.totals.unavailableRepos > 0 || (Service.sync.stale && root.totals.failedRepos === 0 && Service.status.repoCount > 0)
        text: root.totals.unavailableRepos > 0 ? root.totals.unavailableRepos + "?" : "?"
        color: FoamyPalette.error
      }
    }
  }

  component BarCount: Text {
    property bool shown: false
    visible: shown && !button.vertical && !Service.syncing
    textFormat: Text.PlainText
    font.family: button.fontFamily
    font.pixelSize: button.fontSize
    renderType: Text.NativeRendering
  }

  FoamyPopup {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.opened
    body: Component {
      GithubView {
        service: Service
        settings: prefs.effectiveSettings
        opened: root.opened
        editingSettings: root.editingSettings
        saving: prefs.busy
        settingsError: root.pageError
        retryable: root.settingsError === "" && prefs.canRetry
        browsing: folderPicker.running
        onCloseRequestedByView: root.close()
        onSettingsErrorReported: function(message) { root.settingsError = message }
        onSettingsRequested: root.openSettings()
        onSettingsClosed: root.closeSettings()
        onSaveRequested: function(key, value) { root.save(key, value) }
        onFoldersEdited: function(rows) { root.saveFolders(rows) }
        onBrowseRequested: function(index) { root.browseFolder(index) }
        onRetryRequested: prefs.retry()
        onSwitchPanelRequested: function(direction) { root.switchPanel(direction) }
        onRepositoryRequested: function(repo) { root.close(); Service.openRepository(repo) }
      }
    }
  }
}
