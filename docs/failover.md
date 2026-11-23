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
  swarm job. A restarted server marks the job as abandoned and tells the sites to
  abort it.
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

1. At the coordinating Hub, stop the network and start it again the same way as
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
2. The other sites need to do nothing. Their FL clients keep running and
   reconnect to the restarted server by themselves: the server takes them back
   on the tokens it gave them before, which it signed with its private key, so
   they do not register again. In a test across three hospitals, the FL server
   was restarted at its host while the sites' clients kept running, and all three
   reconnected without any action at the sites.
3. Resume the interrupted run ([Resuming the run](#resuming-the-run)). The
   restarted server has marked the job as abandoned and told the sites to abort
   it.

If the server runs at a standby site after a switch, restart it there with the
command that started it. As at the coordinating Hub, the site's Celery worker
must have the image settings that the server was started with.

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
  `swarmmed_resume_checkpoint.json`. The first round of a new run starts from an
  empty model and writes none. The file is as large as the model (94 MB for
  the MST model of the worked example). FLARE itself saves the global model
  (`FL_global_model.pt`) only at the site that aggregated the round.
* **Resume.** `swarmed training resume` submits a new job that starts at the
  interrupted round from the newest checkpoint at the site and runs the rounds
  that are left. The round numbers continue those of the interrupted run.

What they do not do:

* Nothing detects the failure or switches automatically. Operators decide that
  the server is lost and switch every site.
* The interrupted round is trained again from its starting model. The resumed run
  is not identical to an uninterrupted one: the aggregator of each round is
  drawn at random, and state that the training script keeps outside the model
  weights, such as optimiser state, starts afresh.
* Only PyTorch jobs can be resumed, because FLARE's PyTorch persistor loads the
  checkpoint.

## Before a study: choose the standby site

The standby site needs:

* the network's startup package, imported into its Hub as at any participating
  site;
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

## Switching to the standby server

1. Make sure the coordinating server is down, and stays down, before any site
   switches. Two servers for one network must never run at the same time: sites
   connected to different servers would each wait for the others.
2. At the standby site:

    ```bash
    swarmed network failover <NETWORK_UUID> --server-host <STANDBY_ADDRESS> --start-server --wait
    ```

3. At every other site, and at the coordinating Hub if it is still reachable:

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
* marks the network's running jobs as failed;
* restarts the network's FL client containers at this Hub so that the name
  `server` resolves to the new address. With `--wait`, the command returns once
  the clients have restarted. A Hub without client containers only switches its
  own connection to the server and keeps the network's status.

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

To return to the coordinating server later, once the current run has finished
or been stopped:

1. At the standby site, switch to the coordinating server's address without
   `--start-server`. This stops the standby's server.
2. At the coordinating site, run the command with its own address and
   `--start-server`.
3. At every other site, run the command with the coordinating server's address,
   without `--start-server`.

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

At a Hub that has the study's project and the newest checkpoint:

```bash
swarmed training checkpoints <JOB_UUID>
swarmed training resume <JOB_UUID>
```

`training checkpoints` lists the checkpoints of the job at this site, newest
round first. Round-start checkpoints exist at every training site, so any of
them can resume the run. A Hub that does not train, such as a coordinating Hub in
server-only mode, has none: copy a site's checkpoint file to it and pass it with
`--checkpoint FILE --start-round N`, where `N` is the round the file starts
(`next_round` in the `.json` next to a round-start checkpoint).

`training resume`:

* takes the newest checkpoint at this site, or the file given with
  `--checkpoint`;
* refuses while the network's current server still lists the interrupted job as
  active, and when it cannot ask that server, for example because the server
  does not answer;
* after a switch, also asks the server the Hub used before
  (`previous_server_host` in `.active_server.json`, or, for a record without
  it, the address in `SWARMMEDHUB_SERVER_HOST` or the kits' `server_host.txt`).
  A standby starts with an empty job store, so only that server can show that
  the job still runs there. If it does, the command refuses; if that server
  cannot be reached, the command resumes and warns that the server must stay
  down;
* gives the Hub's admin session 20 s (`SWARMMEDHUB_FLARE_CONNECT_TIMEOUT`) to
  log in to each server, as a job submission does, and each server as long
  again to answer;
* skips these checks with `--force`;
* submits a job with FLARE's `start_round` set to the checkpoint's round and runs
  the rounds left of the interrupted job's total (`--rounds` sets the total);
* marks the interrupted job as failed.

The checkpoint travels inside the job to the starting site. The new job records
what it started from in `config/swarmmed_resume.json` of the starting site's app,
and the Hub keeps a list in `workspaces/<project>/<network>/job/resumed_jobs.json`.

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

The Hub's job monitor, which runs every minute in the Celery worker, therefore
warns about a running job when

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
   running swarm job.
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
| `SWARMMEDHUB_FLARE_CONNECT_TIMEOUT` | the process that submits the job | seconds the Hub's admin session gets to log in to an FL server when it submits a job and when `swarmed training resume` asks a server whether it still runs the job; in the latter case also the seconds the server gets to answer; default 20 |
| `SWARMMEDHUB_STALL_WARNING_MINUTES` | the Celery worker (set it in `.env`) | minutes without a finished round after which a running job is flagged as stalled; default `60`, `0` turns the check off, an invalid value means `60` and a warning in the log ([When a site drops out](#when-a-site-drops-out)) |

## Testing status

The unit tests cover the switch, the server kit, the job changes, the choice of
checkpoint, the addresses the Hub's admin session uses before and after a switch,
and the checks before a resume, with Docker and the FL servers replaced by
stand-ins. A test in FLARE's simulator
(`SWARMMEDHUB_RUN_FLARE_SIMULATOR=1`, in `apps/training/test_resume_simulator.py`)
runs a four-round job, then resumes it at round 3 for a six-round run, and checks
that every site starts round 3 from the same weights as in the first run. It
passed with NVIDIA FLARE 2.7.1 and inside the image of the worked example
(`SWARMMEDHUB_SIMULATOR_IMAGE`).

In the test across three hospitals described in
[Resuming over a slow link](#resuming-over-a-slow-link), the FL server was
restarted at its host, and the sites' clients reconnected to it without any
action at the sites.

A failover across hosts, with the server stopped during a run, the standby
started at another host, the sites switched and the run resumed, has not yet
been carried out, and neither has moving the server's address to another
machine. Rehearse them before relying on them in a study.
