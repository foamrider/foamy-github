# Foamy GitHub

Local Git repository status, fetch, push, and pull.

![Foamy GitHub screenshot](preview.png)

## Install

Requires Omarchy 4.5 (also loads on 4.0.6, in English), Git, Python 3, Bash,
util-linux `setpriv` with Landlock and `bubblewrap` (all standard on Omarchy),
`jq` and `wl-clipboard`. Opening a repository requires `lazygit` and Omarchy's
terminal launcher; **Browse for folder** uses `qml6` and `hyprctl`.

```sh
omarchy plugin add https://github.com/foamrider/foamy-github.git --enable
```

## Use

1. Left-click the widget, open **Settings** (cog or `s`) and add your repository folders.
2. Choose how many levels deep to search each folder, and the refresh intervals.
3. Refresh (header button, `r`, or middle-click the widget) fetches remotes and updates status.
4. A repository's arrows push or pull; the terminal button (or Enter) opens it in lazygit.

The popup lists repositories that need attention: ahead, behind, changed, or
whose last fetch failed. A failed fetch is shown on the repository's row, in
red after its status line (for example `main · 10 changed · No origin remote
configured…`); hover it for the full message.

Keyboard: arrows or `hjkl` move one cursor through the rows, Refresh and
Settings (hovering moves the same cursor); Enter activates; `r` refreshes;
`s` opens Settings; Esc closes (or leaves Settings); Tab and Shift+Tab move to
the next bar panel.

## Settings

| Setting | Default | |
|---|---|---|
| Repository folders | none | Folders to search, each with a search depth of 0 to 5 levels (up to 32 folders) |
| Detect changes automatically | on | Updates through filesystem notifications (inotify); recommended |
| Refresh every | 30 seconds | Timed refresh; with automatic detection it is only the fallback. **Off** turns polling off |
| Fetch remotes every | 15 minutes | Background `git fetch` of every repository |
| Git LFS: trust per repository | none | Shown only when a Git LFS repository is found. See [Trust model](#trust-model) |

Settings save in place (`bar.shell.updateEntryInline`, else `omarchy bar set`).

**Detect changes automatically** groups changes saved close together over
300 ms and checks only the affected repositories. Ignored dependency/build
directories and Git object stores aren't watched; Git worktrees and new
repositories within the search depth are picked up. Opening the popup doesn't
rescan while monitoring is live. If monitoring fails (for example, the system
watch limit is reached), the panel says so, uses the timed refresh, and
retries monitoring after 30 seconds, then 1, 2, 4… up to 30 minutes while it
keeps failing.

On Omarchy 4.5, which draws a bar on every monitor, the status helper,
filesystem watcher, timers and IPC target run once per shell, not once per
monitor.

## Trust model

The widget runs Git by itself, in every repository it finds: `status`,
`ls-files` and `rev-parse` on every change, and `git fetch` on a timer. A
repository's own Git config (`.git/config`, `config.worktree` and any file they
include) and its attributes can make Git run programs. A repository unpacked
from an untrusted archive into one of your folders could otherwise run code
just by being there.

So unless you trust that repository, automatic status checks run with
overrides that Git applies after all config files and includes. They are
passed as `GIT_CONFIG_COUNT`/`KEY`/`VALUE` pairs, so key names can't be
misparsed. Automatic fetches don't read the repository's config at all; see
below.

