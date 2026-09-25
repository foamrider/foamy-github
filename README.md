# Foamy GitHub

Local Git repository status, fetch, push, and pull.

![Foamy GitHub screenshot](screenshot.png)

## Install

```sh
omarchy plugin add https://github.com/foamrider/foamy-github.git --enable
```

## Use

1. Left-click the widget, open the cog, and add your repository folders.
2. Choose the folder search depth, refresh intervals, and language.
3. Use refresh (or middle-click the widget) to fetch remotes and update status.
4. Use a repository's arrows to push or pull, or its eye button to open lazygit.

Uses your existing Git authentication. Pull requires a clean, non-diverged
branch with an upstream and uses fast-forward only. Works with any Git host.

## License

Licensed under [MIT](LICENSE), with [Omarchy](LICENSE-OMARCHY) and
[Lucide](LICENSE-LUCIDE) notices.

Provided **as is**, without warranty or guaranteed support. Use at your own risk.
