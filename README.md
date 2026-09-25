# Foamy GitHub

A compact Git repository plugin for Omarchy Quattro, using the `foamy.github`
namespace. View local changes and upstream differences, fetch remotes, push,
pull with fast-forward only, or open a repository in lazygit.

## Installation

```sh
omarchy plugin add https://github.com/foamrider/foamy-github.git --enable
```

Requires Omarchy Quattro, Python 3, Git, Bash, jq, coreutils, and Qt Quick Dialogs
(`qml6`). Opening repositories also requires lazygit and Omarchy's terminal
launcher. No Python packages, API tokens, or GitHub CLI are required. Remote
operations use your existing Git authentication and the `origin` remote.

No folders are configured by default. Open the cog and add your repositories.
The plugin works with local Git repositories hosted on GitHub or other Git hosts.
It does not create repositories or configure remotes.

## Controls

- Left-click opens the overview; middle-click refreshes and fetches remotes.
- The refresh icon or **R** fetches remotes, then checks local status.
- The row arrows push or pull; the eye opens lazygit.
- **S** opens settings. **Escape** returns to the overview or closes the popup.
- Arrow keys or **H/J/K/L** select a repository; Enter opens it in lazygit.
- Operation progress replaces the title with animated dots. Completion stays
  visible for five seconds before the normal title returns.

Opening the panel and the local refresh timer only inspect local Git state.
Remote fetch updates remote-tracking refs without changing working files.
Pull requires a clean, non-diverged branch with an upstream and uses fast-forward
only. Missing remotes and other Git failures appear under Last sync issues.

## Settings

All preferences live on the `foamy.github` widget entry in Omarchy's
`$XDG_CONFIG_HOME/omarchy/shell.json` (`~/.config` by default). The plugin saves
through Omarchy's `setBarWidget` IPC method, which updates both live settings and
shell.json. It does not maintain a separate preferences file.

- **Language:** system language (default), English, or Norsk bokmål. System
  language uses Norwegian for `nb`, `nn`, or `no` locales and English otherwise.
  Labels update when the setting is saved. Repository names, paths, and messages
  from Git and the repository helper keep their original text.
- **Repository folders:** use **+** to add a row. Browse with the folder icon or
  enter an absolute path or `~/` path. Choose how many levels below the folder
  to search: **0** checks only that folder, **1** includes immediate children,
  up to **5**. New rows start at 2 levels. The trash icon removes a row.
- Paths autosave on Enter or focus loss. Browsing, depth changes, and deletions
  save immediately. Empty paths are ignored; an empty list shows one blank row.
  At 32 rows, **+** is disabled and its tooltip explains the limit.
- **Local status refresh:** defaults to 30 seconds.
- **Remote fetch:** defaults to 15 minutes. Manual refresh fetches immediately.

The advanced settings keys are `language` (`system`, `en`, or `nb`),
`repositoryFolders` (an array of
`{ "path": "~/Projects", "depth": 2 }`, default `[]`), `refreshIntervalSec`
(10–3600 seconds), and `fetchIntervalSec` (300–86400 seconds). Legacy string
folder entries use two levels. Interval dropdowns offer common presets.

Discovery skips hidden child directories, `node_modules`, and child symlinks.
A configured folder may itself be a repository, including a Git worktree or a
hidden directory. Missing folders are ignored. Overlapping selections are
combined without duplicate repositories; discovery stops at each repository.
Selected folders also bound the repositories available to push and pull.

Runtime sync results and locks are stored in `$XDG_CACHE_HOME/foamy-github`
(`~/.cache/foamy-github` by default). This local cache can contain repository
paths and Git error messages; it is not part of the plugin distribution.

## Local development

From this checkout, with no existing plugin at the destination:

```sh
omarchy plugin validate .
mkdir -p ~/.config/omarchy/plugins
ln -s "$PWD" ~/.config/omarchy/plugins/foamy.github
omarchy restart shell
omarchy plugin enable foamy.github
```

Disable another repository widget before enabling this one. Restart the shell
after source edits before live checks. IPC is available through
`omarchy-shell foamy.github open`, `close`, `toggle`, `refresh`, and `status`.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.js
bash -n folder-picker.sh
qmllint -I /usr/share/omarchy/shell Panel.qml Service.qml Github*.qml FolderPicker.qml
omarchy plugin validate .
```

## License and attribution

MIT-licensed; see [LICENSE](LICENSE). The popup and dropdown are adapted from
Omarchy's `Ui/KeyboardPanel.qml` and `Ui/Dropdown.qml`; retain
[LICENSE-OMARCHY](LICENSE-OMARCHY). The UI follows
[Foamy Weather](https://github.com/foamrider/foamy-weather) and
[Foamy Syncthing](https://github.com/foamrider/foamy-syncthing). Inline icons derive
from Lucide/Feather; retain [LICENSE-LUCIDE](LICENSE-LUCIDE).
