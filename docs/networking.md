---
title: Networking
description: What the platform needs from the network, and how to provide it with NetBird, Tailscale, WireGuard, OpenVPN or IPSec.
---

# Networking

No overlay network is required for federated training itself. This page
states what SwarmMed actually needs, so that a site can satisfy it with whatever
its information security team already approves. For a consortium that has no
network of its own yet, the documentation recommends [NetBird](https://netbird.io/)
with a management server that the consortium hosts itself: NetBird's control
plane is open source and supported for self-hosting, so no third party mediates
the connections between the sites. The worked example ran over
[Tailscale](https://tailscale.com/). The Hub reads this host's NetBird or
Tailscale address (see
[The overlay helper and the Hub](#the-overlay-helper-and-the-hub)). NetBird has
so far been exercised with SwarmMed only in a containers-only test on one host
([Validation status](#validation-status)).

## What the platform requires

**For federated training: one reachable address and one TCP port, at the
coordinating site.** Participating sites that run their own SwarmMedHub also use
a second port, the admin port, and their Hubs can exchange status over HTTPS
(see [Traffic between Hubs](#traffic-between-hubs)).

| Direction | Endpoint | Default port | Purpose |
|---|---|---|---|
| participating site → coordinating site | FL server, fed-learn port | 8002 | client registration, task exchange, model payloads |
| participating site's Hub, if it runs one → coordinating site | FL server, admin port | 8003 | job-status polling every 60 s; job submission, and the check before `swarmed training resume`, if training is started or resumed from that Hub |
| every Hub ↔ every other participant's Hub | SwarmMedHub HTTPS | 5085 (fixed in code) | status exchange and training-state mirroring; optional |
| users' browsers → their own site | SwarmMedHub web interface | 5085 (443 serves the same site) | the interface, at the site that hosts it |

The coordinating site's own Hub and the `swarmed` CLI reach the admin port from
the same host, so job submission and run control there need no firewall
opening. A participating Hub's job monitor connects to the admin port every 60 s
as soon as the startup bundle is imported; if the port is blocked, each attempt
waits for connection timeouts. The Hub's job records do not depend on it: they
come from the jobs the Hub submits and from the logs of its own FL containers.
With NVIDIA FLARE 2.7.1 the monitor does not yet find, in the server's job list,
jobs that other Hubs submitted. A Hub that resumes a run asks the server over
this port whether it still runs the job, or any other job
([Server failover](failover.md#resuming-the-run)).

Both server ports are configurable with `SWARMMEDHUB_FED_LEARN_PORT` and
`SWARMMEDHUB_ADMIN_PORT`. This is necessary when the host already runs another
FLARE deployment on 8002/8003: the FL server container cannot publish a port that
is already in use, so it would not start. The ports are written into the kits
when the network is provisioned, so set the variables before creating the
network, for both the `app` and the `celery_worker` service: provisioning from
the web interface or the CLI runs in the app container, and the Hub's status
display reads the fed-learn port in both. A third variable,
`SWARMMEDHUB_FLARE_ADMIN_PORT`, only adds a port to the connectivity probe
logged before each job submission. That probe also tries the admin port named
in the kit, and tries 8003 and 8002 only when neither the variable nor the kit
names a port.

Networks provisioned earlier keep their ports, and the Hub keeps polling every
network that is provisioned, starting or running, and every training job
recorded as running. Stopping a network marks its unfinished jobs as stopped,
but networks left running or provisioned continue to be probed. On a shared host,
polls for networks on the default ports reach the other deployment's server and
fail certificate verification, which fills the worker log. Stop or delete networks
you no longer use.

What this means for a participating hospital:

* **No inbound connections are needed at a participating site, provided it
  starts its runtime in client-only mode.** Every site's startup bundle also
  contains the FL server's startup kit. Without client-only mode, starting the
  network at a participating site also starts a local copy of the FL server,
  which publishes the fed-learn and admin ports on that host, and the site's
  client registers with that local server instead of the coordinator. In
  client-only mode the client dials out to the coordinating server and keeps the
  connection open; client-to-client messages are relayed by the server rather
  than sent site-to-site (see
  [Threat model](threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)).
  For training traffic, such a site therefore needs an outbound allow-rule to one
  host and one port, not a hole in its perimeter.
* **The FL client also listens locally.** It opens a TCP port chosen at random
  between 1025 and 65535 on all of the host's IPv4 addresses, without encryption,
  for its own job processes. Nothing outside the host needs to reach it, so keep
  it closed to other hosts. The client runs with host networking, so ordinary
  host firewall rules apply to it.
* **No public IP is needed at a participating site.** Each site does need a
  stable IPv4 address of its own (see [Addressing](#addressing)).
* A participating site that runs its own SwarmMedHub also uses the admin port
  and the Hub traffic in the table above; the Hub traffic needs inbound
  connections (see [Traffic between Hubs](#traffic-between-hubs)).
* The coordinating site needs an address the others can reach, and must accept
  inbound connections on the fed-learn port, and on the admin port and 5085 if
  participating sites run a Hub.
* A site agreed as the standby server (see [Server failover](failover.md)) needs
  the same address and inbound rules as the coordinating site, in case it has to
  take over the server.

This is the single most useful fact to put in front of an information security
committee. For federated training, it is what makes the choice of overlay
network a site decision rather than a platform constraint; the Hub's
conveniences, which read NetBird or Tailscale, are the exception.

Client-only mode, and the choice of which site's kit to start from the bundle,
are environment variables of the process that starts the network. That is the
Hub's Celery worker: a start from the web interface or the CLI is handed to it.
The site's name is also read in the `app` container, where the CLI and the web
interface import the startup package, so set it for both services:

```yaml
# docker-compose.override.yml at a participating site, next to docker-compose.yml
services:
  app:
    environment:
      - SWARMMEDHUB_LOCAL_PARTICIPANT=site-a   # the site's name as provisioned
  celery_worker:
    environment:
      - SWARMMEDHUB_CLIENT_ONLY_MODE=true
      - SWARMMEDHUB_LOCAL_PARTICIPANT=site-a
```

Apply it with `docker compose up -d app celery_worker`. Without
`SWARMMEDHUB_LOCAL_PARTICIPANT`, the start aborts unless the hostname or one of
the host's addresses (its NetBird and Tailscale addresses and those in
`SWARMMEDHUB_LOCAL_ADDRESSES`) matches a site in the bundle. On the coordinating host,
if it does not also train, set `SWARMMEDHUB_SERVER_ONLY_MODE=true` for the worker
instead; otherwise starting the network there also starts a client for every
site. The port variables above go in the same file, under both `app` and
`celery_worker`. These settings apply to every network the Hub starts. The
worked example set the same variables for the processes that started each
site's client and the coordinating server.

### How a Hub knows which site it is

A Hub learns which participant it is in two ways: from
`SWARMMEDHUB_LOCAL_PARTICIPANT`, the site's name as provisioned, or from its own
addresses, its NetBird and Tailscale addresses and those in
`SWARMMEDHUB_LOCAL_ADDRESSES`, one of which must then be the address the site
was provisioned with. They are used for different things:

| The Hub ... | with `SWARMMEDHUB_LOCAL_PARTICIPANT` | with the provisioned address among its own |
|---|---|---|
| starts the site's FL client from the bundle (`swarmed network start`, `swarmed network failover`) | yes, set for `celery_worker` | yes |
| records the jobs that its FL client runs, also those started at another Hub, and resumes them (`swarmed training checkpoints`, `swarmed training resume`) | not needed | not needed |
| sends its status to the other Hubs and mirrors their training state ([Traffic between Hubs](#traffic-between-hubs)) | no | yes |
| shows its own FL client as online in `swarmed network status` and on the Network page | no | yes |
| at the coordinating host, shows the clients' status from its FL server's log on the Network page | no: the server has no site name | yes |
| warns at import when a single site's kit is another site's | yes, set for `app` | no |

<!-- r106-check: rows 3-5 are matched by address only in the merged code (apps/network/views.py broadcast_network_status and the network view's i_am_server and is_me, apps/network/runtime.py own client Online, apps/training/views.py mirror header); row 2 needs the record-less resume, the label scrape and the jobs' own totals merged; a shared participant helper stayed deferred -->

A Hub records the jobs of its site from the jobs it submits and from the logs of
its own FL containers, which SwarmMed labels with the network's ID, whoever
submitted the jobs. `swarmed training resume` finds an interrupted job by its
folder in the network's workspace, also without a record. So job records and the
resume need neither setting, and no connection between the Hubs on port 5085.
Set `SWARMMEDHUB_LOCAL_PARTICIPANT` at every site that trains, the standby
included, for both services. List the server address in
`SWARMMEDHUB_LOCAL_ADDRESSES` at the coordinating host, unless it is one of the
host's NetBird or Tailscale addresses, and a site's provisioned address at any
site whose Hub is to exchange status with the other Hubs. Up to version 1.0.5, a
Hub without its provisioned address among its own did not record a job that
another Hub had submitted, and could not resume it
([Server failover](failover.md#testing-status)).

## Listening ports by host role

What a default installation listens on. "All addresses" means every IPv4 and
IPv6 address of the host.

**Coordinating host**

| Port (default) | Bound to | Service | Must be reachable from |
|---|---|---|---|
| 8002/tcp, fed-learn | all addresses | FL server container | every participating site's FL client |
| 8003/tcp, admin | all addresses | FL server container | participating sites' Hubs, if they run one |
| 5085/tcp | all addresses | nginx, HTTPS | users' browsers; other participants' Hubs (optional) |
| 443/tcp | all addresses | nginx, HTTPS, same site as 5085 | nothing in the platform |
| 80/tcp | all addresses | nginx, HTTP | nothing; it redirects to `https://<host>:5085` and serves Let's Encrypt challenges, which are not set up by default |
| 9000, 9001/tcp | `DOCKER_HOST_IP` | MinIO API and console | this host and its containers |
| 5432/tcp | 127.0.0.1 | PostgreSQL | this host |
| 6379/tcp | 127.0.0.1 | Redis, TLS only | this host |

On Linux, `make env` sets `DOCKER_HOST_IP` to the gateway address of Docker's
default bridge network as Docker reports it (usually 172.17.0.1, which is also
the fallback when Docker cannot be queried); on macOS and Windows it sets
127.0.0.1. A value already in `.env` is kept. `POSTGRES_BIND_ADDR` and
`POSTGRES_HOST_PORT` change the PostgreSQL binding. If the coordinating host is
also a training site, it runs an FL client as well, with the listener below.

**Participating site**

| Port (default) | Bound to | Service | Must be reachable from |
|---|---|---|---|
| random, 1025–65535/tcp | all IPv4 addresses | FL client's internal listener, unencrypted | this host only (the client's own job processes) |

A participating site that runs its own Hub also has the nginx, MinIO, PostgreSQL
and Redis ports listed for the coordinating host. Local test networks publish no
ports unless `SWARMMEDHUB_LOCAL_TEST_PUBLISH_PORTS` is set. `make docs-serve`, a
documentation preview, listens on 0.0.0.0:9999 while it runs.

Ports published by Docker (nginx's and the FL server's) are opened in Docker's
own packet-filter rules and are not filtered by host firewalls such as UFW.
Restrict them with rules in Docker's `DOCKER-USER` chain on the external
interface, or at a network firewall. SwarmMed has no setting that binds the FL
server's ports to a single address.

Besides the flows above, a host that starts a network builds the runtime image
at that point, downloading a base image from Docker Hub and Python packages from
PyPI. It does not if a prebuilt image is already on the host and configured with
`SWARMMEDHUB_FLARE_IMAGE` together with `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true`;
without the second variable the image is built anyway (see
[Runtime backend and portability](runtime-backend.md#nvflare-version)).
Installing the Hub also downloads software from public registries, and building
its images fetches MinIO's source code from GitHub, so `github.com` must be
reachable then.

## Traffic between Hubs

When participating sites run their own SwarmMedHub, the Hubs exchange status
over HTTPS on port 5085, which is fixed in the code. Training does not need any
of it: in the worked example the training hosts ran no Hub worker and sent none
of it.

* **Status exchange.** Every 30 s, and whenever someone opens the Network page,
  a Hub posts its status to
  `https://<participant address>:5085/network/api/gossip/<network>/` for every
  other participant of each running network. The coordinating Hub also forwards
  every client's status as its FL server's log shows it.
* **Training-state mirroring.** Every few seconds while someone has the Training
  page or the home page open, a Hub asks the coordinating Hub, then up to three
  participants' Hubs, for the current job state (`/training/api/state/<network>/`),
  waiting up to 2 s for each.

Both need inbound connections on port 5085:

* A participating Hub shows the other participants' status only if it accepts
  these connections, at least from the coordinating host. Otherwise its Network
  page shows them as "Offline" (never heard from) or "Offline (Stale)" (nothing
  for three minutes).
* Participating Hubs get the job state from the coordinating Hub only if it
  accepts their connections. Otherwise their Training page falls back to other
  participants' Hubs, their own client container's log and their admin-port
  polling. Their job records and `swarmed training resume` do not depend on this
  channel.
* On any Hub, each peer that silently drops these connections delays every
  status refresh of the Training and home pages by up to 2 s.
* A Hub that runs the network's FL server reads the clients' status from that
  server's log; see [The overlay helper and the Hub](#the-overlay-helper-and-the-hub)
  for when the Network page does so.
* Training, model exchange and results are not affected.

!!! note "Known limitation"
    In the worked example the coordinating Hub showed connected remote clients
    as offline. A defect in that display has been fixed since (its log patterns
    were compared with the wrong letter case, so join and heartbeat messages
    never matched), but it has not been checked again in a multi-site run. Check
    connectivity in the FL server's log instead (see
    [Troubleshooting](#troubleshooting)).

A Hub accepts these requests only if the address it was provisioned with is
listed in its `DJANGO_ALLOWED_HOSTS`; the same applies to every name or address
users type into their browsers. A Hub sends status only if it finds one of
its own addresses among the participants (see
[The overlay helper and the Hub](#the-overlay-helper-and-the-hub)). A Hub that
finds none says so in its log once every six hours. <!-- r106-check: only if it landed (a should-item: the "cannot tell which participant" notice at INFO, once per network per 6 h); if not, drop this sentence -->

The channel uses HTTPS without certificate verification by default
(`SWARMMEDHUB_SKIP_PEER_SSL_VERIFY` defaults to `true`). Turning verification on
does not work with the generated certificates: each site's web certificate is
issued by that site's own internal CA and names only the host's local names and
addresses (`localhost`, `swarmmedhub`, `host.docker.internal`, `127.0.0.1` and
`172.17.0.1`; before version 1.0.1 only `localhost` and `127.0.0.1`), not the
site's address, which is also why browsers on other machines warn about it.
Requests are authenticated with one token per network, carried in every site's
startup bundle, so anyone holding a bundle can post status for any participant.
Restrict port 5085 to the participants' addresses.

Training containers that read their data through the platform's data adapter
(the bundled examples do; the worked example read a local directory) ask their
own Hub for their data manifest, over HTTPS on port 5085 of their own host. Only
a container on a Docker network also tries plain HTTP to the Hub's application
container; one on the host's network never does, also when an earlier version of
SwarmMed started it. The data adapter comes with the job, so this holds for jobs
submitted by a Hub of version 1.0.6 or later. Versions 1.0.5 and earlier asked
the FL server's host first, after a server switch the standby site's host;
nothing needed to answer there.

## Addressing

The coordinating host's address is fixed when the network is provisioned. It is
written into every startup kit (`startup/server_host.txt`) and becomes a hosts
entry for the name `server` in the client containers at participating sites. It
must be an IPv4 address that every participant can reach and that stays the same
for the study.

Give it in the **FL server address** field of the network form in the web
interface, or with `--server-ip` when creating the network with the CLI, run
inside the Hub's app container (see [CLI](cli.md)):

```bash
docker exec -w /app swarmmedhub python3 -m swarmed_cli.main network create \
  --user alice --project <PROJECT_UUID> --name "Hospital Swarm" \
  --server-ip 10.90.0.1 \
  --participant site-a=10.90.0.12 \
  --participant site-b=10.90.0.13 \
  --no-local-client
```

* The address must be a dotted IPv4 address. A host name, an IPv6 address, an
  address with a prefix length (`100.64.0.10/16`), and loopback, link-local,
  multicast, reserved and `0.0.0.0/8` addresses are rejected, and the network
  is not created.
* Left empty, the address is this host's current overlay address, NetBird's or
  Tailscale's (see [The overlay helper and the Hub](#the-overlay-helper-and-the-hub)).
  The network is not created if there is none: if the overlay is not connected,
  or if the overlay helper has not reported for more than two minutes.
* If the server address is not one of this host's NetBird or Tailscale
  addresses, also list it in `SWARMMEDHUB_LOCAL_ADDRESSES`; otherwise the Hub
  does not recognise itself as the server. The CLI warns about this, and so
  does the Network page.
* The web interface always adds the coordinating host as a training site; the
  CLI does so unless `--no-local-client` is given.

Each participant also needs an IPv4 address of its own (`--participant NAME=IPv4`).
It does not have to be public or reachable from other sites, but it must stay
assigned to that site's host for the study: it is written into the startup
package, other Hubs use it to reach that site's Hub, and the site's Hub
recognises its own site by it for the status exchange between Hubs and, without
`SWARMMEDHUB_LOCAL_PARTICIPANT`, to pick the site's kit
([How a Hub knows which site it is](#how-a-hub-knows-which-site-it-is)). FLARE
itself does not use it: the FL client's job processes reach their parent process
at `localhost`. A participant entered without a valid IPv4 address gets no
startup kit; provisioning only logs a warning.

### The overlay helper and the Hub

SwarmMed does not install, configure or join an overlay network; joining one
(`netbird up`, `tailscale up`) is a manual step, and the Hub runs neither
client. `make start` and `make overlay` start a helper on the host,
`scripts/overlay_watcher.py`, which runs `netbird status --json` and
`tailscale status --json` every 30 s and writes this host's address and state
in each overlay it finds to `tmp/overlay_status.json`. The Hub's `app` and
`celery_worker` containers read that file through their bind mount of `tmp/`;
neither client's socket is mounted into a container. The helper needs only the
host's `python3` (3.8 or later). It logs changes of state and errors to
`tmp/overlay_watcher.log`, and keeps running after an error. It runs once per
checkout: while it runs it holds a lock on `tmp/overlay_watcher.pid`, which
names its process. `make overlay` and `make overlay-stop` go by that lock, not by
the number in the file, so a file left behind by a helper that was killed, or
that ended with the host, is not mistaken for a running helper, and no other
process is stopped in its place. It is not a service: after a reboot, start it
again with `make overlay`; `make overlay-stop` stops it.

`SWARMMEDHUB_OVERLAY` in `.env` selects whose address the Hub uses. A change
takes effect when the containers are recreated
(`docker compose up -d app celery_worker`).

| Value | Address the Hub uses |
|---|---|
| `auto` (default) | NetBird's if NetBird is connected, otherwise Tailscale's if Tailscale is connected; if neither is, the Network page shows the state of the first one installed. The Hub applies this order itself to every overlay the helper reports, whichever one the helper's `--provider` option put first |
| `netbird` | NetBird's only |
| `tailscale` | Tailscale's only |
| `none` | no overlay; the first address in `SWARMMEDHUB_LOCAL_ADDRESSES`, if any |

The Hub uses that address:

* as the server address of a new network when the form's **FL server address**
  field or the CLI's `--server-ip` is left empty. Only a current address is
  used: the network is not created if the overlay is not connected or the
  helper has not reported for more than two minutes;
* to recognise which participant it is. Every address the helper reports,
  NetBird's and Tailscale's, counts, and so does every address in
  `SWARMMEDHUB_LOCAL_ADDRESSES`; with `SWARMMEDHUB_OVERLAY=none`, only the
  latter count. A Hub sends status and mirrors training state
  ([Traffic between Hubs](#traffic-between-hubs)) only if one of these is the
  address it was provisioned with. The Network page shows client status from
  the FL server's log only at a Hub that recognises the server's address as its
  own; elsewhere it shows the status that other Hubs sent. `swarmed network
  status` shows client status from the log of the network's FL server container
  at this Hub while that container runs, also at a Hub that does not recognise
  itself, such as a standby after a switch; <!-- r106-check: the Network page is unchanged; the CLI reads the server log only while the container runs (StaleServerLogTests) -->
* as one of the ways, with `SWARMMEDHUB_LOCAL_PARTICIPANT` and the hostname, to
  pick the local client kit from a bundle;
* for the overlay status on the Network page: the address, NetBird's or
  Tailscale's state, the number of connected peers and the time of the last
  update. The page warns if the address is missing from
  `DJANGO_ALLOWED_HOSTS`, because the Hub then refuses the status messages that
  other Hubs send to it.

A status older than two minutes (`SWARMMEDHUB_OVERLAY_STALE_SECONDS`, default
120) is shown as stale; for a helper started with a longer `--interval`, the
limit is three of its intervals, at most 30 minutes. A status dated more than one
minute in the future is stale too:
a running helper replaces its status every 30 s with the current time, so such a
status was written before the host's clock was set back, by a helper that has
not written since, and its age is unknown. The address of a stale status is
still used to recognise this host, but not as the server address of a new
network. FL traffic uses none of this: the FL server and clients use the
provisioned addresses.

`SWARMMEDHUB_LOCAL_ADDRESSES` lists further IPv4 addresses of this host,
comma-separated, for example a WireGuard or routed address. `make env` drops
variables that are not in `.env.template`, so set it, like
`SWARMMEDHUB_OVERLAY_STALE_SECONDS`, for both the `app` and the `celery_worker`
service in `docker-compose.override.yml` (see
[What the platform requires](#what-the-platform-requires)).

Details:

* The helper reads the address from `netbirdIp` in `netbird status --json`. It
  does not use `netbird status --ipv4`, which prints login instructions instead
  of an address, and exits 0, when NetBird needs a login.
* From NetBird 0.83 (a release candidate when this was written, on
  11 November 2026), the NetBird daemon includes the address in its status only
  for root and the user who connected the client. The helper then reads the
  address of the interface `wt0` with `ip -4 addr`; set
  `SWARMMEDHUB_NETBIRD_INTERFACE` when starting the helper if NetBird uses
  another interface name.
* The helper of earlier versions, `scripts/tailscale_watcher.sh`, wrote
  `tmp/tailscale_status.json`. The Hub still reads that file when the new one is
  missing or stale, so a host on which the old helper still runs keeps working.
  Once the old file is stale and the new helper has written a more recent
  status, the Hub ignores the old file, also for recognising this host, because
  its address may since have been given to another peer. Stop an old helper
  with `pkill -f tailscale_watcher.sh` and start the new one with
  `make overlay`.
* `scripts/tailscale_watcher.sh` is kept under its old name and now starts the
  new helper with `--provider tailscale`. A cron entry or systemd unit that
  starts it therefore keeps working, and in mode `auto` the Hub still uses
  NetBird's address while NetBird is connected. Replace such entries with
  `make overlay`, which starts the helper as `make start` does.

## Verifying the startup package

The coordinating site exports one startup package for the whole network and hands
it to every other site outside SwarmMed. The package contains `manifest.json`,
which lists the SHA-256 of every other file in the package and sums up what the
sites share: the fingerprints of the root CA and FL server certificates, the FL
server's address as the kits' `server_host.txt` give it, the FL and admin ports,
and the participants with their addresses and certificate fingerprints. Every
site's kit in the package holds the same `manifest.json`. The **network
fingerprint** is the SHA-256 of that file, shown as 16 groups of four characters
so that it can be read out; `unzip -p <package>.zip manifest.json | sha256sum`
prints it too.

A Hub checks a package before it writes anything from it. Every file that the
manifest lists must be there with that SHA-256, no other file may be there, every
site's kit must hold the same manifest, and the manifest's summary must agree with
the files it sums up. In any package, entries that are symbolic links, whose path
is absolute or has a `..`, `.` or empty component (such as `./node-A.zip`), or
that lie in a folder named like a site's kit (such as `node-A.zip/startup/`) are
refused, and so are entries compressed with another method than deflate, unless
stored uncompressed: SwarmMed writes deflate, and Python's zipfile decompresses
bzip2 and LZMA data whole, whatever size an entry declares, and cannot read other
methods. So is a package of more
than 1 GiB, packed or unpacked, or with a file of more than 256 MiB, as the
package declares the sizes, before its files are read; the import unpacks no more
of a file than the size it declares. So is a package with more than 100,000
entries, counting those of its site kits, before the entries are read. A package
exported before the network first starts is far smaller. If anything differs, the
import names the first file that does and writes nothing. A single site's kit,
taken out of the package, is checked against the manifest's entries for that kit
and shows the same fingerprint as the whole package.

Hand the package over like this:

1. The coordinating site exports the package once (**Kits** on the Network page,
   or `swarmed network export-package`) and notes the network fingerprint:
   `swarmed network export-package` prints it, and the Network page shows it for
   the current network (after a download with **Kits**, reload the page).
   Export before the network first starts there: once it has run, the kits at the
   coordinating site can hold logs, job folders or changed settings, which a later
   export carries, so that export has another fingerprint. A Hub that imported a
   single site's kit cannot export the package, nor can one that imported it with
   `SWARMMEDHUB_SERVER_HOST` set to another address than the packaged one.
2. It sends that one file, over an encrypted channel, to a named person at every
   site.
3. Each site imports it with the fingerprint, which the coordinating site reads out
   by phone or video call: `swarmed network import --expect-fingerprint
   '<fingerprint>'`, or **Expected fingerprint** in the import form. A package with
   another fingerprint, or without one, is not imported. Alternatively, every site
   imports the package without it, and all sites compare the fingerprint shown
   after the import in one joint call before anyone starts the network. A site
   that imports only its own kit, taken out of the package, gets the same
   fingerprint; the import also names the kit's site, so check that it is yours.
   If `SWARMMEDHUB_LOCAL_PARTICIPANT` names another site, the import warns: with
   the kit of another site, the Hub takes part as that site.
4. Optionally, each site also compares the root CA certificate's fingerprint, which
   the Network page and `swarmed network show` give, with the output of
   `openssl x509 -noout -fingerprint -sha256 -in <kit>/startup/rootCA.pem`, and
   runs NVIDIA FLARE's check of the kit's signatures:
   `test -f <kit>/startup/signature.json && python -m
   nvflare.tool.verify_startup_kits -f <kit>/startup -c <kit>/startup/rootCA.pem`.
   The check prints nothing; exit status 1 means that a file in the folder does
   not match its signature, or has none. NVIDIA FLARE 2.7.1's check also passes a
   folder without `signature.json`, hence the `test -f`. It fails on every kit of a
   network provisioned by SwarmMed 1.0.4 or earlier, and on a kit whose
   `server_host.txt` the import rewrote
   ([NVIDIA FLARE's signatures](#nvidia-flares-signatures)). `<kit>` is a kit's
   folder in the Hub's `workspaces/<project>/<network>/workspace/<study>/prod_00/`
   (in the `app` container, under `/app/workspaces/`), or a site's kit unpacked
   from the package.

Compare the fingerprint among all sites, not only each site with the coordinating
site, which would not reveal a coordinating site that gave different sites
different packages. Never compare it through the channel that carried the package:
whoever can change the package there can change the fingerprint as well.

**What it protects against.** A package that was altered, substituted (for example
one from another provisioning, with another root CA), stale (exported before the
kits changed, or another network's) or corrupted on the way gets another
fingerprint, or is refused by the import.

**What it does not protect against:**

* A dishonest or compromised coordinating site. It holds the root CA's private key
  and every participant's key, so it can give every site the same package and
  still issue other identities.
* Someone who copies the package. Every site's kit carries every site's private
  key, the FL server's key and an administrator identity, so a copy lets its holder
  act as any site or as the server without changing a byte, and no fingerprint
  shows that a file was read. The package must still travel over an encrypted
  channel to a named person
  ([Key custody](threat-model.md#key-custody-what-the-startup-package-contains)).
* Changes after the import. The check runs once, when the package is imported. An
  import with `SWARMMEDHUB_SERVER_HOST` set writes that address into every kit's
  `server_host.txt`, and a server switch with `swarmed network failover` records
  the new address in `prod_00/.active_server.json`, which takes precedence over
  the kits ([Server failover](failover.md)).
* The code that runs: container images and job code are not part of the package.

Packages exported by SwarmMed 1.0.4 or earlier have no manifest. Such a package is
imported as before, with a warning that it has no fingerprint; with
`--expect-fingerprint` it is refused. Verify it by other means: send the same file
to every site, and have all sites compare its SHA-256 (`sha256sum <file>`) and the
root CA's fingerprint in one joint call before importing. To compare fingerprints,
every site needs a SwarmMed version that checks the manifest. An import of a
network that already exists at the Hub is refused: to import a network's package
again, for example to get the fingerprint of a network imported from a package
without a manifest, delete the network at that Hub first, which also removes its
folder and its training jobs there. The Hub removes the folder in the background,
after it has synchronised the project's results; import the package again only
once `workspaces/<project>/<network>/` is gone. A package that SwarmMed 1.0.4 or earlier
exported again, from a network imported from a later version, is refused: its
kits carry a manifest that the package lacks. Use the package that the
coordinating site exported.

### NVIDIA FLARE's signatures

NVIDIA FLARE signs each kit's `startup` and `local` folders with the root CA's key
(`signature.json`). SwarmMed starts FLARE in secure mode, in which a server or
client refuses to start if its configuration, certificate or key files do not
match their signatures. SwarmMed adds its own files to each kit's `startup` folder
after FLARE has signed it (`.all_participants.json`, `server_host.txt` and, in
client kits, `server_aliases.txt`), and then signs that folder again with the root
CA's key, so that FLARE's verifier passes on the kits' `startup` folders as
provisioned; on kits provisioned by version 1.0.4 or earlier it fails. Run it on a
`startup` folder, as above, not on a whole kit. NVIDIA FLARE 2.7.1 writes the FL
server's `local/comm_config.json` after it has signed that folder, so the verifier
fails on the server's `local` folder and on the server's kit as a whole, also in
kits that `nvflare provision` makes without SwarmMed for a server with a
`listening_host`, as SwarmMed's provisioning gives it. A Hub that rewrites
`server_host.txt` later, as `SWARMMEDHUB_SERVER_HOST` does at import when it
differs from the packaged address, changes a signed file, and FLARE's verifier then
fails for that kit (exit status 1, without naming the file); FLARE's start-up check
does not cover the file. A server switch leaves the kits' `startup` folders
unchanged. These signatures cannot show that two sites hold the same package: a
kit from another provisioning carries its own root CA and passes against it. The
network fingerprint, or the root CA's fingerprint compared outside the package,
tells them apart.

## Options

The Hub reads NetBird and Tailscale (see
[The overlay helper and the Hub](#the-overlay-helper-and-the-hub)). With any
other option, enter the server address in the network form or give it with the
CLI's `--server-ip`, and set `SWARMMEDHUB_OVERLAY=none`. At each site set
`SWARMMEDHUB_LOCAL_PARTICIPANT`, and list the address the site was provisioned
with in `SWARMMEDHUB_LOCAL_ADDRESSES` where the Hub is to exchange status with
other Hubs; at the coordinating host list the server address there
([How a Hub knows which site it is](#how-a-hub-knows-which-site-it-is)).
Tailscale (the worked example), an existing VPN between institutions (the
three-hospital runs of the ECG template) and, within one institution, the hosts'
own addresses (a failover rehearsal) have been used for training across hosts so
far; NetBird has been exercised in a containers-only test on one host
([Validation status](#validation-status)).

### NetBird (recommended)

[NetBird](https://netbird.io/) connects hosts with WireGuard tunnels, directly
where the networks allow it and through a relay otherwise. Its management server
enrols the hosts, assigns their addresses and distributes the access policies;
its signal server helps peers find each other. Both, and the relay, can be run
by the consortium itself, so no third party mediates the connections between
the sites. NetBird Cloud, the hosted service of NetBird GmbH
([app.netbird.io](https://app.netbird.io/)), is a quick way to try NetBird; for a
study, host the management server yourself.

Licences: the client (command line, daemon and desktop interface) is
BSD-3-Clause. The management, signal and relay servers have been AGPL-3.0 since
v0.53.0 (August 2025), and so are the combined server image
`netbirdio/netbird-server` and the dashboard
([NetBird's announcement](https://netbird.io/knowledge-hub/netbird-agpl-announcement)).
SwarmMed contains no NetBird code; its helper runs the client's command line as a
separate program.

#### Self-hosting the management server

NetBird's [self-hosting quickstart](https://docs.netbird.io/selfhosted/selfhosted-quickstart)
installs, with one script, a combined server (management, signal, relay, STUN
and an embedded identity provider), the dashboard, and Traefik with
certificates from Let's Encrypt. It needs a Linux host with at least 1 CPU and
2 GB of RAM, a public domain name that points at it, and TCP 443 and UDP 3478
reachable from the internet. TCP 80 must be free on the host, because Traefik
binds it to redirect HTTP to HTTPS, but need not be reachable: Traefik obtains
its certificate over port 443 (the TLS-ALPN-01 challenge; checked in NetBird's
quickstart on 11 November 2026). The peers need no inbound port, only
outbound access to these
([ports](https://docs.netbird.io/about-netbird/ports-and-firewalls)). The
script uses the images' `latest` tags unless `NETBIRD_SERVER_IMAGE` and
`DASHBOARD_IMAGE` name versions; pin them for a study. The
[advanced guide](https://docs.netbird.io/selfhosted/selfhosted-guide) covers
existing identity providers and separate containers. Back up the server's data
directory: it holds the peers, their addresses and the policies. In the
[test on one host](#netbird-test-on-one-host), established connections kept
working while the management server was stopped for 90 s, but a peer whose
NetBird daemon restarted in that time had no address until the server was
back.

#### Installing the client and joining

On every host that runs the FL server or trains
([other platforms and the APT and RPM repositories](https://docs.netbird.io/get-started/install/linux)):

```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh    # uses NetBird's apt, dnf or yum repository if it finds one
sudo netbird up --management-url https://netbird.example.org --setup-key <SETUP_KEY>
netbird status       # NetBird IP: 100.64.0.10/16, and whether management and signal are connected
netbird status -d    # also every peer this host may reach, with its address and connection type
```

For a trial on NetBird Cloud, leave out `--management-url`. The coordinating
host's NetBird address, without the `/16`, is the FL server address. NetBird
also gives every peer a DNS name (`<name>.netbird.selfhosted` on a self-hosted
server), but startup kits need the IPv4 address: the server address becomes a
Docker hosts entry in the client containers.

#### Setup keys and groups

Hosts enrolled with a setup key need no user login, and their login does not
expire. Create the keys in the dashboard (**Setup Keys**) or with the
[API](https://docs.netbird.io/api/resources/setup-keys), and let each key put its
hosts into a group:

* a one-off key for the coordinating host, with the group `fl-server`;
* a reusable key, limited to as many uses as there are participating sites,
  with the group `fl-clients`.

Do not make the keys ephemeral: NetBird deletes an ephemeral peer after 10
minutes offline, and a host that joins again may get a new address. Hand the keys
to the sites over an encrypted channel, as you would the startup package, and
revoke them once the hosts have joined.

#### Limiting the sites to the FL ports

A new NetBird account has a policy `Default` that lets every peer reach every
other on any protocol; NetBird's
[access control documentation](https://docs.netbird.io/manage/access-control)
recommends replacing it. One policy covers federated training: participating
sites may open TCP connections to the coordinating host's fed-learn and admin
ports. Create it in the dashboard (**Access Control**, **Policies**) or with the
[API](https://docs.netbird.io/api/resources/policies), then disable or delete
`Default`:

```json
{
  "name": "fl-clients-to-fl-server",
  "enabled": true,
  "rules": [{
    "name": "fed-learn and admin ports",
    "enabled": true,
    "action": "accept",
    "bidirectional": false,
    "protocol": "tcp",
    "ports": ["8002", "8003"],
    "sources": ["<id of the group fl-clients>"],
    "destinations": ["<id of the group fl-server>"]
  }]
}
```

Use the configured ports if `SWARMMEDHUB_FED_LEARN_PORT` or
`SWARMMEDHUB_ADMIN_PORT` are set. NetBird's firewall is stateful, so replies need
no rule. If the sites run a Hub, add a rule for TCP 5085 with
`"bidirectional": true` between the two groups: the participating Hubs then
exchange status with the coordinating Hub, which forwards every client's
status, but not with each other ([Traffic between Hubs](#traffic-between-hubs)).
This second rule has not been tested; the first, with the ports 8012 and 8013,
was used in the [test on one host](#netbird-test-on-one-host).
`netbird status -d` lists only the peers that a policy connects with this host,
in either direction.

#### Addresses

Each NetBird account takes its peers' addresses from one range, by default a /16
chosen at random within 100.64.0.0/10. A peer keeps its address while it keeps
its NetBird state (`/var/lib/netbird`), is not deleted and is not ephemeral, and
while the range is not changed. Changing the range (**Settings**, **Networks**,
since v0.53.0) gives every peer a new address at once
([network settings](https://docs.netbird.io/manage/settings/networks)). The
addresses are written into every startup kit, so choose the range before the
first network is provisioned and do not change it during a study; otherwise the
network has to be provisioned again. Choose a range that no site uses, and avoid
Docker's 172.17.0.0/16 and its default address pools.

#### Hosts that also run Tailscale

On Linux, Tailscale by default adds a firewall rule that drops packets from
100.64.0.0/10 that do not arrive on its own interface
([Tailscale's documentation](https://tailscale.com/docs/reference/cgnat-interoperability)).
NetBird's default range lies inside 100.64.0.0/10, so on a host that runs both,
NetBird's traffic would be dropped. This follows from Tailscale's documentation
and code; it has not been tested with SwarmMed, and neither have these remedies:

* give the NetBird account a range outside 100.64.0.0/10 before any network is
  provisioned (see [Addresses](#addresses));
* or have the tailnet's administrator set the node attribute
  `disable-linux-cgnat-drop-rule` for these hosts in the tailnet policy file;
  Tailscale then recommends keeping reverse-path filtering on
  (`sysctl net.ipv4.conf.all.rp_filter` other than 0);
* or start Tailscale with `--netfilter-mode=nodivert`, as Tailscale's
  documentation describes;
* or do not run both on the hosts that run the FL server or train.

Both clients also manage the host's DNS resolver; let only one of them do so
(`tailscale set --accept-dns=false` or `netbird up --disable-dns`). This has not
been tested either.

### Tailscale (used in the worked example)

WireGuard-based, with a coordination service run by Tailscale Inc. Install it
on every host, join the same tailnet with `sudo tailscale up`, and use the
coordinating host's address (`tailscale ip -4`) as the server address. The
client is open source (BSD-3-Clause); the coordination service is not, and
cannot be self-hosted. Tailscale's clients can be pointed at a self-hosted,
Tailscale-compatible control server from a third-party project such as
Headscale (`tailscale up --login-server <url>`); that combination has not been
exercised with SwarmMed. The tailnet's access control policy can limit the
sites to the FL ports in the same way as NetBird's.

### Self-hosted WireGuard

No control plane at all: a small set of static peers, which is often the easiest
thing for a hospital network team to reason about and audit.

```ini
# /etc/wireguard/wg0.conf at a participating site
[Interface]
PrivateKey = <site private key>
Address    = 10.90.0.12/24

[Peer]                       # the coordinating site
PublicKey  = <coordinator public key>
Endpoint   = coordinator.example.org:51820
AllowedIPs = 10.90.0.1/32    # only the FL server, nothing else
PersistentKeepalive = 25
```

`AllowedIPs` restricted to the coordinator's address is worth pointing out in a
security review: the tunnel cannot reach anything else on the consortium network.
Only the coordinating site needs a reachable UDP endpoint. Provision each site
with its tunnel address (`site-a=10.90.0.12`) and the server with
`--server-ip 10.90.0.1`. In this layout a participating site's Hub can exchange
status with the coordinating Hub, which forwards every client's status, but not
with other participating sites' Hubs.

### OpenVPN

Where a hospital already operates an OpenVPN concentrator, join it rather than
introducing another tunnel. Route the coordinating host's address over the
existing profile; FLARE traffic needs no platform change. Where OpenVPN
authenticates each site with its own certificate, that authenticates the hosts
independently of the FLARE study certificates.

### Site-to-site IPSec

The usual arrangement between institutions that already exchange traffic. The
network teams configure a tunnel between each participating site's gateway and
the coordinating site's; the platform sees ordinary routed addresses and needs
no VPN client on the host at all. This is the heaviest to set up and often the
easiest to get approved, because it is what the institution already does.

### No overlay at all

If the coordinating host is reachable on a routed address that participating
sites can reach — for example within one national research network, or via a
firewalled public address restricted to the participants' source ranges — FLARE
traffic does not need a tunnel.

Federated-learning traffic (the fed-learn and admin ports) is protected by mutual
TLS in all cases. Two things limit what that TLS provides on its own:

* The startup bundle that every site imports contains the FL server's private
  key and every site's client and admin keys. TLS therefore does not stop one
  consortium member from impersonating the server, or another site, on a network
  path it can influence.
* The Hubs' status traffic on port 5085 is HTTPS without certificate
  verification by default (see [Traffic between Hubs](#traffic-between-hubs)).

An overlay with per-host keys therefore adds host authentication that TLS alone
does not provide here, on top of network-level isolation: traffic to the
coordinator's overlay address reaches only the host that holds the coordinator's
overlay key. Source-address filtering on the server's ports limits who can
connect to the server.

## Validation status

| Option | Status |
|---|---|
| Tailscale | Used for the worked example: the coordinating host and three training hosts on one tailnet, with the coordinating host's Tailscale address as the server address |
| NetBird | Exercised in a containers-only test on one host on 11 November 2026 ([below](#netbird-test-on-one-host)); not yet used for training across hosts |
| An existing VPN between institutions (neither NetBird nor Tailscale) | Used for training across three hospitals and a coordinating host on 13 and 16 November 2026 ([ECG template](https://github.com/KatherLab/SwarmMed/blob/main/examples/ecg_ptbxl/README.md#what-was-run)): server address given with `--server-ip` and listed in `SWARMMEDHUB_LOCAL_ADDRESSES` at the coordinating host; the hospitals connected outbound only; Docker's default address pools had to be moved off the VPN's address range |
| Direct routed addressing, no overlay | Used once, within one institution: in the failover rehearsal of 23 November 2026 on two lab machines, the sites reached the FL server, and later the standby, at the machines' own addresses, with the Hubs in containers on them ([Server failover](failover.md#testing-status)); no platform change was needed for FLARE traffic. Not yet used between institutions |
| Self-hosted WireGuard | Not yet exercised; no platform change expected for FLARE traffic; give the server address explicitly and set `SWARMMEDHUB_OVERLAY=none` |
| OpenVPN, site-to-site IPSec | Not yet exercised; no platform change expected for FLARE traffic; give the server address explicitly and set `SWARMMEDHUB_OVERLAY=none` |

<!-- r106-fill: after the 1.0.6 re-run, update the "Direct routed addressing, no overlay" row (and the matching clause in "Options") if the re-run used the machines' own addresses again, for example "Used in two failover rehearsals within one institution (23 November 2026 and <date>)" -->

Federated training has no dependency on any of them beyond reachability and a
stable IPv4 address for every participant, so for FLARE traffic the untested
rows are a matter of local network configuration. The Hub's conveniences read
NetBird or Tailscale (see
[The overlay helper and the Hub](#the-overlay-helper-and-the-hub)). The Hub's
reading of NetBird (the overlay helper, the Network page, and the server
address in the network form and the CLI) is covered by unit tests; no Hub has
yet created or started a network over NetBird.

Once the coordinating site has started the network, confirm reachability from
each site with `nc -zv <server-address> <fed-learn port>` (8002 by default)
before starting that site's client. Before the server runs nothing listens on
the port, so a refused connection is expected; to test the path in advance, run
a temporary listener on the coordinating host (`nc -l 8002` with the OpenBSD
netcat that Ubuntu ships).

### NetBird test on one host

On 11 November 2026, NetBird was exercised with SwarmMed in a test that ran
entirely in containers on one Linux host, on a private Docker network without
published ports, with NetBird 0.82.1 (server and client), Docker 29.5.2 and
NVIDIA FLARE 2.7.1 in SwarmMed's runtime image:

* A self-hosted NetBird server (`netbirdio/netbird-server`, with management,
  signal, relay and STUN in one container, plain HTTP) and three peers
  (`netbirdio/netbird`, userspace WireGuard), enrolled with setup keys: the FL
  server's peer in a group `fl-server`, two clients' peers in a group
  `fl-clients`.
* Startup kits provisioned by SwarmMed's own provisioning code, with the
  server's NetBird address as the server address (fed-learn port 8012, admin
  port 8013), and a swarm job built by SwarmMed from `examples/pytorch` with two
  clients and five rounds. The FL server and clients ran in containers that
  shared the peers' network namespaces, started by a test script rather than by
  the Hub. The job completed over the overlay in 24.9 s, with the same final
  model on both clients, and an admin session over port 8013 from a client's
  peer worked.
* A policy that let `fl-clients` open TCP connections to `fl-server` on ports
  8012 and 8013 only, with `Default` disabled, applied within 5 s. Afterwards
  only these connections were open; everything else tried was dropped: other
  ports, connections from the server to the clients and between the clients,
  and ICMP. A second five-round job completed under this policy in 30.8 s.
* While the management server was stopped for 90.7 s, the peers stayed
  connected and kept their addresses and links, and new TCP connections and a
  new admin session to the FL server over the overlay worked. A peer whose
  NetBird daemon was restarted during the outage had no address until the
  management server was back. One ping from a client to the server got through
  during the outage although the policy blocks ICMP; this single observation is
  unexplained.
* The overlay helper, run in a fourth peer, reported each state the test
  produced: not logged in, connected, management server down (still connected,
  with the error), reconnecting without an address, and no daemon.

No 40-round job completed in this test. Three attempts, one during the outage
and two with the management server running, stopped with an execution error,
which the test traced to a timing race in NVIDIA FLARE 2.7.1's swarm client
controller (a site received a learn task while the previous one was still
finishing), not to the network.

Not tested: hosts in different networks or institutions (all peers shared one
Docker network, so relays, STUN and NAT traversal were not exercised), TLS to
the management server and login through an identity provider, a host that also
runs Tailscale, throughput and latency over a wide-area network, kernel
WireGuard, long-running jobs, and the Hub creating and starting a network over
NetBird.

## Troubleshooting

* If the Network page shows the overlay status as stale, or reports no status
  from the overlay helper, the helper is not running: start it on the host with
  `make overlay` and check `tmp/overlay_watcher.log`. With NetBird, `netbird status -d` shows whether
  the management and signal servers are reachable and the state of every peer
  this host may reach; with Tailscale, `tailscale status`.
* A client that starts but never appears as registered is usually blocked
  outbound to the fed-learn port, pointed at an address that changed after
  provisioning, or started without client-only mode. In the last case the site
  also runs a `swarm-…-server` container, and its client joined that.
* The coordinator's address is stored in each kit's `startup/server_host.txt` and
  applied as a hosts entry for the name `server` when the client container
  starts. If the address changes, set `SWARMMEDHUB_SERVER_HOST=<new IPv4>` for
  both the `app` and the `celery_worker` service, under both services in
  `docker-compose.override.yml`: the worker starts the client, and in the `app`
  container the CLI and the web interface submit jobs through an admin session
  that tries that address, and `swarmed network status` reports it. Recreate
  both containers (`docker compose up -d app celery_worker`) and restart the
  client in client-only mode; the variable takes precedence over
  `server_host.txt`. Re-provisioning also works, but it issues new certificates
  that must be redistributed to every site.
* Job submission uses only the admin port, which the coordinating Hub reaches
  from the same host, so a submission that fails while clients are connected is
  rarely a firewall problem. The training log lists the hosts and ports the
  check before submission tried.
* A site's Hub lists a job that another Hub started
  (`swarmed training list --network <NETWORK_UUID>`) once its job monitor, which
  runs every minute, has read the job in the log of the site's running FL client
  container. Containers that SwarmMed did not start carry no
  `swarmmedhub.network_id` label, and their logs are not read. A Hub that does
  not list the job can still resume it:
  `swarmed training checkpoints <JOB_UUID> --network <NETWORK_UUID>` finds the
  job by its folder in the network's workspace
  ([Server failover](failover.md#resuming-the-run)). <!-- r106-check: scrape_docker_progress(network_id=...) filters docker ps by label=swarmmedhub.network_id=<network UUID> and the generic fallback is gone (TrainingLogSummaryTests::test_docker_scrape_reads_only_the_networks_labelled_containers, SiteRecordTests) -->
* Check what the server sees, on the coordinating host:
  `docker logs swarm-<first 12 characters of the network ID>-server | grep "New client"`
  (for example `swarm-1a7f19db-dd2-server`). The address after `@` is the one
  the client reports for itself (normally the address of its default route), not
  the address its connection came from. The same lines are in the server's
  `log.txt` under
  `workspaces/<project ID>/<network ID>/workspace/<project name>/prod_00/server/`.
