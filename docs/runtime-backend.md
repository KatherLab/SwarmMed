---
title: Runtime backend and portability
description: Where SwarmMed depends on NVIDIA FLARE, what does not, and what moving to another runtime such as Flower would involve.
---

# Runtime backend and portability

SwarmMed is built on NVIDIA FLARE (NVFlare) and has no runtime-abstraction layer.
There is no interface, abstract base class or plug-in point behind which another
runtime could be added, and the name `DecentralizedRuntimeEngine` does not occur
in the code or in its history. Code that calls NVFlare's tools and APIs, or that
depends on its file layout (startup kits, the `prod_00` directory, job workspaces)
and its log messages, sits in 16 modules of the network, training, results, home
and logs apps, and NVFlare's job identifiers are stored in the data model.

This page maps those dependencies so that the cost of moving to another runtime
can be judged from the code. No port has been attempted: the sizes below are line
counts of the code that would change, not a measured effort. Line numbers refer to
commit `f3a96fae`. What FLARE provides, and what SwarmMed adds on top of it, is
described in [SwarmMed and NVIDIA FLARE](nvflare-comparison.md).

## Where SwarmMed depends on NVFlare

| Layer | Code | What is NVFlare-specific |
|---|---|---|
| **Provisioning and PKI** | `apps/network/provision.py` (`generate_flare_startup_kit`, 70–443); `apps/network/master_template.yml` | Writes an NVFlare `project.yml` (a server and, for each site, a client and an admin identity with the `project_admin` role) and runs `nvflare provision` with NVFlare's workspace, Docker, static-file, certificate and signature builders. Expects the kits in `workspace/<project>/prod_00/<participant>/startup` and adds SwarmMed's own files to them (`.all_participants.json`, `server_host.txt`, `server_aliases.txt`). Rewrites site names to satisfy NVFlare's naming rule (no underscores; 58–67). The template is a modified copy of an NVFlare Lighter template, 2,022 lines. |
| **Startup packages** | `apps/network/utils.py` (`create_startup_kits_zip`, 208–332); `apps/network/services.py` (`import_network`, 254–658) | Export packs each site's kit together with an admin kit and a copy of the server kit (`admin_startup/`, `server_startup/`). Import rebuilds the `prod_00` layout, renames admin kits to `admin-<site>@nvidia.com`, and recovers the server address from `server_host.txt`, `project.yml` or the `overseer_agent` endpoint in `fed_client.json`. |
| **Container start-up** | `apps/network/tasks.py` (kit discovery, 237–286, 323–397, 446–536; `start_swarm_network_task`, 1376–1990; `run_nvflare_preflight_check`, 1993–2082) | Finds server and client kits by their `fed_server.json` or `fed_client.json`; starts each container with the kit's `sub_start.sh`, which runs NVFlare's `server_train` or `client_train` module; removes NVFlare's `pid.fl` markers; publishes the two server ports read from `fed_server.json`; mounts NVFlare's state directory `/tmp/nvflare`; maps the host name `server` to the coordinating host; sets `NVFLARE_START_METHOD=spawn`; runs `nvflare.tool.preflight_check` with an admin kit. |
| **Job construction** | `apps/training/services.py` (`submit_training_job`, 490–991) | Lays out an NVFlare job (`app_server`, `app_client/custom`) and builds it with NVFlare's Job API (`FedJob`): `SwarmServerController` and `SwarmClientController` (the client-controlled swarm workflow; every site trains and is eligible to aggregate, and every site's result is required), `InTimeAccumulateWeightedAggregator`, `PTFileModelPersistor` or `NPModelPersistor` chosen from the training script's imports, `SimpleIntimeModelSelector`, and a Client API executor that runs `custom/training.py`. |
| **Training-script contract** | `apps/training/flare_adapter.py` (16–17, 596–660, 713–725); `examples/*/training.py`; `experiments/duke_mst_cli/training.py` | The adapter's `init`, `receive`, `send` and `is_running` wrap `nvflare.client`, and `send` packs the weights into an `FLModel`. The eight example scripts use the adapter; the breast-MRI example also hands its Lightning trainer to NVFlare's `lightning.patch`. The worked example imports `nvflare.client.lightning` directly and does the same (lines 15–16, 113). |
| **Job control** | `apps/training/runtime.py` (57–742); `apps/training/services.py` (`sync_job_from_nvflare_status`, 252–290; `stop_training_job`, 994–1041) | Opens NVFlare admin sessions (`Session` from `nvflare.fuel.flare_api`) with an admin kit, taking the user name from the kit certificate's common name, probing candidate hosts and ports, and patching gRPC to override the TLS server name. Sends `submit_job`, `list_jobs`, `list_clients` and `abort_job`, and parses the replies. |
| **Monitoring** | `apps/training/tasks.py` (`monitor_training_jobs`, 100–394); `apps/training/utils.py` (18–58, 119–208, 228–328); `apps/training/runtime.py` (759–1019); `apps/network/runtime.py` (19–262); `apps/home/views.py` (143–256); `apps/logs/views.py` (134–228) | Round progress and final state come from matching NVFlare log messages (for example `action=finished_learn_task`, `ending workflow`, `received failure report from client`) in the job's `log*.txt` files and in `docker logs`; the number of rounds is read from the generated `config_fed_server.json`. Whether a site has joined is decided from the FL server's connection, registration and heartbeat messages. The logs page finds containers through the `compose.yaml` written by NVFlare's Docker builder. |
| **Result collection** | `apps/results/tasks.py` (45–176); `apps/training/services.py` (359–487); `apps/training/tasks.py` (321–389); `apps/results/artifacts.py` (8–15) | Looks for artefacts in NVFlare's job workspace (for example `prod_00/<participant>/<job id>/app_<participant>/`), recognises result folders by NVFlare's file names (`log_fl.txt`, `fl_app.txt`, `meta.json`, …) and the global model by `FL_global_model.pt`, and uploads them to `<project>/results/<job id>/<participant>/` in the object store. |
| **Job identity** | `apps/training/models.py` (62–99); `apps/results/models.py` (109–111); `apps/network/models.py` (75–80, 239–243); `apps/training/utils.py` (`extract_flare_job_uuid`, 63–86) | Jobs are identified by NVFlare's job ID and UUID (`flare_job_id` and `flare_job_uuid`; the UUID is unique per network); a network records the path of its admin kit; participant IDs are FLARE participant names. The two job fields appear on 160 lines in 12 modules, the UUID parser is called 21 times in 7 modules, and result keys in the object store contain the job UUID (`<project>/results/<job UUID>/…`). |
| **Web interface and CLI** | `apps/network/views.py` (476–486, 528–556); `templates/apps/network/new_network.html` (67–117); `templates/apps/network/network.html` (287); `apps/training/views.py` (157–201, 588–964); `swarmed_cli/main.py` (210–219, 537–551) | Users create, import and export "startup packages" (`swarmed network export-package` on the command line). The training page and its status and log endpoints show NVFlare job IDs and log lines, and merge the monitoring sources above. |

