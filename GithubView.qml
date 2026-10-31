pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "kit"
import "Model.js" as Model
import "Preferences.js" as Schema
import "kit/FoamyPrefs.js" as Prefs

// The popup body: header, repository list (one keyboard/pointer cursor) and
// the settings page. Data comes from `service` (the Service singleton, or a
// mock in preview/Preview.qml); the Panel owns saving and the folder browser.
PanelKeyCatcher {
  id: root

  property QtObject service: null
  property var settings: ({})
  property bool opened: true
  property bool editingSettings: false
  property bool saving: false
  property string settingsError: ""
  property bool retryable: false
  property bool browsing: false
  property double now: Date.now()

  signal closeRequestedByView()
  signal settingsRequested()
  signal settingsClosed()
  signal saveRequested(string key, var value)
  signal foldersEdited(var rows)
  signal browseRequested(int index)
  signal retryRequested()
  // Errors raised inside the view (clipboard paste) go to the Panel, which owns settingsError.
  signal settingsErrorReported(string message)
  signal switchPanelRequested(int direction)
  signal repositoryRequested(var repo)
  signal trustRequested(var repo, bool trusted)

  readonly property var repos: service ? service.affectedRepos : []
  readonly property var totals: service ? service.totals : Model.defaultStatus().totals
  readonly property var sync: service ? service.sync : Model.defaultStatus().sync
  readonly property bool busy: !!service && service.busy
  readonly property bool hasFailures: sync.failures.length > 0
  // Cursor stops: each row, then its "LFS" button when the repository uses
  // Git LFS and is untrusted, then (note open, trust available) its "Trust
  // this repository" button; then Refresh and Settings.
  readonly property var stops: {
    var rows = [], infos = [], trusts = [], count = 0
    for (var i = 0; i < repos.length; i++) {
      var lfs = !!repos[i] && repos[i].usesLfs === true && repos[i].trusted !== true && repos[i].complete !== false
      rows.push(count++)
      infos.push(lfs ? count++ : -1)
      trusts.push(lfs && repos[i].path === lfsNotePath && !!repos[i].identity ? count++ : -1)
    }
    return { rows: rows, infos: infos, trusts: trusts, count: count }
  }
  // The repository (path) whose Git LFS note is open under its row: one at a time.
  property string lfsNotePath: ""
  readonly property int refreshStop: stops.count
  readonly property int settingsStop: stops.count + 1
  readonly property var trustedList: Model.trustedRepositories(settings)
  readonly property var lfsRepositories: service && service.lfsRepos ? service.lfsRepos : []
  readonly property string healthText: {
    if (!service) return ""
    if (service.syncing) return FoamyI18n.t("Fetching repositories…")
    if (service.lastError !== "") return FoamyI18n.t("Repository check needs attention")
    if (service.lastChecked === 0) return FoamyI18n.t("Checking repositories…")
    if (service.status.repoCount === 0) return FoamyI18n.t("No repositories found")
    if (service.status.affectedCount === 0 && !sync.stale && !hasFailures) return FoamyI18n.t("Everything is in sync")
    if (service.status.affectedCount === 0 && !hasFailures) return FoamyI18n.t("Working trees are clean")
    return FoamyI18n.tn("%1 repositories need attention", Math.max(service.status.affectedCount, repos.length))
  }
  readonly property string statusTitle: service && service.actionStatus !== "" ? service.actionStatus : healthText
  readonly property bool animateTitle: !!service && (service.syncing || service.repositoryActionRunning
    || (service.refreshing && service.lastChecked === 0))
  property int progressStep: 0
  // Animated dots only while shown and motion is allowed; otherwise a static ellipsis.
  readonly property string displayTitle: animateTitle && !FoamyMotion.reduceMotion
    ? statusTitle.replace(/(?:…|\.{1,3})$/, "") + [".", "..", "..."][progressStep]
    : statusTitle
  readonly property string lastSyncText: FoamyI18n.t("Last remote sync %1", [Model.relativeTime(sync.lastSuccess, now, FoamyI18n.t)])

  implicitWidth: FoamyStyle.popupWidth
  implicitHeight: editingSettings ? settingsPage.implicitHeight : list.contentHeight
  blocked: editingSettings

  FoamyCursor { id: cursor; count: root.stops.count + 2 }   // rows (+ LFS, trust), refresh, settings

  onMoveRequested: function(dx, dy) { cursor.move(dy !== 0 ? dy : dx) }
  onActivateRequested: root.activate(cursor.index)
  onCloseRequested: root.closeRequestedByView()
  onTabRequested: function(direction) { root.switchPanelRequested(direction) }
  onTextKey: function(text) {
    if (text === "s" || text === "S") root.settingsRequested()
    else if ((text === "r" || text === "R") && !root.busy) root.service.fetch()
  }
  onOpenedChanged: if (!opened) { cursor.reset(); lfsNotePath = "" }
  onEditingSettingsChanged: {
    if (editingSettings) {
      loadFolders()
      settingsPage.resetScroll()
      Qt.callLater(settingsPage.focusBack)
    } else {
      Qt.callLater(function() { root.forceActiveFocus() })
    }
  }

  Connections {
    target: cursor
    function onIndexChanged() {
      var row = root.rowForStop(cursor.index)
      if (row >= 0) list.positionViewAtIndex(row, ListView.Contain)
      else if (cursor.index >= root.refreshStop) list.positionViewAtBeginning()
    }
  }

  function rowForStop(index) {
    var row = stops.rows.indexOf(index)
    if (row < 0) row = stops.infos.indexOf(index)
    return row >= 0 ? row : stops.trusts.indexOf(index)
  }

  // Open or fold a repository's Git LFS note (the cursor stays where it is).
  function toggleLfsNote(repo) {
    if (!repo) return
    lfsNotePath = lfsNotePath === repo.path ? "" : String(repo.path)
  }

  // Preview/tests: open a row's Git LFS note with the cursor on its LFS button.
  function clearCursor() { cursor.reset(); lfsNotePath = "" }
  function showLfsInfo(row) {
    if (row < 0 || row >= stops.infos.length || stops.infos[row] < 0) return
    lfsNotePath = String(repos[row].path)
    cursor.hover(stops.infos[row])
  }
  // Preview: scroll Settings to its end (the Git LFS section, when shown).
  function showSettingsEnd() { settingsPage.keepVisible(lfsSection.visible ? lfsSection : refreshSection) }

  function activate(index) {
    if (index < 0) return
    var info = stops.infos.indexOf(index)
    var trust = stops.trusts.indexOf(index)
    if (info >= 0) toggleLfsNote(repos[info])
    else if (trust >= 0) {
      root.trustRequested(repos[trust], true)
      lfsNotePath = ""
      cursor.hover(stops.rows[trust])         // stay on that repository once its LFS button goes
    }
    else if (stops.rows.indexOf(index) >= 0) root.repositoryRequested(repos[stops.rows.indexOf(index)])
    else if (index === refreshStop) { if (!busy) service.fetch() }
    else if (index === settingsStop) root.settingsRequested()
  }

  Timer {
    interval: 400
    repeat: true
    running: root.opened && root.animateTitle && !FoamyMotion.reduceMotion
    onRunningChanged: if (!running) root.progressStep = 0
    onTriggered: root.progressStep = (root.progressStep + 1) % 3
  }

  // "Updated N minutes ago" ages only while the popup is shown.
  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    triggeredOnStart: true
    onTriggered: root.now = Date.now()
  }

  // ---- folder rows (settings) ---------------------------------------------------
  ListModel { id: folderRows }

  function loadFolders() {
    var result = Model.parseFolderRows(Model.repositoryFolders(root.settings))
    folderRows.clear()
    if (!result.error) result.folders.forEach(function(folder) { folderRows.append(folder) })
    if (folderRows.count === 0) folderRows.append({ path: "", depth: 2 })
  }

  function folderList() {
    var rows = []
    for (var i = 0; i < folderRows.count; i++) {
      var row = folderRows.get(i)
      // Blank editor rows are placeholders, not configured directories.
      if (row.path.trim() !== "") rows.push({ path: row.path, depth: row.depth })
    }
    return rows
  }

  function setFolderPath(index, path) {
    if (index < 0 || index >= folderRows.count) return
    folderRows.setProperty(index, "path", path)
    root.foldersEdited(folderList())
    Qt.callLater(function() {
      var row = folderRepeater.itemAt(index)
      if (row) row.focusPath()
    })
  }

  function addFolder() {
    if (folderRows.count >= 32) return
    folderRows.append({ path: "", depth: 2 })
    Qt.callLater(function() {
      var row = folderRepeater.itemAt(folderRows.count - 1)
      if (row) row.focusPath()
    })
  }

  // ---- repository list ------------------------------------------------------------
  ListView {
    id: list
    visible: !root.editingSettings
    anchors.fill: parent
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    interactive: contentHeight > height
    // Rows are virtualised: only visible repositories are instantiated.
    model: root.editingSettings ? [] : root.repos
    cacheBuffer: 0
    ScrollBar.vertical: ScrollBar {
      visible: list.interactive
      width: FoamyStyle.gapSmall
      padding: 0
      contentItem: Rectangle { implicitWidth: FoamyStyle.gapSmall; radius: FoamyStyle.pillRadius(width); color: FoamyPalette.mutedText }
      background: Item {}
    }

    header: Column {
      width: list.width
      // The foamy hero band: title row on the tinted wash, the repository
      // summary (ahead / behind / changed / failed) in its hero slot.
      FoamyHeroBand {
        id: band
        width: parent.width
        iconName: "github"
        title: FoamyI18n.t("GitHub")
        meta: root.displayTitle
        settingsHasCursor: cursor.at(root.settingsStop)
        onSettingsClicked: root.settingsRequested()
        onSettingsHovered: function(hovered) { if (hovered) cursor.hover(root.settingsStop) }
        trailing: [
          FoamyAction {
            iconName: "refresh-cw"
            tooltipText: root.service && root.service.syncing ? FoamyI18n.t("Fetching repositories…")
              : FoamyI18n.t("Fetch remotes and refresh") + "\n" + root.lastSyncText
            spinning: root.opened && !!root.service && (root.service.syncing || root.service.refreshing)
            actionEnabled: !root.busy
            hasCursor: cursor.at(root.refreshStop)
            foreground: FoamyPalette.secondaryText
            onHoveredChanged: if (hovered) cursor.hover(root.refreshStop)
            onClicked: root.service.fetch()
          }
        ]
        Flow {
          Layout.fillWidth: true
          spacing: FoamyStyle.padding
          SummaryCount { value: root.totals.aheadRepos; label: FoamyI18n.t("ahead"); symbol: "↑"; tint: FoamyPalette.onSurface(FoamyPalette.success) }
          SummaryCount { value: root.totals.behindRepos; label: FoamyI18n.t("behind"); symbol: "↓"; tint: FoamyPalette.onSurface(FoamyPalette.info) }
          SummaryCount { value: root.totals.dirtyRepos; label: FoamyI18n.t("changed"); symbol: "•"; tint: FoamyPalette.onSurface(FoamyPalette.warning) }
          SummaryCount { visible: value > 0; value: root.totals.failedRepos; label: FoamyI18n.t("failed"); symbol: "!"; tint: FoamyPalette.onSurface(FoamyPalette.error) }
        }
      }
      Item { width: parent.width; height: FoamyStyle.gapLarge }
      Text {
        id: statusWarning
        visible: !!root.service && (root.service.lastError !== "" || root.sync.stale)
        x: FoamyStyle.padding
        width: parent.width - FoamyStyle.padding * 2
        bottomPadding: FoamyStyle.gapLarge
        text: !root.service ? "" : root.service.lastError !== "" ? FoamyI18n.t(root.service.lastError)
          : root.lastSyncText + " · " + FoamyI18n.t("remote state may be stale")
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignLeft
        color: root.service && root.service.lastError !== "" ? Commons.Color.urgent : FoamyPalette.onSurface(FoamyPalette.warning)
        font.family: FoamyStyle.fontFamily
        font.pixelSize: FoamyStyle.font.bodySmall
        Accessible.role: root.service && root.service.lastError !== "" ? Accessible.AlertMessage : Accessible.StaticText
      }
    }

    delegate: Item {
      id: rowItem
      required property var modelData
      required property int index
      width: list.width
      height: surface.height + FoamyStyle.gapTiny
      RepositoryRow {
        id: surface
        x: FoamyStyle.gapLarge
        width: parent.width - FoamyStyle.gapLarge * 2
        repo: rowItem.modelData
        rowIndex: rowItem.index
      }
    }

    footer: Column {
      width: list.width
      topPadding: FoamyStyle.gap
      bottomPadding: FoamyStyle.padding
      spacing: FoamyStyle.gapLarge
      Text {
        visible: root.repos.length === 0
        x: FoamyStyle.padding
        width: parent.width - FoamyStyle.padding * 2
        topPadding: FoamyStyle.gapLarge
        bottomPadding: FoamyStyle.gapLarge
        text: !root.service ? "" : root.service.lastError !== "" ? FoamyI18n.t("Repository status is unavailable. Try Refresh again.")
          : root.service.lastChecked === 0 ? FoamyI18n.t("Checking local repositories…")
          : root.service.status.repoCount === 0 ? FoamyI18n.t("No repositories yet. Add a folder in Settings.")
          : FoamyI18n.t("All repositories are clean and match their upstream")
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignLeft
        color: FoamyPalette.mutedText
        font.family: FoamyStyle.fontFamily
        font.pixelSize: FoamyStyle.font.bodySmall
      }
      Rectangle { x: FoamyStyle.padding; width: parent.width - FoamyStyle.padding * 2; height: 1; color: FoamyPalette.divider }
      RowLayout {
        x: FoamyStyle.padding
        width: parent.width - FoamyStyle.padding * 2
        spacing: FoamyStyle.gap
        Text {
          Layout.fillWidth: true
          text: FoamyI18n.tn("%1 repositories", root.service ? root.service.status.repoCount : 0)
          textFormat: Text.PlainText
          elide: Text.ElideRight
          horizontalAlignment: Text.AlignLeft
          color: FoamyPalette.secondaryText
          font.family: FoamyStyle.fontFamily
          font.pixelSize: FoamyStyle.font.bodySmall
        }
        Text {
          text: root.service ? Model.updatedText(root.service.lastChecked, root.now, FoamyI18n.t, FoamyI18n.tn) : ""
          textFormat: Text.PlainText
          color: FoamyPalette.secondaryText
          font.family: FoamyStyle.fontFamily
          font.pixelSize: FoamyStyle.font.bodySmall
        }
      }
    }
  }

  // ---- settings -------------------------------------------------------------------
  FoamySettingsPage {
    id: settingsPage
    visible: root.editingSettings
    width: parent.width
    height: parent.height
    saving: root.saving
    error: root.settingsError
    retryable: root.retryable
    onBack: root.settingsClosed()
    onRetry: root.retryRequested()

    FoamySettingsSection {
      width: parent.width
      title: FoamyI18n.t("Repository folders")
      description: FoamyI18n.t("Folders to search for Git repositories, and how many levels deep.")

      Repeater {
        id: folderRepeater
        model: folderRows
        RowLayout {
          id: folderRow
          required property int index
          required property string path
          required property int depth
          function focusPath() { folderPath.forceActiveFocus() }
          width: parent.width
          spacing: FoamyStyle.gap
          enabled: !root.browsing

          FoamyAction {
            iconName: "folder"
            focusable: true
            tooltipText: FoamyI18n.t("Browse for folder")
            foreground: FoamyPalette.secondaryText
            onClicked: root.browseRequested(folderRow.index)
          }
          SafeTextField {
            id: folderPath
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            implicitHeight: FoamyStyle.controlHeight
            text: folderRow.path
            placeholderText: "~/Projects"
            translate: FoamyI18n.t
            Accessible.name: FoamyI18n.t("Repository folder %1", [folderRow.index + 1])
            onPasteErrorChanged: if (pasteError !== "") root.settingsErrorReported(pasteError)
            onTextEdited: folderRows.setProperty(folderRow.index, "path", text)
            onEditingFinished: root.foldersEdited(root.folderList())
            onInputActiveFocusChanged: if (inputActiveFocus) settingsPage.keepVisible(folderRow)
          }
          FoamyDropdown {
            Layout.preferredWidth: Style.space(96)
            showLabel: false
            label: FoamyI18n.t("Scan levels for %1", [folderRow.path])
            value: String(folderRow.depth)
            options: [0, 1, 2, 3, 4, 5].map(function(depth) {
              return { value: String(depth), label: FoamyI18n.tn("%1 levels", depth) }
            })
            onChanged: function(value) {
              folderRows.setProperty(folderRow.index, "depth", Number(value))
              root.foldersEdited(root.folderList())
            }
          }
          FoamyAction {
            iconName: "trash"
            focusable: true
            destructive: true
            tooltipText: FoamyI18n.t("Remove folder")
            foreground: FoamyPalette.secondaryText
            onClicked: {
              folderRows.remove(folderRow.index)
              if (folderRows.count === 0) folderRows.append({ path: "", depth: 2 })
              root.foldersEdited(root.folderList())
            }
          }
        }
      }
      FoamyAction {
        iconName: "plus"
        label: FoamyI18n.t("Add folder")
        focusable: true
        foreground: FoamyPalette.secondaryText
        tooltipText: folderRows.count >= 32 ? FoamyI18n.t("You can add up to 32 folders.") : ""
        actionEnabled: folderRows.count < 32 && !root.browsing
        onClicked: root.addFolder()
      }
    }

    FoamySettingsSection {
      id: refreshSection
      width: parent.width
      title: FoamyI18n.t("Refresh")
      FoamyToggle {
        width: parent.width
        label: FoamyI18n.t("Detect changes automatically")
        description: FoamyI18n.t("Recommended. Watches the repositories for changes.")
        checked: Prefs.value(Schema.fields, root.settings, "inotifyEnabled")
        onClicked: root.saveRequested("inotifyEnabled", !checked)
      }
      FoamyDropdown {
        width: parent.width
        label: Prefs.value(Schema.fields, root.settings, "inotifyEnabled") ? FoamyI18n.t("Fallback refresh every") : FoamyI18n.t("Refresh every")
        value: String(Prefs.value(Schema.fields, root.settings, "refreshIntervalSec"))
        options: [
          { value: "0", label: FoamyI18n.t("Off") },
          { value: "10", label: FoamyI18n.tn("%1 seconds", 10) },
          { value: "30", label: FoamyI18n.tn("%1 seconds", 30) },
          { value: "60", label: FoamyI18n.tn("%1 minutes", 1) },
          { value: "300", label: FoamyI18n.tn("%1 minutes", 5) }
        ]
        onChanged: function(value) { root.saveRequested("refreshIntervalSec", Number(value)) }
      }
      FoamyDropdown {
        width: parent.width
        label: FoamyI18n.t("Fetch remotes every")
        value: String(Prefs.value(Schema.fields, root.settings, "fetchIntervalSec"))
        options: [
          { value: "300", label: FoamyI18n.tn("%1 minutes", 5) },
          { value: "900", label: FoamyI18n.tn("%1 minutes", 15) },
          { value: "1800", label: FoamyI18n.tn("%1 minutes", 30) },
          { value: "3600", label: FoamyI18n.tn("%1 hours", 1) }
        ]
        onChanged: function(value) { root.saveRequested("fetchIntervalSec", Number(value)) }
      }
    }

    // Git LFS: per-repository trust, only when a repository using LFS was found.
    // The long explanation is the section's (i) note, not a tooltip.
    FoamySettingsSection {
      id: lfsSection
      width: parent.width
      visible: root.lfsRepositories.length > 0
      title: FoamyI18n.t("Git LFS")
      description: FoamyI18n.t("Turn on for repositories you trust. They then run their own Git LFS settings during automatic checks.")
      info: FoamyI18n.t("Automatic checks run Git without a repository's own settings, filters and hooks, so nothing in a repository can run code just by being in these folders. For Git LFS that means files you edited may show as changed until Git refreshes. A trusted repository runs its own Git settings, including LFS, during automatic checks and fetches, so its counts are exact. Trust applies to the repository at that exact path; moving or renaming it removes the trust.")
      Repeater {
        model: root.lfsRepositories
        FoamyToggle {
          required property var modelData
          width: lfsSection.width
          label: String(modelData.label)
          description: modelData.identity ? String(modelData.path)
            : FoamyI18n.t("Trust isn't available for this repository's filesystem.")
          enabled: !!modelData.identity
          checked: Model.isTrusted(root.trustedList, modelData)
          onClicked: root.trustRequested(modelData, !checked)
        }
      }
    }
  }

  // ---- components ---------------------------------------------------------------------
  component SummaryCount: Row {
    property int value: 0
    property string label: ""
    property string symbol: ""
    property color tint: FoamyPalette.text
    spacing: FoamyStyle.gapSmall
    Text {
      id: countValue
      text: parent.symbol + " " + FoamyI18n.number(parent.value)
      textFormat: Text.PlainText
      color: parent.tint
      font.family: FoamyStyle.fontFamily
      font.pixelSize: FoamyStyle.font.heading
    }
    Text {
      text: parent.label
      textFormat: Text.PlainText
      color: FoamyPalette.secondaryText
      font.family: FoamyStyle.fontFamily
      font.pixelSize: FoamyStyle.font.bodySmall
      anchors.baseline: countValue.baseline
    }
  }

  component RepositoryRow: FoamyCursorSurface {
    id: repoRow
    property var repo: null
    property int rowIndex: 0
    readonly property bool unavailable: !!repo && repo.complete === false
    readonly property string meta: Model.repositoryMeta(repo, FoamyI18n.t, FoamyI18n.tn)
    readonly property string syncError: Model.syncErrorText(repo, FoamyI18n.t)
    readonly property color stateColor: !repo ? FoamyPalette.success
      : repo.complete === false || repo.syncError ? FoamyPalette.error
      : repo.dirtyCount > 0 ? FoamyPalette.warning
      : repo.behind > 0 ? FoamyPalette.info : FoamyPalette.success

    height: content.implicitHeight + FoamyStyle.rowPadding * 2
      + (lfsPanel.visible ? lfsPanel.implicitHeight + FoamyStyle.gap : 0)
    readonly property int rowStop: root.stops.rows[rowIndex] !== undefined ? root.stops.rows[rowIndex] : -1
    readonly property int infoStop: root.stops.infos[rowIndex] !== undefined ? root.stops.infos[rowIndex] : -1
    readonly property int trustStop: root.stops.trusts[rowIndex] !== undefined ? root.stops.trusts[rowIndex] : -1
    readonly property bool lfs: !!repo && repo.usesLfs === true
    readonly property bool trusted: !!repo && repo.trusted === true
    readonly property bool trustable: !!repo && !!repo.identity
    // The Git LFS note under the row: open while this is the view's note
    // (FoamyInfoNote keeps its own `expanded`, so follow it imperatively).
    readonly property bool noteWanted: lfs && !trusted && !unavailable && !!repo && root.lfsNotePath === repo.path
    onNoteWantedChanged: {
      lfsNote.expanded = noteWanted
      if (noteWanted) Qt.callLater(function() { list.positionViewAtIndex(repoRow.rowIndex, ListView.Contain) })
    }
    Component.onCompleted: lfsNote.expanded = noteWanted
    hasCursor: cursor.at(rowStop)
    onHovered: cursor.hover(rowStop)
    Accessible.role: Accessible.ListItem
    Accessible.name: repo ? String(repo.label) : ""
    Accessible.description: meta + (syncError ? " · " + syncError : "")

    RowLayout {
      id: content
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.topMargin: FoamyStyle.rowPadding
      anchors.leftMargin: FoamyStyle.rowPadding
      anchors.rightMargin: FoamyStyle.gapSmall
      spacing: FoamyStyle.rowPadding

      Rectangle {
        Layout.preferredWidth: 2
        Layout.preferredHeight: FoamyStyle.controlHeight
        radius: FoamyStyle.pillRadius(width)
        color: repoRow.stateColor
      }

      ColumnLayout {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        spacing: FoamyStyle.gapTiny
        Text {
          Layout.fillWidth: true
          text: repoRow.repo ? String(repoRow.repo.label) : ""
          textFormat: Text.PlainText
          elide: Text.ElideRight
          horizontalAlignment: Text.AlignLeft
          color: FoamyPalette.text
          font.family: FoamyStyle.fontFamily
          font.pixelSize: FoamyStyle.font.title
        }
        // Status line, then the last fetch error inline in the error colour:
        // "main · 3 changed · No origin remote configured…" (full text on hover).
        // The error keeps at least 40% of the line, else it gets its own
        // line, so a long branch never hides it.
        Flow {
          id: statusLine
          Layout.fillWidth: true
          Layout.minimumWidth: 0
          spacing: 0
          readonly property bool hasError: repoRow.syncError !== "" && !repoRow.unavailable
          readonly property real badgeSpace: lfsBadge.visible ? lfsBadge.width + FoamyStyle.gapSmall : 0
          readonly property bool errorInline: hasError
            && metaText.implicitWidth + badgeSpace + Math.min(errorMetrics.advanceWidth, width * 0.4) <= width
          TextMetrics { id: errorMetrics; font: errorText.font; text: " · " + repoRow.syncError }
          Text {
            id: metaText
            width: statusLine.errorInline ? implicitWidth
              : repoRow.unavailable ? statusLine.width : Math.min(implicitWidth, statusLine.width - statusLine.badgeSpace)
            height: Math.max(implicitHeight, lfsBadge.visible ? lfsBadge.height : 0)
            verticalAlignment: Text.AlignVCenter
            text: repoRow.meta
            textFormat: Text.PlainText
            wrapMode: repoRow.unavailable ? Text.WordWrap : Text.NoWrap
            elide: repoRow.unavailable ? Text.ElideNone : Text.ElideRight
            horizontalAlignment: Text.AlignLeft
            color: FoamyPalette.secondaryText
            font.family: FoamyStyle.fontFamily
            font.pixelSize: FoamyStyle.font.bodySmall
          }
          // Git LFS: an "LFS" button that opens a note (with Trust) under the
          // row, or an "LFS" chip once trusted.
          Item {
            id: lfsBadge
            visible: repoRow.lfs && !repoRow.unavailable
            width: (repoRow.trusted ? chip.width : lfsButton.width) + FoamyStyle.gapSmall
            height: repoRow.trusted ? chip.height : lfsButton.height
            Rectangle {
              id: chip
              visible: repoRow.trusted
              x: FoamyStyle.gapSmall
              width: chipText.implicitWidth + FoamyStyle.gap * 2
              height: chipText.implicitHeight + FoamyStyle.gapTiny * 2
              radius: FoamyStyle.pillRadius(height)
              color: Qt.alpha(FoamyPalette.success, 0.16)
              Accessible.role: Accessible.StaticText
              Accessible.name: FoamyI18n.t("Git LFS, trusted")
              Text {
                id: chipText
                anchors.centerIn: parent
                text: FoamyI18n.t("LFS")
                textFormat: Text.PlainText
                color: FoamyPalette.onSurface(FoamyPalette.success)
                font.family: FoamyStyle.fontFamily
                font.pixelSize: FoamyStyle.font.caption
                font.weight: Font.DemiBold
              }
            }
            FoamyAction {
              id: lfsButton
              objectName: "githubLfsButton"
              visible: !repoRow.trusted
              x: FoamyStyle.gapSmall
              implicitHeight: FoamyStyle.iconSize + FoamyStyle.gapSmall * 2
              iconName: "info"
              iconSize: FoamyStyle.iconSize - 2
              label: FoamyI18n.t("LFS")
              foreground: repoRow.noteWanted ? FoamyPalette.text : FoamyPalette.secondaryText
              color: active && (repoRow.noteWanted || hasCursor || hovered) ? FoamyPalette.hoverFill : "transparent"
              hasCursor: cursor.at(repoRow.infoStop)
              Accessible.name: FoamyI18n.t("This repository uses Git LFS")
              Accessible.checkable: true
              Accessible.checked: repoRow.noteWanted
              onHoveredChanged: if (hovered) cursor.hover(repoRow.infoStop)
              onClicked: {
                cursor.hover(repoRow.infoStop)
                root.toggleLfsNote(repoRow.repo)
              }
            }
          }
          Text {
            id: errorText
            visible: statusLine.hasError
            width: statusLine.errorInline ? Math.max(0, statusLine.width - metaText.width - statusLine.badgeSpace) : statusLine.width
            text: (statusLine.errorInline ? " · " : "") + repoRow.syncError
            textFormat: Text.PlainText
            elide: Text.ElideRight
            maximumLineCount: 1
            horizontalAlignment: Text.AlignLeft
            color: FoamyPalette.onSurface(FoamyPalette.error)
            font.family: FoamyStyle.fontFamily
            font.pixelSize: FoamyStyle.font.bodySmall
            Accessible.role: Accessible.AlertMessage
            Accessible.name: repoRow.syncError
            HoverHandler { id: errorHover }
            PanelToolTip {
              visible: errorHover.hovered && errorText.truncated
              text: repoRow.syncError
              fontFamily: FoamyStyle.fontFamily
            }
          }
        }
      }

      Row {
        spacing: FoamyStyle.gapTiny
        Layout.alignment: Qt.AlignVCenter
        FoamyAction {
          iconName: "arrow-up"
          tooltipText: FoamyI18n.t("Push %1", [repoRow.repo ? String(repoRow.repo.label) : ""])
          enabled: !root.busy && !!repoRow.repo && repoRow.repo.complete === true && repoRow.repo.ahead > 0
          foreground: FoamyPalette.secondaryText
          onClicked: root.service.pushRepository(repoRow.repo)
        }
        FoamyAction {
          iconName: "arrow-down"
          tooltipText: FoamyI18n.t("Pull %1", [repoRow.repo ? String(repoRow.repo.label) : ""])
          enabled: !root.busy && !!repoRow.repo && repoRow.repo.complete === true && repoRow.repo.behind > 0
            && repoRow.repo.dirtyCount === 0 && repoRow.repo.ahead === 0
          foreground: FoamyPalette.secondaryText
          onClicked: root.service.pullRepository(repoRow.repo)
        }
        FoamyAction {
          iconName: "eye"
          tooltipText: FoamyI18n.t("Open in lazygit")
          enabled: !!repoRow.repo
          foreground: FoamyPalette.secondaryText
          onClicked: root.repositoryRequested(repoRow.repo)
        }
      }
    }

    // The Git LFS note, under the status line and in line with it: what the
    // automatic checks skip, and Trust (when this filesystem allows it).
    Column {
      id: lfsPanel
      visible: repoRow.noteWanted
      anchors.top: content.bottom
      anchors.topMargin: FoamyStyle.gap
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: FoamyStyle.rowPadding * 2 + 2     // past the state bar
      anchors.rightMargin: FoamyStyle.rowPadding
      spacing: FoamyStyle.gap
      FoamyInfoNote {
        id: lfsNote
        width: parent.width
        showHeader: false
        focusable: false
        text: FoamyI18n.t("Automatic checks don't run a repository's own filters, so LFS files you edited may show as changed until Git refreshes.")
          + (repoRow.trustable ? "" : "\n" + FoamyI18n.t("Trust isn't available for this repository's filesystem."))
      }
      FoamyButton {
        objectName: "githubTrustButton"
        visible: repoRow.trustable
        text: FoamyI18n.t("Trust this repository")
        iconName: "check"
        hasCursor: cursor.at(repoRow.trustStop)
        onHoveredChanged: if (hovered) cursor.hover(repoRow.trustStop)
        onClicked: root.activate(repoRow.trustStop)
      }
    }
  }
}
