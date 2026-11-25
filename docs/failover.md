---
title: Server Failover
description: What happens when the FL server is lost, how to restart it or have a standby site take over, how an interrupted run is resumed, and the warning when a site drops out.
---

# Server failover and resuming a run

A SwarmMed network has one NVIDIA FLARE server. In a swarm run the server does
not aggregate, but it starts and monitors each job and relays every message
between the sites, model updates included (see
[Background](background.md)). This page describes what happens when that
server is lost and how to recover. If only the server has stopped and its host
is still there, restart the server where it ran and resume the interrupted run
from a checkpoint ([Restarting the server](#restarting-the-server-where-it-ran)).
If the host or its network is lost, SwarmMed provides a standby server, started
by hand at a site agreed in advance, and the same resume. Together they make a
run recoverable after the server is lost. They do not remove the server as a
single point of failure: there is always exactly one server, a restart or a
switch interrupts the run, and switching to another server needs an operator at
every site. The page also describes the warning SwarmMedHub gives when a site
drops out of a run ([When a site drops out](#when-a-site-drops-out)).

## What happens when the server is lost

* The sites can no longer exchange models, so the run stops.
* FLARE keeps no state that lets a restarted or replacement server continue a
  swarm job. A restarted server tells the sites to abort the job when they
  reconnect. SwarmMed starts every FL server, a restarted one or a standby, with
  a new job store, so a restarted server does not list the interrupted job at
  all. Start an FL server again only with SwarmMed's commands
  ([Restarting the server](#restarting-the-server-where-it-ran)), not with
  `docker restart` or `docker start`: after a bare `docker restart` of an FL
  container, NVIDIA FLARE finds its old process-ID file and refuses to start.
  If a server container is nevertheless brought back by hand with its old job
  store, NVIDIA FLARE 2.7.1, in the image SwarmMed builds, and the FLARE build
  in the worked example's image mark the job as abandoned
  (`FINISHED:ABANDONED`), and the FLARE build in MediSwarm 1.9.3's image keeps
  listing it as running.
* NVIDIA FLARE's own failover support (one active server with standby servers,
  chosen by an "overseer") was retired in FLARE 2.6 to 2.8 and never continued a
  swarm job across a switch. SwarmMed runs FLARE 2.7.1 and cannot use it.
* The loss of one training site also stops a run, because every site must
  return a result in every round (see
  [SwarmMed and NVFlare](nvflare-comparison.md#time-limits-and-failure-behaviour)).
  The tools on this page also help in that case: once the site is back, the
  run can be resumed ([When a site drops out](#when-a-site-drops-out)).

## Restarting the server where it ran

When the FL server's process or container has stopped but its host and the
host's network are fine, for example because the container stopped or the host
was restarted, start the server again at the same host:

1. Make sure that no standby server has taken over the network in the meantime:
   ask the standby site, or check the study's operating procedure. If one has,
   do not start the server here; see
   [When the Hub that ran the server comes back](#when-the-hub-that-ran-the-server-comes-back).
2. At the coordinating Hub, stop the network and start it again the same way as
   it was started:

    ```bash
    swarmed network stop <NETWORK_UUID> --wait
    swarmed network start <NETWORK_UUID> --wait
    ```

    The Hub's Celery worker must have the image settings
    (`SWARMMEDHUB_FLARE_IMAGE`, `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE`) and the
    start mode (`SWARMMEDHUB_SERVER_ONLY_MODE`) that the server was started
    with; for a server started as in the
    [worked example](reproduce.md#4-start-the-fl-server-coordinating-host), run
    that command again. [Resuming over a slow link](#resuming-over-a-slow-link)
    explains what goes wrong otherwise. If the resumed job will cross slow
    links, set `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` for this start as well.
3. The other sites need to do nothing. Their FL clients keep running and
   reconnect to the restarted server by themselves: the server takes them back
   on the tokens it gave them before, which it signed with its private key, so
   they do not register again. In a test across three hospitals, the FL server
   was restarted at its host while the sites' clients kept running, and all three
   reconnected without any action at the sites.
4. Resume the interrupted run ([Resuming the run](#resuming-the-run)). The
   restarted server has told the sites to abort the job and does not list it,
   so the resume needs no `--force`. If the server container came back by hand
   with its old job store instead, the FLARE build in MediSwarm 1.9.3's image
   still lists the job as running: `swarmed training resume` then refuses, and
   `swarmed training stop` does not change that. Once you have made sure that
   no other FL server runs for the network, resume with `--force`.

If the server runs at a standby site after a switch, restart it there with the
command that started it, once you have made sure that no other FL server runs
for the network. As at the coordinating Hub, the site's Celery worker must have
the image settings that the server was started with.

```bash
swarmed network failover <NETWORK_UUID> --server-host <STANDBY_ADDRESS> --start-server --wait
```

A site's worker runs in client-only mode, so `swarmed network start` would
start only the site's clients there, and `swarmed network stop` would stop the
server as well.

Use the standby server only when the server's host or its network is lost.

## What SwarmMed provides

* **A standby server.** Every site's startup package contains the FL server's
  kit, including its private key (see [Threat model](threat-model.md)). Packages
  exported by this version also contain the server's local settings. Any site
  can therefore start the server; the consortium agrees in advance which one
  does.
* **Round-start checkpoints.** At the start of each round, every training site
  writes the model that round starts from to `swarmmed_resume_checkpoint.pt` in
  the job's app folder, overwriting the previous one, with the round number in
  `swarmmed_resume_checkpoint.json`. For jobs submitted with SwarmMed 1.0.6 or
  later, both files also hold the run's total number of rounds, and every site's
  copy of the job holds it in `config/swarmmed_run.json` of its app folder. The
  first round of a new run starts from an empty model and writes none. The file
  is as large as the model (94 MB for the MST model of the worked example).
  FLARE itself saves the global model (`FL_global_model.pt`) only at the site
  that aggregated the round.
* **Resume.** `swarmed training resume` submits a new job that starts at the
  interrupted round from the newest checkpoint at the site and runs the rounds
  that are left. The round numbers continue those of the interrupted run. Any
  training site can resume the run, the standby included, whether or not its
  Hub started the job.

What they do not do:

* Nothing detects the failure or switches automatically. Operators decide that
  the server is lost and switch every site.
* The interrupted round is trained again from its starting model. The resumed run
  is not identical to an uninterrupted one: the aggregator of each round is
  drawn at random, and state that the training script keeps outside the model
  weights, such as optimiser state, starts afresh.
* Only PyTorch jobs can be resumed, because FLARE's PyTorch persistor loads the
  checkpoint.
* The resumed job chooses its best model only among its own rounds. FLARE's
  model selector keeps the global model with the best validation metric so far
  as `best_FL_global_model.pt`, but the resumed job does not know the metrics of
  the rounds before the interruption, so the first round it validates counts as
  a new best. The interrupted job's own `best_FL_global_model.pt` stays in that
  job's folder at the site that aggregated its best round, and that site's job
  log names the metric ("new best validation metric at round N"). To report the
  best model of the whole run, compare the two. In the rehearsal of 23 November
  2026, the resumed job took round 3 as its best (0.800), although round 2 had
  reached 0.813 before the loss.
* A Hub whose FL server was lost does not learn, when it comes back, that the
  sites have switched to the standby, and may still show the network and the
  interrupted job as running
  ([When the Hub that ran the server comes back](#when-the-hub-that-ran-the-server-comes-back)).

## Before a study: choose the standby site

The standby site needs:

* the network's whole startup package, imported into its Hub as at any
  participating site (a Hub that imported only its own site's kit cannot start
  the server);
* the settings of every participating site: client-only mode for its Celery
  worker, and the site's name as provisioned for both the `celery_worker` and the
  `app` service (`SWARMMEDHUB_CLIENT_ONLY_MODE=true`,
  `SWARMMEDHUB_LOCAL_PARTICIPANT=<site>`;
  [Networking](networking.md#what-the-platform-requires)). To resume a run, the
  standby needs neither `SWARMMEDHUB_LOCAL_ADDRESSES` nor connections between the
  Hubs on port 5085
  ([How a Hub knows which site it is](networking.md#how-a-hub-knows-which-site-it-is));
* for its Celery worker, the image settings of the coordinating server
  (`SWARMMEDHUB_FLARE_IMAGE`, `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE`) and its
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT`, so that the standby server runs the same
  FLARE build with the same time limits
  ([Resuming over a slow link](#resuming-over-a-slow-link));
* an IPv4 address that every other site can reach, and inbound connections on
  the fed-learn and admin ports (8002 and 8003 unless changed), as at the
  coordinating site (see [Networking](networking.md)). Host names are not
  accepted, because the address is passed to Docker's `--add-host`, which takes
  only an IP address;
* if training is to be resumed from its Hub: the study's project with the same
  training code. Create the project from the same code directory
  (`swarmed project create --code-dir ...`) and import the startup package into
  it.

Write the standby's address into the study's operating procedure, so that every
site has it when the coordinating host is down.

During the first run of the study, check at the standby that its Hub knows the
job: `swarmed training list --network <NETWORK_UUID>` lists it, with the FLARE
job ID that the coordinating Hub shows (`flare_job_uuid`) and the run's number of
rounds, and `swarmed training checkpoints <JOB_UUID> --network <NETWORK_UUID>`
lists a round-start checkpoint of it. Better still, rehearse the switch, the
resume and the return with a short run before the study
([Testing status](#testing-status)).

## Switching to the standby server

1. Make sure the coordinating server is down, and stays down, before any site
   switches. Two servers for one network must never run at the same time: sites
   connected to different servers would each wait for the others.
2. At the standby site:

    ```bash
    swarmed network failover <NETWORK_UUID> --server-host <STANDBY_ADDRESS> --start-server --wait
    ```

3. At every other site, and at the coordinating Hub if it is still reachable or
   once it comes back
   ([When the Hub that ran the server comes back](#when-the-hub-that-ran-the-server-comes-back)):

    ```bash
    swarmed network failover <NETWORK_UUID> --server-host <STANDBY_ADDRESS> --wait
    ```

At each Hub the command:

* records the switch in `prod_00/.active_server.json` of the network's
  workspace, with the address of the server the Hub used before. This record
  takes precedence over `SWARMMEDHUB_SERVER_HOST` and over the `server_host.txt`
  files in the kits, which keep the address the network was provisioned with;
* with `--start-server`, starts the FL server from the server kit in the site's
  package, writing any of the server's local settings the package lacks
  (packages exported before this version have none). If the server does not
  start, the Hub keeps the server it used before and the network shows `ERROR`.
  Without `--start-server`, stops an FL server container of the network at this
  Hub, if one is running;
* marks the running jobs that this Hub has recorded as failed;
* restarts the network's FL client containers at this Hub so that the name
  `server` resolves to the new address. With `--wait`, the command returns once
  the clients have restarted. A Hub without client containers only switches its
  own connection to the server and keeps the network's status, and `--wait`
  returns once the switch is recorded.

As with `swarmed network start`, the Hub's Celery worker carries out the
command, so client-only mode and the site's training variables (for example
`DATA_DIR`) must be set for the worker.

After the switch, the Hub's admin session, which submits and monitors the
network's jobs, connects only to the new address, with the kits' TLS server name
`server`. Without a switch it tries the name `server` first, which inside the
Hub's containers reaches an FL server container of the network at this Hub if
one runs, then the configured server address and local addresses. After a
switch such a local server would be a stale one, such as the coordinator's own
server started again or a server that a site started without client-only mode.
Only at the standby, which runs the new server itself, does the session still
try its own server first. Versions 1.0.3 and 1.0.4 tried the name `server`
before the new address, and local addresses after it, at every Hub: a server of
the network that still ran at a Hub, or ran again, received that Hub's jobs.
`swarmed network status` shows which server the Hub uses (`fl_server`) and the
addresses its admin session tries, in order (`fl_server.admin_session_hosts`).

### When the Hub that ran the server comes back

A Hub whose FL server stopped while the sites switched to the standby does not
learn of the switch. When it comes back, its FL server's container stays
stopped, because SwarmMed starts FL containers without a restart policy, and the
sites stay with the standby. The Hub may still show the network and the
interrupted job as running; `swarmed network status` shows the sites as
offline. Do not restart the server there. At that Hub, switch to the standby as
the other sites did:

```bash
swarmed network failover <NETWORK_UUID> --server-host <STANDBY_ADDRESS> --wait
```

This starts no FL server: it stops an FL server container of the network at this
Hub if one runs, marks the interrupted job as failed, points the Hub's admin
session at the standby, and restarts the Hub's FL client containers of the
network, if it has any, against the standby. Move the server back later, once the
current run has finished or been stopped, as described next.

While a Hub is switched to an FL server that another site started, `swarmed
network start` does not start the Hub's own FL server, and outside client-only
mode not its FL clients either: the network then shows `ERROR`. The network's
log says why, names the server the Hub uses, and points to
[Returning to the coordinating server](#returning-to-the-coordinating-server)
for running the server at this Hub again. With `--wait`, the command exits with
code 2; while the Hub is switched, its message also names that server, as a
possible cause, and points to this section. To start only the Hub's FL clients
while it is switched, set client-only mode for its Celery worker. The return
below starts the server with `swarmed network failover ... --start-server`.

At a Hub whose own FL clients still run, such as a coordinating Hub that also
trains, a refused start sets `ERROR` too, although the clients keep training.
The Hub then no longer discovers new jobs of the network and refuses `swarmed
training start` and `swarmed training resume`. Once the run has finished, not
during it, switch that Hub to the standby again with the command above: this
restarts the Hub's FL clients, which during the run would stop the run, and sets
the network to `RUNNING`.

### Returning to the coordinating server

To return to the coordinating server later, once the current run has finished
or been stopped:

1. At the standby site, switch to the coordinating server's address without
   `--start-server`. This stops the standby's server.
2. At the coordinating site, run the command with its own address and
   `--start-server`.
3. At every other site, run the command with the coordinating server's address,
   without `--start-server`.

Afterwards every Hub shows `switched` as `false` in `swarmed network status`,
because it uses the network's own server address again. The record stays in
`prod_00/.active_server.json`: it still names the server the Hub used before,
which `swarmed training resume` also asks, and it still takes precedence over
`SWARMMEDHUB_SERVER_HOST`. To use another address, switch again with `swarmed
network failover`.

### Moving the server's address instead (not tested)

If the coordinating institution can move the server's IPv4 address to another
of its machines, for example as a floating IP, the replacement server can be
started there, at the same address, from the network's startup package as at a
standby site. The other sites then need not switch, because their clients and
Hubs keep using that address. The interrupted run still has to be resumed, and
the old machine must not come back with the same address. This covers the loss
of the server's machine, not of the institution's network, and it has not been
tested.

## Resuming the run

At any training site, the standby included, whose Hub has the study's project
with the same training code
([Before a study](#before-a-study-choose-the-standby-site)):

```bash
swarmed training checkpoints <JOB_UUID> --network <NETWORK_UUID>
swarmed training resume <JOB_UUID> --network <NETWORK_UUID>
```

`<JOB_UUID>` is the interrupted job's FLARE job ID, which is the same at every
site: `flare_job_uuid` in the output of `swarmed training list`, and the name of
the job's folder in the site's workspace. The `identifier` of a Hub's own record
of the job differs from Hub to Hub. The first 8 or more characters of the FLARE
job ID are enough if no other job at the Hub, recorded or in the network's
workspace, starts with them; otherwise the command names the jobs that do
([CLI](cli.md#7-start-and-watch-training)).

`training checkpoints` lists the checkpoints of the job at this site, newest
round first. Round-start checkpoints exist at every training site, so any of
them can resume the run; resume it at one site only, agreed in advance. The Hub
needs no record of the job: if it has none, for example because another Hub
started the job, it finds the job by its folder in the network's workspace, and
records it when it resumes it. Without `<JOB_UUID>`, both commands take the job
of the newest round-start checkpoint at this site, or without one the Hub's
latest job, and stop if the Hub's latest job is another one. A submission that
failed, which records no FLARE job ID, does not count as the latest job.

A Hub that does not train, such as a coordinating Hub in server-only mode, has
no checkpoints: copy a site's `swarmmed_resume_checkpoint.pt` to it and pass
`--checkpoint FILE --network <NETWORK_UUID>`. The file names its job and round.
Without `--network` and without a job, the command takes the network that the
file names, as a checkpoint of a job submitted with SwarmMed 1.0.6 or later
does, else the one network at this Hub that holds the job's folders, and
otherwise asks for `--network`. For another model file, which names no job,
also give the job and `--start-round N`, where `N` is the round the file starts
(`next_round` in the `.json` next to a round-start checkpoint). Given without a
job, such a file is taken for the job that the command finds without
`<JOB_UUID>`, in the current network unless `--network` is given.

`training resume`:

* takes the newest checkpoint at this site, or the file given with
  `--checkpoint`, and refuses a checkpoint of another job or another network;
* refuses while the network's current server lists the interrupted job, or any
  other job, as active, and when it cannot ask that server, for example because
  the server does not answer. A job that is already running may be this run,
  resumed at another site: do not resume it twice;
* after a switch, also asks the server the Hub used before
  (`previous_server_host` in `.active_server.json`, or, for a record without
  it, the address in `SWARMMEDHUB_SERVER_HOST` or the kits' `server_host.txt`).
  A standby starts with an empty job store, so only that server can show that
  the job still runs there. If it does, the command refuses; if that server
  cannot be reached, the command resumes and warns that the server must stay
  down;
* gives the Hub's admin session 20 s (`SWARMMEDHUB_FLARE_CONNECT_TIMEOUT`) to
  log in to each server, as a job submission does, and each server as long
  again to answer. A server whose admin port does not accept a connection
  within 1 s gets 5 s to log in, or the setting if it is shorter, so a server
  that is down costs 5 to 6 s;
* skips these checks with `--force`;
* submits a job with FLARE's `start_round` set to the checkpoint's round, which
  runs the rounds left of the run's total. The total comes from the job: every
  site's copy of a job submitted with SwarmMed 1.0.6 or later holds it, and so
  do its round-start checkpoints. For a run started with an earlier version,
  pass `--rounds` with the number of rounds the run was started with, except at
  the Hub that ran the job's FL server, which finds the total in that server's
  job settings. The command stops rather than guess, and its message names
  `--rounds`. `--rounds` always sets the total, from 1 to 100000, with a warning
  when it differs from the total the job records; a total above 100000 in the
  job's files or checkpoints counts as unknown. The Hub's log, which the command
  prints, names the round the new job starts at, the total and where the total
  came from, for example `Job <JOB_UUID> resumed as <NEW_JOB_UUID> from round 3
  of 40 (total from --rounds).`;
* marks the interrupted job as failed, unless it has already ended:
  `swarmed network stop` marks a running job as stopped, and a switch marks it
  as failed. A Hub that had no record of the job records it as failed, once the
  checks have passed, and says so in a warning.

The checkpoint travels inside the job to the starting site. The new job records
what it started from in `config/swarmmed_resume.json` of the starting site's app,
and the Hub keeps a list in `workspaces/<project>/<network>/job/resumed_jobs.json`.
Every site's copy of the new job also describes the run in
`config/swarmmed_run.json`.

With NVIDIA FLARE 2.7.1, a race in FLARE's swarm controller can end a job after
some rounds: one site logs `already running a task: aborting it` in the job's
log and takes no further part, and the FL server aborts the job once that site
has not reported its status for 90 s (`FINISHED:EXECUTION_EXCEPTION`). The race
ends new jobs too ([Networking](networking.md#netbird-test-on-one-host)); in the
rehearsal of 24 November 2026 it ended two of the four resumed jobs
([Testing status](#testing-status)). If it ends a resumed job, wait until the FL
server has ended that job, because `training resume` refuses while the server
lists it as running, then resume the interrupted job again with the same
command, as in that rehearsal: the new job starts again from the interrupted
job's checkpoint and repeats the rounds that the ended job ran. Resuming the
ended job itself, from its own newer checkpoints, has not been tested.

### Resuming over a slow link

A resumed job carries the checkpoint (94 MB for the MST model of the worked
example), and two FLARE deadlines that a new run stays well within apply to
moving it. In a test across three hospitals, both expired over a slow link:

* **Deploying the job.** The FL server waits for each site to accept the job
  for at most its `admin_timeout`, which provisioning sets to 10 s. The starting
  site did not answer within 10 s, so the server started the job without it, and
  the job failed (`invalid value 'node-A' in 'participating_clients'` in the
  server's log). With 300 s the job was deployed. Set
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` (seconds) for the process that starts the FL
  server, then stop the network at that Hub and start it again the same way as
  before:

    * For a start from the CLI or the web interface, that process is the Hub's
      Celery worker. Put the variable in `docker-compose.override.yml` under
      `celery_worker` and apply it with `docker compose up -d celery_worker`. Do
      not put it in `.env`: `make env`, which every `make start` runs, drops
      variables that `.env.template` does not list
      ([Installation](installation.md#environment-variables)).
    * For a server started as in the
      [worked example](reproduce.md#4-start-the-fl-server-coordinating-host), add
      `-e SWARMMEDHUB_FLARE_ADMIN_TIMEOUT=300` to that command.

    The server also takes its image (`SWARMMEDHUB_FLARE_IMAGE`,
    `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE`) and start mode
    (`SWARMMEDHUB_SERVER_ONLY_MODE`) from that process. Without the image
    settings it was started with, it comes back in another image with that
    image's FLARE time limits (NVIDIA FLARE 2.7.1's 90 s, 3 600 s and 10 s in the
    image SwarmMed builds). Without `SWARMMEDHUB_SERVER_ONLY_MODE`, a client for
    every site is started at that host too
    ([Networking](networking.md#what-the-platform-requires)). In the test across
    three hospitals, the server had been started in the worked example's image
    and was restarted through the CLI. That put it in SwarmMed's own image, which
    is why the workflow's start (below) then had only 10 s.

    Before it starts the server, SwarmMed writes the value into the server's
    `local/resources.json`, which FLARE reads in place of
    `resources.json.default`, and keeps every other setting. The value stays in
    that file when the variable is removed; set the variable to 10 to return to
    the provisioned value. Set it at the standby site as well.

* **Starting the workflow.** The FL server then waits `start_task_timeout`
  (10 s in NVIDIA FLARE 2.7.1) for the starting site to start the workflow. The
  starting site confirms only once it has sent the first learn task to every
  other site, and in a resumed run that task carries the checkpoint: sending it
  to the two other sites (about 190 MB) took 110 s, where new runs confirmed in
  1 to 2.4 s. SwarmMed therefore gives a resumed job 600 s.
  `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` (whole seconds, in the process that
  submits the job) replaces that value, and also applies to new jobs. A long
  start also counts against FLARE's status-report limit (90 s in NVIDIA FLARE
  2.7.1), which runs from the start of the job: a site reports only once its
  first learn task has started, the starting site sends no report while it
  starts the workflow, and FLARE checks the reports 2 s after the workflow has
  started. In the test, the starting site's first report arrived 0.26 s before
  that check, so SwarmMed has FLARE check a resumed job's status reports every
  10 s instead of every 2 s, which leaves every site time to report first.

`swarmed training resume` names both settings after it submits the job, unless
`SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` is set in its environment. Stopping the network
at the Hub that runs the FL server ends the jobs on that server, so set the admin
timeout before you resume, or once the resumed job has failed. Then resume the
interrupted job again, not the failed one: a resumed job that fails before its
first round leaves no checkpoint.

## When a site drops out

Every site must return its result in every round, so when one site's training
process ends, for example because it was killed, or the site's FL client stops,
the other sites wait for it. Whether FLARE then ends the job depends on the time
limits of the FL server's FLARE build
([time limits](nvflare-comparison.md#time-limits-and-failure-behaviour)). With
NVIDIA FLARE 2.7.1's defaults the server aborts the job once the site has not
reported its status for 90 s. With longer limits, such as the 100 h of the
worked example's image, the job keeps running:
in a test across three hospitals, the Hub showed a job as running for five hours
after one site's training process had been killed.

The Hub's job monitor, which runs every minute in the Celery worker, one pass at
a time, therefore warns about a running job when

* **the FL server reports a site as dropped out**: its training job as missing
  (the site's FL client runs, but the job's process has ended) or its FL client
  as dead (no heartbeat for 10 minutes, FLARE's default), and the site has not
  taken part in the job since. A site whose job is missing is named only once
  the server's reports about it span a minute, as FLARE also waits a minute
  before it counts the site as disconnected: a job that is still starting at a
  site can be reported as missing once. The monitor reads this in the server's
  logs, so only the Hub that runs the FL server gives this warning;
* **no round has finished** for longer than `SWARMMEDHUB_STALL_WARNING_MINUTES`
  (default 60 minutes; `0` turns this check off). Every Hub checks this. A slow
  first round, counted from the start of the job, or a model whose rounds take
  longer than that, also triggers it; raise the value for such a study.

The warning appears under the progress bar on the **Training** page, as a
**Stalled** badge on the dashboard, on the job's page in the admin site, and in
`swarmed training status`, `list` and `watch`: a `warning:` line, and with
`--json` in `warnings` and as the job's `stall_warning`, next to `last_round_at`,
the time a round last finished or the job started running. The monitor also
writes it to the **Logs** page.
The warning does not change the job's status. A warning about a site disappears
when the site takes part in the job again, one about rounds when a round
finishes; both disappear when the job ends. The monitor reads only the end of
each log, so in a stall of more than a few hours it can also name a site that
FLARE counted as disconnected earlier in the job, although the site took part
again.

What to do:

1. Check the site that the warning names: whether its FL client container is
   running, and what the job's log at the site says.
2. Stop the job, with **Stop Training** on the Training page or
   `swarmed training stop <JOB_UUID>`. FLARE cannot take a site back into a
   running swarm job. The job is recorded as stopped only once the FL server
   has taken the abort, or has answered that the job had already finished;
   otherwise both show an error and the job's record stays as it was
   ([CLI](cli.md#7-start-and-watch-training)).
3. Once the site can train again, resume the run from the newest checkpoint as
   described in [Resuming the run](#resuming-the-run). Only PyTorch jobs can be
   resumed; start other jobs again. The resumed job's progress counts the rounds
   of the whole run, so it shows 0 % until the job's first round has finished,
   and its ETA is too short: the rounds of the interrupted job count as done in
   the resumed job's time.

## Settings

| Variable | Process | Effect |
|---|---|---|
| `SWARMMEDHUB_RESUME_CHECKPOINTS` | the process that submits the job | `false` stops writing round-start checkpoints; `FL_global_model.pt` is still saved by the aggregating site |
| `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` | the process that starts the FL server: for a start from the CLI or the web interface, the Hub's Celery worker (`docker-compose.override.yml` under `celery_worker`, not `.env`); applies from the next server start | seconds the FL server waits for each site's reply, for example when it deploys a job (provisioned: 10 s); see [Resuming over a slow link](#resuming-over-a-slow-link) |
| `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` | the process that submits the job (`docker exec -e` for the CLI, `docker-compose.override.yml` under `app` for the web interface) | whole seconds the FL server waits for the starting site to start the workflow; without it, 600 s for a resumed job and the FLARE build's default for a new one (10 s in NVIDIA FLARE 2.7.1) |
| `SWARMMEDHUB_FLARE_CONNECT_TIMEOUT` | the process that submits the job | seconds the Hub's admin session gets to log in to an FL server when it submits a job and when `swarmed training resume` asks a server whether it still runs the job; in the latter case also the seconds the server gets to answer; default 20. An address whose admin port does not accept a connection within 1 s gets at most 5 s |
| `SWARMMEDHUB_STALL_WARNING_MINUTES` | the Celery worker (set it in `.env`) | minutes without a finished round after which a running job is flagged as stalled; default `60`, `0` turns the check off, an invalid value means `60` and a warning in the log ([When a site drops out](#when-a-site-drops-out)) |

## Testing status

The unit tests cover the switch, the server kit, the job changes, the choice of
checkpoint, the addresses the Hub's admin session uses before and after a switch,
and the checks before a resume, with Docker and the FL servers replaced by
stand-ins. Since 1.0.6 they also cover a resume at a Hub that has no record of
the job, found by its folder or by a copied checkpoint; the run's total in every
job and checkpoint; the refusal while the server runs another job; a site's Hub
that identifies itself only with `SWARMMEDHUB_LOCAL_PARTICIPANT` and records the
job its FL client runs; starting the network, switching servers and starting
the standby with a Celery worker that runs as an unprivileged user, after an
import with the CLI as root; the refusal of `swarmed network start` while the
Hub is switched to a server that another site started; which job the start of a
FLARE job ID names; and that `swarmed training stop` changes no record when the
FL server refuses the abort or does not answer. A test in FLARE's simulator
(`SWARMMEDHUB_RUN_FLARE_SIMULATOR=1`, in `apps/training/test_resume_simulator.py`)
runs a four-round job, then resumes it at round 3 for a six-round run, and checks
that every site starts round 3 from the same weights as in the first run, and,
since 1.0.6, that every site's round-start checkpoints of the resumed job name
the run's six rounds. With version 1.0.6 it passed on 24 November 2026 with
NVIDIA FLARE 2.7.1 in a Python environment on the host. Its earlier form,
without the check of the run's total, also passed inside the image of the
worked example (`SWARMMEDHUB_SIMULATOR_IMAGE`); the extended form has not been
run there.

In the test across three hospitals described in
[Resuming over a slow link](#resuming-over-a-slow-link), the FL server was
restarted at its host, and the sites' clients reconnected to it without any
action at the sites.

On 23 November 2026 the failover was rehearsed once, with SwarmMed 1.0.5 and
NVIDIA FLARE 2.7.1, on two lab machines in one institution, not at hospitals.
Three Hubs ran in privileged containers, each with its own Docker Engine: the
coordinating Hub, which ran only the FL server, and two training sites with one
PTB-XL share of the
[ECG template](https://github.com/KatherLab/SwarmMed/blob/main/examples/ecg_ptbxl/README.md)
each, on CPU. One site, the agreed standby, ran on the second machine, the other
on the same machine as the coordinating Hub. The sites reached the FL server, and
later the standby, at the machines' own addresses, without an overlay or VPN,
and traffic between the Hubs on port 5085 was blocked.

During round 3 of a 40-round run of a model with 79,749 parameters, the
coordinating Hub's container was killed. The standby started the server with
`swarmed network failover ... --start-server`, and the other site switched to it
with `swarmed network failover`. Both sites were registered with the standby's
server 3 min 11 s after the kill; 2 min 30 s of that passed before the first
command, while the operator checked that the coordinating server was down. The
run was resumed with `swarmed training resume` at the other site, not at the
standby (see below). The command asked the current server, the standby's, first,
and warned that the previous server could not be reached and must stay down.
Training restarted at round 3, 5 min 12 s after the kill, from weights identical
to those that round had started from before the loss. Round 3, which had run for
less than a second, was repeated, and no completed round was lost. The run
finished with the same final global model, byte for byte, at both sites. It was
not compared with an uninterrupted run.

When the coordinating Hub was started again, its FL server stayed down, and the
switched sites stayed with the standby. The coordinating Hub still showed the
network and the job as running and the sites as joined. The three commands under
[Returning to the coordinating server](#returning-to-the-coordinating-server)
then moved the server back in 1 min 17 s. For about 31 s no FL server accepted
connections, and two servers never ran at the same time. A 5-round run then
completed. All three Hubs showed the same network fingerprint, and two altered
copies of the startup package were refused at import, with nothing written.

With version 1.0.5 the rehearsal needed three things that a default
installation does not have or that this guide did not ask for:

* Every Hub's Celery worker ran as root, through a change to the worker's
  entrypoint made for the rehearsal. With the default worker, the first FL start
  at every Hub failed with a permission error. Version 1.0.6 removes the causes.
* The standby could not resume the run. Its Hub, set up with
  `SWARMMEDHUB_LOCAL_PARTICIPANT` and without `SWARMMEDHUB_LOCAL_ADDRESSES`, held
  no record of the job, which the coordinating Hub had submitted, and every form
  of `swarmed training resume` refused. The run was resumed at the other site,
  whose Hub had a record only because `SWARMMEDHUB_LOCAL_ADDRESSES` had been set
  there before training. Version 1.0.6 resumes at any training site, with or
  without a record.
* The resume needed `--rounds 40`: the resuming site's record gave 10 rounds, a
  number its Hub had guessed. Version 1.0.6 carries the run's total in every job
  and checkpoint, and never guesses it.

The rehearsal does not show:

* the failover across hospitals or institutions, through a VPN, NAT, firewalls
  or slow links;
* a default installation, or a standby that resumes the run;
* how long detecting the failure and agreeing on the switch take: the times
  above assume an operator ready at every Hub;
* other ways to lose the server: a reboot, a power loss or a network partition
  (the sites' connections were refused at once, without timeouts), a loss during
  aggregation or while a job is deployed, a second failover, a switch while the
  coordinating server can still be reached, or a coordinating server that runs
  again at its address while the standby serves;
* restarting the server in place, moving the server's address, or a site that
  drops out during the failover;
* larger models or GPUs: the checkpoint was 330 KB, not the 94 MB of the worked
  example's model;
* that the resumed run matches an uninterrupted one. The choice of the best model
  also started again at round 3 ([What they do not do](#what-swarmmed-provides));
* the web interface, or that it can be repeated: there was one run, one moment
  of loss and one standby.

<!-- r106-fill: the 1.0.6 re-run paragraph, from its evidence only and checked by a second reader against cmd/ and logs/: the date; where (same layout: two lab machines in one institution, nested Hubs, CPU, the ECG template, one standby on the second machine); that it was a default installation (every Celery worker as the image's unprivileged user, the standby and the other site identified only by SWARMMEDHUB_LOCAL_PARTICIPANT, no SWARMMEDHUB_LOCAL_ADDRESSES, port 5085 blocked between the Hubs, the package imported with the CLI as root); the round and the way the server was lost; that the standby started the server and resumed the run with swarmed training resume <JOB_UUID> --network <NETWORK_UUID> without --rounds; the time from the loss to both registrations and to the restart of training; how many rounds were repeated and lost; whether the final global models were identical; that a second resume at the other site was refused; what the returning coordinator showed and that network start there was refused after its switch; the time of the return and that two servers never ran; the restart in place with the resume at the other site without --rounds; that training stop returned; anything that needed a workaround (or "Nothing needed a workaround"); what the re-run still did not show (update the list above for the re-run), and whether the optional docker-proxy probe ran -->

Moving the server's address to another machine has not been tested. Rehearse a
failover with your own sites, and with a short run, before relying on it in a
study.