## What does not depend on the runtime

These parts do not touch NVFlare's APIs, files or logs (line counts exclude tests
and migrations):

* **Projects, membership and roles**: `apps/project` (1,876 lines).
* **Users and two-factor authentication**: `apps/users` (1,914 lines).
* **Audit and application logs**: `apps/logs` (1,751 lines), apart from the live
  training-log panel listed above.
* **Data import, object storage and data delivery**: `apps/data` (3,325 lines),
  including the manifest endpoint that training containers call
  (`apps/data/views.py`, 59–118). Its client, `FlareDataFileSystem` in the adapter
  (`apps/training/flare_adapter.py`, 103–588), uses only HTTP and environment
  variables despite its name. Mounting a site's data directory read-only into the
  client container is a Docker option (`apps/network/tasks.py`, 1823–1828).
* **Sandboxed validation and visualisation**: `apps/data/sandbox.py` and the
  results visualisation task (`apps/results/tasks.py`, 262–538).
* **Results browsing once artefacts are stored**: `apps/results/services.py`,
  `views.py` and `visualization.py` work on object-store keys of the form
  `<project>/results/<job id>/<participant>/<file>` (`apps/results/artifacts.py`,
  52–82), whatever wrote them. They depend on NVFlare only through the
  job-identity helpers and the global-model file names listed above.
* **Messaging and backups**: `apps/communication` (940 lines) and `apps/backup`
  (935 lines).
* **Container handling**: about 1,300 of the 2,224 lines in
  `apps/network/tasks.py`: Docker networks and subnets, image builds, port checks,
  container status, stop and clean-up.
* **Status exchange between Hubs**: the authenticated HTTPS messages between sites
  (`apps/network/views.py`, 120–292; `apps/network/tasks.py`, 1274–1305;
  `apps/training/views.py`, 123–154 and 771–843) are SwarmMed's own; only the
  status values they carry come from NVFlare.
