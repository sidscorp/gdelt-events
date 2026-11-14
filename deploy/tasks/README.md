# deploy/tasks — generated snapshot, do not hand-edit

The scheduled-task definitions here are **output**, not configuration. The
source of truth is the production Windows host. These files exist so the
host can be rebuilt after a disk loss, and so that a change to a live task
shows up as a reviewable diff.

Producing them by hand is what went wrong before: `GDELT-Ingest.xml` sat in
this directory describing "wscript, hourly" for months while the live task ran
"python, every 15 minutes", and `GDELT-EmbedWatchdog.xml` claimed the watchdog
was enabled while the host had it disabled. Both were fixed by re-exporting
rather than by editing the file.

## Re-export

On the host, from the repository root:

```powershell
powershell -File deploy\export_tasks.ps1
git diff --stat deploy/tasks     # any diff here is real drift
```

The script only reads task definitions (`Export-ScheduledTask`); it never
starts, stops or registers anything. It writes every task whose name matches
`^(GDELT-|Ollama)` and deletes XML for tasks that no longer exist, so the
directory always mirrors the host exactly.

It normalises the output so repeated exports of an unchanged host are byte
identical:

- UTF-8 with a BOM, and the XML declaration corrected to `encoding="UTF-8"`
  (Task Scheduler exports declare `UTF-16`; storing UTF-16 in git makes the file
  an undiffable binary blob).
- LF line endings, one trailing newline.

A `StartBoundary` that differs only in its UTC offset (`-04:00` vs `-05:00`) is
the host re-rendering the same instant across a DST boundary, not drift.

## Import

`schtasks /create /xml` expects a UTF-16 export, so it rejects these files.
Use the cmdlet, which ignores the declaration:

```powershell
Register-ScheduledTask -TaskName 'GDELT-Ingest' -Xml (Get-Content -Raw deploy\tasks\GDELT-Ingest.xml)
```

Prefer the registration scripts below where they exist: they also create the
VBS shim, log directory or working directory that the task depends on. An XML
import restores the action and triggers but nothing beside them.

## What has a generator in this repo

| Task | Registered by |
|---|---|
| `GDELT-Ingest` | `deploy/register_task.ps1` |
| `GDELT-Dashboard` | `deploy/register_dashboard.ps1` |
| `GDELT-Dashboard-Dev` | `scripts/register_dev_task.ps1` |
| `GDELT-SecIngest` | `scripts/register_sec_task.ps1` |
| `GDELT-SocialPublisher` | `scripts/register_social_task.ps1` |
| `Ollama-Serve` | `scripts/register_ollama_task.ps1` |

Eleven tasks have no generator at all — this snapshot is the only record of
them: `GDELT-BriefingAudit`, `GDELT-BriefingPrewarm`,
`GDELT-BriefingPrewarm-Dev`, `GDELT-Cloudflared`, `GDELT-Cluster`,
`GDELT-EmbedNewArticles`, `GDELT-EmbedWatchdog`, `GDELT-FeedWarm`,
`GDELT-PruneEmbeddings`, `GDELT-RebuildFaiss`, `OllamaServe`.

Those eleven are the reason this directory still exists. If you add a generator
for one of them, add it to the table; once every task has one, this folder can
be deleted and the register scripts become the single source of truth.
