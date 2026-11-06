---
title: Server Failover
description: What happens when the FL server is lost, how a standby site takes over the server, and how an interrupted run is resumed.
---

# Server failover and resuming a run

A SwarmMed network has one NVIDIA FLARE server. In a swarm run the server does
not aggregate, but it starts and monitors each job and relays every message
between the sites, model updates included (see
[Background](background.md)). This page describes what happens when that
server is lost and the two tools SwarmMed provides for it: a standby server,
started by hand at a site agreed in advance, and resuming the interrupted run
from a checkpoint. Together they make a run recoverable after the server is
lost. They do not remove the server as a single point of failure: there is
always exactly one server, and switching to another one interrupts the run and
needs an operator at every site.

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
  run can be resumed.

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
  workspace. This record takes precedence over `SWARMMEDHUB_SERVER_HOST` and over
  the `server_host.txt` files in the kits, which keep the address the network was
  provisioned with. The Hub's job submission and status polling use the new
  address as well;
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
`DATA_DIR`) must be set for the worker. `swarmed network status` shows which
server the Hub uses (`fl_server`).

To return to the coordinating server later, once the current run has finished
or been stopped:

1. At the standby site, switch to the coordinating server's address without
   `--start-server`. This stops the standby's server.
2. At the coordinating site, run the command with its own address and
   `--start-server`.
3. At every other site, run the command with the coordinating server's address,
   without `--start-server`.

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
  running (override with `--force`);
* submits a job with FLARE's `start_round` set to the checkpoint's round and runs
  the rounds left of the interrupted job's total (`--rounds` sets the total);
* marks the interrupted job as failed.

The checkpoint travels inside the job to the starting site. The new job records
what it started from in `config/swarmmed_resume.json` of the starting site's app,
and the Hub keeps a list in `workspaces/<project>/<network>/job/resumed_jobs.json`.

## Settings

| Variable | Process | Effect |
|---|---|---|
| `SWARMMEDHUB_RESUME_CHECKPOINTS` | the process that submits the job | `false` stops writing round-start checkpoints; `FL_global_model.pt` is still saved by the aggregating site |

## Testing status

The unit tests cover the switch, the server kit, the job changes and the choice of
checkpoint. A test in FLARE's simulator
(`SWARMMEDHUB_RUN_FLARE_SIMULATOR=1`, in `apps/training/test_resume_simulator.py`)
runs a four-round job, then resumes it at round 3 for a six-round run, and checks
that every site starts round 3 from the same weights as in the first run. It
passed with NVIDIA FLARE 2.7.1 and inside the image of the worked example
(`SWARMMEDHUB_SIMULATOR_IMAGE`).

A failover across hosts, with the server stopped during a run, the standby
started at another host, the sites switched and the run resumed, has not yet
been carried out. Rehearse it before relying on it in a study.