* **Command line**: `swarmed_cli` (1,058 lines) calls the same services as the web
  interface and inherits their dependencies; apart from `network export-package`
  it contains nothing FLARE-specific.

In all, about 5,200 of the 24,502 lines of Python under `apps/` (tests and
migrations excluded) depend on NVFlare.

## What a port would involve

The example here is [Flower](https://flower.ai/). Statements about Flower refer to
its documentation for version 1.39, listed under [Sources](#sources).

### Three changes that are design decisions

1. **The swarm workflow has no Flower counterpart.** Flower's documentation
   describes a hub-and-spoke architecture (one server, many clients): SuperNodes
   (the sites) communicate with the SuperLink, and the strategy, including
   aggregation, runs in the ServerApp. SwarmMed's job uses NVFlare's
   client-controlled workflow, in which the sites aggregate and the aggregating
   site is drawn at random for each round (`apps/training/services.py`,
   904–947). A port would either aggregate in a ServerApp at the coordinating
   site, which changes what SwarmMed can claim (see
   [What "fully decentralised" means here](nvflare-comparison.md#what-fully-decentralised-means-here)),
   or build client-side aggregation itself on top of Flower's messages, with every
   update still passing through the SuperLink. Neither has been tried.

2. **Credentials and deployment work differently.** NVFlare's provisioning gives
   each participant an X.509 certificate signed by a project CA, packed into a
   startup kit with its configuration and start scripts, and jobs are submitted
   through admin identities issued at the same time. A self-hosted Flower
   deployment is a long-running SuperLink at the coordinating site and a SuperNode
   at each site (`flower-supernode --superlink <address>`, with site settings in
   `--node-config`). TLS is set with command-line options: the SuperLink gets a CA
   certificate and its own certificate and key (`--ssl-ca-certfile`,
   `--ssl-certfile`, `--ssl-keyfile`), SuperNodes get the CA certificate
   (`--root-certificates`), and the TLS is server-authenticated only. SuperNodes
   authenticate with their own elliptic-curve key pairs, whose public keys are
   registered with the SuperLink (`flwr supernode register`, with the SuperLink
   started with `--enable-supernode-auth`). Runs are submitted, listed, stopped
   and followed with `flwr run`, `flwr ls`, `flwr stop` and `flwr log`. There are
   no startup kits to generate, package or import, so the startup-package
   workflow would be redesigned rather than translated.

3. **Training scripts change shape.** NVFlare's Client API lets a script run its
   own loop (`while flare.is_running(): flare.receive() … flare.send()`), and
   NVFlare's Lightning `patch` adds a callback that receives the global weights
   when training or validation starts and sends the trained weights when training
   ends (for an evaluation task, the metrics when validation ends). A Flower ClientApp
   is a function that Flower calls once per message (`@app.train()`, taking a
   `Message` and a `Context` and returning a `Message` that carries the weights
   as an `ArrayRecord` and metrics as a `MetricRecord`); site settings come from
   `context.node_config`. Scripts written as loops, including the worked
   example's, would have to be restructured or run behind a shim that maps the
   loop onto per-message calls; SwarmMed has no such shim. The two scripts that
   use the Lightning `patch` would need another way to exchange weights.

### Code to replace

| Part | Code affected today | In a Flower port |
|---|---|---|
| Provisioning and startup packages | 973 lines (`provision.py` 443, `create_startup_kits_zip` 125, `import_network` 405), plus the 2,022-line template | Create a CA and the SuperLink's certificate; generate or collect each site's SuperNode key pair and register the public keys; replace the package with one that carries the CA certificate and the SuperLink address |
| Container start-up | about 920 of the 2,224 lines of `network/tasks.py` | Run `flower-superlink` and `flower-supernode` in place of the kits' start scripts; replace kit discovery, port handling and the preflight check; keep the Docker handling |
| Job construction | 502 lines | A Flower app (a ServerApp with a strategy, and a ClientApp around the training script), packaged and submitted with `flwr run` |
| Training-script adapter and scripts | 80 adapter lines; nine scripts (1,482 lines) | A new wrapper around the ClientApp interface; each script's training loop restructured |
| Job control | 773 lines | `flwr ls`, `flwr stop` and `flwr log`; the admin-kit sessions, host probing and gRPC patch are dropped |
| Monitoring and site status | 1,241 lines in six modules | A new status source (run status from the SuperLink, metrics reported by the app); every log pattern rewritten |
| Result collection | 269 lines | Collect the model from wherever the Flower app saves it (in Flower's examples, the ServerApp saves it); everything downstream of the object store is unchanged |
| Job identity | 160 lines in 12 modules, and a schema migration | Flower run IDs are integers, not UUIDs: the two fields, the UUID parser and the result-key prefix change |
| Web interface and CLI | about 400 lines | Rename the startup-package screens and command; the status views follow the monitoring rewrite |

Together that is about 5,200 lines in 16 modules and the CLI, plus the
job-identity fields, the template and the training scripts. A further 311 lines in
`apps/training/views.py` (within 204–578) are copies of, or wrappers around,
helpers in `apps/training/runtime.py` that nothing in the application calls; a
port would delete them. A later NVFlare release that changes any of these APIs,
file layouts or log messages would affect the same places.

## NVFlare version

The Hub, meaning the Django and Celery processes that provision networks, build
jobs and use the admin API, is pinned to `nvflare==2.7.1` (`pyproject.toml`, line
70; `uv.lock`). The FL server and clients run in runtime containers and use
whichever FLARE build the runtime image contains. A Hub starts all of its server
and client containers from one image (`apps/network/tasks.py`, 1424–1473, 1923):

* By default the image is built for each network from `python:3.12-slim` and the
  project's runtime requirements, which always include `nvflare==2.7.1`
  (`apps/network/tasks.py`, 810–864; `apps/common/utils.py`, 56–67). The code
  first looks for a Dockerfile in `prod_00/nvflare/` (1451–1463), but NVFlare
  2.7.1 writes its Dockerfile to `prod_00/nvflare_compose/`, so the build from
  `python:3.12-slim` is the one that runs.
* `SWARMMEDHUB_FLARE_IMAGE` names an image to use instead. It takes effect only
  together with `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true`: the check for
  project-specific requirements (929–968) counts packages from SwarmMed's own
  baseline, such as `torch`, as project requirements, so without the second
  variable the image is always rebuilt (1431–1443). SwarmMed does not check which
  FLARE build the configured image contains.

The worked example's instructions set both variables
(`experiments/duke_mst_cli/README.md`, 18–19), and its runs used
`jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6`,
tagged locally as `odelia:paper-apr2026`. The run records name it as the training
image of all three repetitions (for the first two, identified afterwards from
build dates) and of the scrambled-label control
(`results/duke_mst_cli/stat_tests/swarm_run_provenance.json`,
`results/duke_mst_cli/scrambled_control/PROVENANCE.json`). The image contains no
`nvflare` distribution:

```console
$ docker run --rm --entrypoint sh odelia:paper-apr2026 -c \
    'pip show nvflare nvflare-mediswarm 2>/dev/null; pip list 2>/dev/null | grep -i flare'
Name: nvflare_mediswarm
Version: 2.7.2.dev260427
...
Location: /opt/conda/lib/python3.10/site-packages
...
nvflare_mediswarm             2.7.2.dev260427
```

Its FLARE is MediSwarm's fork, distributed as `nvflare_mediswarm`
2.7.2.dev260427 and installed from a local source checkout into Python 3.10.14.
The import name is still `nvflare`, so the kits' start scripts and the training
code load the fork; `nvflare.__version__` reports `0+unknown`, and the image's
environment records `NVF_BRANCH=2.7.2`. In the worked example, therefore, the
startup kits and the job definition were produced by FLARE 2.7.1 on the Hub and
executed by the 2.7.2 development fork in the training containers. How the fork
differs from 2.7.1 is described under
[SwarmMed and NVFlare](nvflare-comparison.md).

## Sources

Flower documentation, version 1.39, consulted on 8 October 2026 and checked again
on 9 October 2026:

* [Flower architecture](https://flower.ai/docs/framework/explanation-flower-architecture.html)
* [Run Flower with the Deployment Runtime](https://flower.ai/docs/framework/how-to-run-flower-with-deployment-engine.html)
* [Enable TLS connections](https://flower.ai/docs/framework/how-to-enable-tls-connections.html)
* [Authenticate SuperNodes](https://flower.ai/docs/framework/how-to-authenticate-supernodes.html)
* [Flower CLI reference](https://flower.ai/docs/framework/ref-api-cli.html)
* [Quickstart PyTorch](https://flower.ai/docs/framework/tutorial-quickstart-pytorch.html)
* [`flwr.app.Context`](https://flower.ai/docs/framework/ref-api/flwr.app.Context.html)