| Vector | Status checks | Automatic fetch |
|---|---|---|
| `core.fsmonitor`, untracked cache | off | off |
| Hooks (`core.hooksPath`, `.git/hooks`) | off (`/dev/null`) | off |
| Clean/smudge/process filters, including global ones such as Git LFS that the repository's attributes select (LFS reads repository settings like `lfs.extension.*`) | off for every defined filter | off |
| Submodule worktrees | not inspected | not fetched |
| `credential.helper` (repository-local) | — | never read (only your global/system config applies) |
| `core.sshCommand` (repository-local) | — | never read: custom SSH commands in a repository's own config apply only to trusted repositories. Your global one, `GIT_SSH_COMMAND`, or `ssh -o BatchMode=yes` is used |
| `core.askPass`, prompts | — | off: `GIT_ASKPASS=""` unless you set it, `SSH_ASKPASS_REQUIRE=never`, SSH `BatchMode` |
| `core.gitProxy` (repository-local) | — | never read |
| Transports (`ext::`, `fd::`, `file://`, remote helpers, `remote.*.vcs`, `protocol.*`, every subsection spelling) | none | only http(s), ssh and git, plus remote helpers your own config allows `always` (`user` isn't enough for an automatic fetch) |
| Local origins (a path, `file://`, relative, `~`, or an `insteadOf` rewrite to one) | — | skipped: "Automatic fetch skipped: local origins need a trusted repository." Trusted repositories fetch them |
| Symlinked or foreign-owned Git-directory paths (`objects`, `refs/remotes/origin`, `packed-refs`, `shallow`, …), `objects/info/alternates` | — | fetch refused with a message (alternates: untrusted only) |
| On-demand fetches of missing objects from a promisor remote (partial clone) | off (`GIT_NO_LAZY_FETCH=1`, and no transport on older Git). Trusted repositories fetch them like Git in a terminal | — |
| `remote.*.uploadpack` | — | `--upload-pack=git-upload-pack` |
| `http.*` / `remote.*.proxy` (proxy, TLS, CA, cookies, extra headers, resolve) | — | never read: your own global/system values (for example a private CA) apply as usual |
| `core.alternateRefsCommand` | — | neutral |
| Automatic maintenance / gc (`gc.recentObjectsHook`, `maintenance.*`, `gc.auto`) | — | not run (`--no-auto-maintenance`) |
| Commit-graph write | — | off |

Include files (`include.path`, `includeIf`) are covered: the overrides come
after them.

The overrides are worked out from a snapshot of the repository's config, but
each Git command reads the config again. A repository changed during a check,
for example by an include adding a filter that `.gitattributes` already
selects, could slip past them. So untrusted **status checks** also run under a
[Landlock](https://docs.kernel.org/userspace-api/landlock.html) execute
restriction (`setpriv` from util-linux): only the Git binary and its loader
may be executed, so no filter, hook, shell or helper can start at all.

A plain filter that can't run passes the content through, so counts stay
right. A `required` one fails the check, which then shows **Status
unavailable**. This costs about 1.5 ms per repository check (10.4 vs 8.9 ms,
7 Git calls).

**Automatic fetches** can't run under that restriction, because they need ssh
and your credential helpers. Instead, Git fetches the repository in place,
with a read-only minimal config mounted over the repository's own (in a
private mount namespace, see below). That config holds only the snapshot's
remote URLs (none when the origin comes from your own config), the origin's
fetch refspecs and tag option, repository format and partial-clone settings.
One function decides which keys survive, for this and the status overlay.

Git reads your system and global config natively, against the repository's
real path. So `includeIf` (gitdir, onbranch, hasconfig), bare booleans, empty
resets, `insteadOf`, relative and `~` origins all behave exactly as in a
terminal. The repository's own config is never read during a fetch. Hooks
are off, and objects, refs and the shallow file are written by Git itself.
Existing tags and `origin/HEAD` are left alone. Reftable, SHA-256, worktrees,
shallow and partial clones work as usual.

**Pinned repositories.** A repository's directories are opened once, and
its identity (device, inode, birth time) is taken from those open
directories. Git then runs in a private mount namespace (`bubblewrap`) where
the repository's paths are bound to exactly those directories. A rename or
symlink swap during a check therefore can't make a trusted check run another
repository's config, and can't redirect a fetch's objects or ref updates. In that namespace the whole filesystem and the worktree are read-only;
only the pinned Git directories are writable. `/dev` is a fresh minimal one
(null, zero, full, random, urandom, tty), so nothing of the host's `/dev`,
`/dev/shm` included, is reachable. It has its own process, IPC and UTS
namespaces and `/proc`, so no outside process is visible (and
`/proc/<pid>/root` can't lead back to the host's mounts), and it ends with
the helper. `/tmp` and `/run` (the ssh agent socket) are the read-only host
view, and the network is shared. Only your own user ID exists inside, so
root-owned files appear owned by `nobody`. ssh refuses included config files
like that, so a copy of `/etc/ssh/ssh_config.d` owned by you, with the same
content, is mounted over it. So a symlink anywhere inside
them that points into another repository (for example below
`refs/remotes/origin`) can't be written through, and the fetch fails
instead. Helpers that run there (ssh, your credential helpers) can read what
they need but can't write outside the Git directory. This
applies to trusted checks and fetches, and to every automatic fetch. The
pin comes first: the trust check and the config snapshot are read through
it. Every Git command gets `GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE`
pointing at the pinned directories, and read-only copies of the `.git` file
and `commondir` (as read when pinning) are mounted over the originals, so
rewriting either can't send Git elsewhere. The
identity covers the worktree as well as the Git and common directories, so
a different worktree whose `.git` file points back at the original Git
directory isn't trusted either. Trust covers one repository: submodule
worktrees aren't inspected or fetched in automatic runs, even for trusted
repositories. Without
bubblewrap, trusted repositories are checked like untrusted ones, and
automatic fetches of untrusted repositories are skipped with a message.

With bubblewrap, untrusted status checks also run in the pinned, read-only
namespace with a frozen minimal config mounted over the repository's own. It
holds only the repository format, worktree and ignore settings, the
branch/remote mapping for upstream counts, and the remote URLs; there are no
filter, fsmonitor, hooks or other driver keys. The URLs are only context, so
your own `includeIf "hasconfig:remote.*.url:…"` matches as in a terminal (an
included `core.excludesFile` still applies). Untrusted status checks never
contact a remote: no transport is allowed and a partial clone's missing
objects are never fetched on demand (`GIT_NO_LAZY_FETCH`). Trusted ones
behave like `git status` in a terminal and may fetch them. A mid-check edit to the repository's config is
therefore invisible to Git, and Landlock stays on as a second layer. Values
are kept as Git read them: a bare boolean (`filemode` with no value, which is
true) stays a bare key, not an empty value (false).

Without bubblewrap the checks fall back to the snapshot overrides plus
Landlock, which leaves a race: someone who can already write into the
repository could change its config during a check. Where `setpriv` or Landlock is missing (Omarchy's kernel and util-linux have
both), status checks fall back to the snapshot overrides alone. A repository
rewritten during a check could then still run a filter; the trust settings
can't close that gap. Your global Git config, credential helpers (for example `gh auth
git-credential`) and SSH agent keep working. Repository-local credential
helpers and unusual repository-local SSH commands don't apply to automatic
fetches: such a repository shows the authentication error on its row, and you
can fetch it in lazygit. A partial clone whose status needs a missing object
shows an error on its row instead of downloading it. Your global
`core.hooksPath` hooks don't run for automatic commands. Files tracked with Git LFS that you touched can count as
changed until Git refreshes its index. **Push and pull** are explicit actions
on one repository and run with its normal Git config and hooks, exactly like
`git push` in a terminal.

Requires Git 2.31 or later (`GIT_CONFIG_COUNT`); `--no-auto-maintenance`
needs 2.29.

### Per-repository trust (Git LFS)

Trust is per repository. It only matters for repositories that use Git LFS,
because there the hardened checks can't run LFS's own filter. A repository
counts as using LFS when its top-level `.gitattributes` (or the Git
directory's `info/attributes`) sets `filter=lfs`, or it has a top-level
`.lfsconfig`. Detection is a few bounded, no-follow file reads: no Git
command, nothing from the repository runs. Live monitoring reads them again
when it rescans a repository (found, refreshed, or one of those files
changed), not on every update. An `lfs/` folder in the Git directory doesn't
count: git-lfs creates one in any repository it touches and keeps it after a
repository stops using LFS. A `.gitattributes` only in a subdirectory isn't
seen.

- **Untrusted LFS repository:** an (i) after its status line explains that LFS
  files you edited may show as changed until Git refreshes. Hover it, or move
  the keyboard cursor onto it, then choose **Trust this repository** (Enter).
- **Trusted:** a quiet **LFS** chip replaces the (i). That repository runs its
  own Git config, including LFS, hooks and filters, in automatic checks and
  fetches, so its counts are exact.
- **Settings → Git LFS** lists only LFS repositories, each with a toggle. The
  section is hidden when none is found.

Trust is stored as the repository's real path (symlinks resolved) plus the
identity of its Git directory: device, inode and birth time
(`trustedRepositories`). A moved or renamed repository is untrusted again, and
so is a different repository that later appears at the same path (deleted and
re-cloned, or swapped in). It shows the (i) again. On a filesystem without a
birth time (`statx` reports none), device and inode alone could be reused by
a replacement, so trust isn't offered there: the (i) and the Settings row say
so and stay untrusted. The identity offered is the one of the directories the
check actually inspected (its pin). When you trust a repository, the panel
pins it again and saves trust only if the identity still matches; otherwise
it says "Repository changed; refresh and try again" and saves nothing.
Repositories that don't use LFS never show any of this.

**From the earlier global switch:** if you had turned on **Trust repository
config** (an earlier build of 1.1) for all repositories, the panel migrates it
once. After
the first complete scan, the LFS repositories found then become trusted and
the global switch is cleared. Non-LFS repositories lose nothing visible: trust
only changed fsmonitor speed and hooks for them.

## Limits

Repository checks stream Git status without retaining file lists. A timeout,
resource limit or Git error shows **Status unavailable** for that repository
and disables push/pull until a fresh check succeeds; other repositories keep
updating. An incomplete folder scan reports an error; it never silently lists a
subset.

Checks allow 16 MiB of Git output and 15 seconds per status command, with at
most four commands at once. Folder discovery allows 256 repositories and
100,000 directory entries within five seconds per traversal. Monitoring allows
8,192 watches and 32,768 repository-to-directory associations. Shell messages
and cached sync state are limited to 8 MiB. Pull requires a clean, non-diverged
branch with an upstream and is fast-forward only. Works with any Git host.

Repository-folder paste reads at most 16 KiB with a two-second deadline.

## Remove

```sh
omarchy plugin remove foamy.github
```

Removal stops repository monitoring and scheduled fetches (the helpers stop
their Git processes when the shell stops them). Your repositories,
Git configuration, authentication, and cached status remain on disk.

Omarchy manages the plugin entry in `shell.json`. Packages and data outside
the plugin directory are retained unless you remove them separately.

## License

Licensed under [MIT](LICENSE), with [Omarchy](LICENSE-OMARCHY) and
[Lucide](LICENSE-LUCIDE) notices.

Provided **as is**, without warranty or guaranteed support. Use at your own risk.

## Language

Language follows **Omarchy → Setup → Language** on Omarchy 4.5 (English and
Norwegian catalogs; other languages fall back to English). Omarchy 4.0 shows
the plugin in English.

## Development

`kit/` holds shared components copied from [foamy-kit](https://github.com/foamrider/foamy-kit); edit them there, not here.

```sh
node --test tests/model.test.js tests/preferences.test.js && node tests/i18n.test.cjs
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/kit-check.sh
python3 tests/i18n-runtime.py                     # OMARCHY_PATH=<4.5 tree> for 4.5
python3 tests/runtime-panel.py                     # offscreen, needs foamy-kit
~/GitHub/Personal/foamy-kit/preview/render.py . --host both
```

Shared components in `kit/` come from [foamy-kit](https://github.com/foamrider/foamy-kit);
don't edit them here.
