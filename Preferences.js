// Match the language choices used by the other Foamy plugins.
var norwegian = {
  "Language": "Språk",
  "Default (system language)": "Standard (systemspråk)",
  "Settings": "Innstillinger",
  "Settings (S)": "Innstillinger (S)",
  "Back": "Tilbake",
  "Repository folders": "Prosjektmapper",
  "You can add up to 32 directories.": "Du kan legge til opptil 32 mapper.",
  "Add folder": "Legg til mappe",
  "Browse for folder": "Velg mappe",
  "Remove folder": "Fjern mappe",
  "Repository folder %1": "Prosjektmappe %1",
  "Scan levels for %1": "Søkenivåer for %1",
  "%1 level": "%1 nivå",
  "%1 levels": "%1 nivåer",
  "Detect changes automatically (Recommended)": "Oppdag endringer automatisk (Anbefalt)",
  "Fallback interval": "Reserveintervall",
  "Interval": "Intervall",
  "Disabled": "Deaktivert",
  "Live monitoring unavailable; automatic local refresh is disabled.": "Direkteovervåking er utilgjengelig; automatisk lokal oppdatering er deaktivert.",
  "Live monitoring unavailable; using timed refresh.": "Direkteovervåking er utilgjengelig; bruker tidsstyrt oppdatering.",
  "Remote fetch": "Hent fra eksterne kilder",
  "Every 10 seconds": "Hvert 10. sekund",
  "Every 30 seconds": "Hvert 30. sekund",
  "Every minute": "Hvert minutt",
  "Every 5 minutes": "Hvert 5. minutt",
  "Every 15 minutes": "Hvert 15. minutt",
  "Every 30 minutes": "Hvert 30. minutt",
  "Every hour": "Hver time",
  "Could not open the folder browser. Enter a path manually.": "Kunne ikke åpne mappevelgeren. Skriv inn en sti manuelt.",
  "Could not read the installed plugin ID.": "Kunne ikke lese ID-en til det installerte tillegget.",
  "Could not save settings. Try again.": "Kunne ikke lagre innstillingene. Prøv igjen.",
  "Use at most 32 repository folders.": "Bruk maksimalt 32 prosjektmapper.",
  "Use an absolute path or ~/ for every folder.": "Bruk en absolutt sti eller ~/ for hver mappe.",
  "Choose a scan depth from 0 to 5 levels.": "Velg en søkedybde fra 0 til 5 nivåer.",
  "Synchronizing repositories": "Synkroniserer prosjekter",
  "Repository check needs attention": "Prosjektkontrollen krever oppfølging",
  "Checking repositories…": "Kontrollerer prosjekter…",
  "No repositories found": "Fant ingen prosjekter",
  "Everything is in sync": "Alt er synkronisert",
  "Working trees are clean": "Ingen lokale endringer",
  "%1 repository needs attention": "%1 prosjekt krever oppfølging",
  "%1 repositories need attention": "%1 prosjekter krever oppfølging",
  "Last remote sync %1": "Siste eksterne synkronisering: %1",
  "Left: details · Middle: refresh": "Venstreklikk: detaljer · Midtklikk: oppdater",
  "ahead": "foran",
  "behind": "bak",
  "changed": "endret",
  "failed": "mislyktes",
  "remote state may be stale": "ekstern status kan være utdatert",
  "Repository status is unavailable. Try Refresh again.": "Prosjektstatus er utilgjengelig. Prøv å oppdatere igjen.",
  "Checking local repositories…": "Kontrollerer lokale prosjekter…",
  "No local repositories are being tracked.": "Ingen lokale prosjekter overvåkes.",
  "All local repositories are clean and aligned with their cached upstream state.": "Alle lokale prosjekter er uten endringer og samsvarer med lagret ekstern status.",
  "Last sync issues": "Siste synkroniseringsproblemer",
  "Repository": "Prosjekt",
  "repository": "prosjekt",
  "Synchronization failed": "Synkronisering mislyktes",
  "%1 repository": "%1 prosjekt",
  "%1 repositories": "%1 prosjekter",
  "Updated just now": "Oppdatert nå",
  "Updated 1 minute ago": "Oppdatert for 1 minutt siden",
  "Updated %1 minutes ago": "Oppdatert for %1 minutter siden",
  "Fetching repositories…": "Henter prosjekter…",
  "Refreshing local status": "Oppdaterer lokal status",
  "Fetch remotes and refresh status (R)": "Hent eksterne endringer og oppdater status (R)",
  "Push %1": "Send %1",
  "Pull %1": "Hent %1",
  "View in lazygit": "Vis i lazygit",
  "Never": "Aldri",
  "Just now": "Akkurat nå",
  "%1m ago": "for %1 min siden",
  "%1h ago": "for %1 t siden",
  "%1d ago": "for %1 d siden",
  "detached": "frakoblet",
  "%1 commit": "%1 innsending",
  "%1 commits": "%1 innsendinger",
  "%1 changed": "%1 endret",
  "Updated %1": "Oppdatert %1",
  "Skipped %1": "Hoppet over %1",
  "Failed %1": "Mislyktes %1",
  "Waiting for network…": "Venter på nettverk…",
  "Fetching and safely updating…": "Henter og oppdaterer trygt…",
  "Pushing %1…": "Sender %1…",
  "Pulling %1…": "Henter %1…",
  "Everything already current": "Alt er allerede oppdatert",
  "Fetch complete": "Henting fullført",
  "Some repositories could not be synchronized": "Noen prosjekter kunne ikke synkroniseres",
  "Repository synchronization is already running": "Prosjektsynkronisering pågår allerede",
  "Failed to read repository status": "Kunne ikke lese prosjektstatus",
  "Repository status failed": "Kunne ikke hente prosjektstatus",
  "Repository synchronization failed": "Prosjektsynkronisering mislyktes",
  "Repository action complete": "Prosjekthandling fullført",
  "Repository action failed": "Prosjekthandling mislyktes",
  "No repository status received": "Ingen prosjektstatus mottatt",
  "Invalid repository status": "Ugyldig prosjektstatus",
  "Failed to parse repository status": "Kunne ikke tolke prosjektstatus",
  "Choose a repository folder": "Velg en prosjektmappe",
  "Choose": "Velg"
}

function languageSetting(settings) {
  var mode = settings ? settings.language : undefined
  return mode === "en" || mode === "nb" ? mode : "system"
}

function language(mode, locale) {
  return mode === "en" || mode === "nb" ? mode
    : /^(nb|nn|no)(_|-|$)/i.test(String(locale || "")) ? "nb" : "en"
}

function text(label, lang, values) {
  var translated = lang === "nb" && Object.prototype.hasOwnProperty.call(norwegian, label) ? norwegian[label] : label
  // Replace placeholders once so repository names containing %1 or $ stay literal.
  return translated.replace(/%([1-9][0-9]*)/g, function(token, index) {
    return values && Number(index) <= values.length ? String(values[Number(index) - 1]) : token
  })
}

if (typeof module !== "undefined") module.exports = {
  languageSetting: languageSetting, language: language, text: text
}
