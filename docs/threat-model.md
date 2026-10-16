---
title: Threat model
description: What SwarmMed protects, what it assumes, and what remains exposed.
---

# Threat model and cryptographic architecture

This page states precisely what leaves a participating institution during a SwarmMed
study, how it is protected, which adversaries the design does and does not defend
against, and what remains exposed. It is deliberately conservative: decentralised
learning reduces data movement, it does not make a collaboration private by
construction.

!!! warning "Pre-release software"
    SwarmMed has not undergone an external security audit or penetration test.
    The statements below describe the implemented design, verified against the
    source and against a running deployment; they are not an assurance of security.

## 1. What leaves a site

During training, a site exchanges three kinds of traffic with the FL server. Its
web interface adds a fourth, with the other participants' web interfaces.

| Category | Content | Size |
|---|---|---|
| **Model parameters** | The full model weight tensors after local training, and the aggregated weights received back. No gradients are sent in the default configuration: local training runs to completion and the resulting weights are exchanged. | ~94 MB per copy in the worked example (MST breast-MRI model). A site sends and receives about one copy per round, two in rounds where it aggregates. |
| **Optimisation metadata** | Round number, the local training-step count reported with each update (`NUM_STEPS_CURRENT_ROUND`, used as the aggregation weight; with the Lightning integration this is `trainer.estimated_stepping_batches`), task and job identifiers, site name and workflow status reports. Validation metrics are not sent: they are attached only if the executor runs with `train_with_evaluation`, which the SwarmMed job does not set. | Travels in the same messages as the parameters; small by comparison. |
| **Orchestration messages** | Task assignment and acknowledgement, heartbeats, client registration, run lifecycle events, admin commands. Each site's web interface also opens an admin session to the FL server every 60 s to list jobs. | Not measured for the worked example. In the [scalability](scalability.md) benchmark (model of about 10 kB, 3 rounds), all traffic through the FL server, job deployment included, averaged about 0.2 MB per client per round with 3 clients and 1 MB with 20; the model was a small part of it. |
| **Hub-to-hub status** | Participant status and training progress, exchanged directly between the participants' web interfaces over HTTPS on port 5085 ([below](#the-hub-to-hub-status-channel)). | Small JSON messages, every 30 s to each peer. |

Raw images, DICOM headers, patient identifiers, labels and local file paths are not
part of any message the FLARE workflow or the hub-to-hub channel sends. The job code
can still move such data off a site: it runs at every site with access to that
site's data ([section 4](#4-adversaries-considered)).

### Where training data is read

Data loading, preprocessing and training run inside the site's own client
container. The container gets its data in one of two ways, and neither sends raw
data off-site.

* **Local directory.** The directory named by `DATA_DIR`, in the environment of the
  process that starts the site's client, is mounted read-only into the client
  container. The platform does not copy, upload or index it. The worked example
  used this path.
* **The site's object store.** Files imported through the web interface or with
  `swarmed data import` are stored in the site's own MinIO. A training script that
  uses the platform's data adapter (`flare.get_data_filesystem`, as in
  `examples/breast_mri`) fetches a manifest of presigned URLs from the platform and
  streams each file from MinIO into the container. In a multi-site network the
  manifest lookup tries the coordinating host's web interface first
  (`https://<server-host>:5085`) and the site's own afterwards; the request carries
  the project identifier and the project's manifest secret, not data. The attempt
  at the coordinating host normally fails certificate verification, because each
  installation has its own internal CA.

!!! note "Fallback addresses in the object-store path"
    When a presigned URL fails, the data adapter (`apps/training/flare_adapter.py`)
    retries the same object path on a fixed list of local host names and
    addresses: `minio`, `host.docker.internal`, `localhost`, `127.0.0.1`, the
    configured `DOCKER_HOST_IP` and the container's own address. None of these
    requests, the first included, verifies certificates, and the retries also try
    plain HTTP. The object path contains each file's uploaded relative path, which
    can include exam or patient identifiers. Versions before 9 October 2026 tried a
    fixed overlay address, `100.127.11.1`, first; on an overlay network that uses
    the 100.64.0.0/10 range that address can belong to another peer, so update
    installations from before that date.

### The hub-to-hub status channel

Separately from FLARE, each site's web interface talks directly to the other
participants' web interfaces, over HTTPS to port 5085 at the addresses recorded for
the network:

* **Status gossip.** Every 30 s, for each running network, the site's worker POSTs
  a small JSON message to every other participant
  (`/network/api/gossip/<network>/`): the sender's participant ID and its status,
  for example `Online`. The site that runs the FL server also reports the status it
  sees for every other participant.
* **Training-state mirroring.** When a user's browser polls the training status,
  the web interface asks the FL server's site and up to three other participants
  for their latest job state (`/training/api/state/<network>/`): job identifiers,
  status, rounds finished and progress.
* **Model download.** Each web interface serves the global model of the network's
  most recent completed job at `/training/api/results/<network>/`. Nothing in
  SwarmMed calls it automatically, but any token holder can.

**Authentication** is one shared token per network, sent in the `X-Gossip-Token`
header together with a participant ID that the receiver takes as given. The gossip
endpoint only logs a mismatch between the sender's address and the address
recorded for that participant; the other two do not check it. The token is
generated at provisioning, written into the startup package, so every site holds
it, and passed into every FLARE container as `GOSSIP_TOKEN`. None of the three
endpoints requires a login when the token is present.

**Certificates are not verified** by default (`SWARMMEDHUB_SKIP_PEER_SSL_VERIFY`
defaults to `true`). Each installation's web certificate is issued by its own
internal CA for `localhost` and `127.0.0.1` only, so turning verification on also
requires certificates that name each site's address and a CA bundle containing
every site's internal CA (`SWARMMEDHUB_CA_CERT`). With verification off, an
attacker on the path can capture the token and then use it as above.

Training does not depend on this channel. Failures are ignored silently; in the
worked example, training completed while the web interfaces showed remote
participants as offline.

## 2. Transport and identity

* **Mutual TLS on every connection between a participant and the FL server.** At
  provisioning, NVFlare issues an X.509 certificate to every participant (server,
  each client, each admin identity) from a project root CA generated for that
  study. Every client-to-server and admin-to-server connection presents a
  certificate and verifies the peer's, so a host without a startup kit cannot join.
  Who holds which kit is the subject of
  [Key custody](#key-custody-what-the-startup-package-contains) below.
* **Plain TCP inside each participant (bound to loopback).** NVFlare's parent and
  job processes at each participant talk over an internal connection without TLS
  or certificates (`"connection_security": "clear"`). SwarmMed provisions client
  kits without an external `listening_host` (and normalizes any legacy
  `local/comm_config.json` internal host to `localhost`), so the internal parent-to-job
  listener binds to `localhost` (`127.0.0.1`) rather than external network
  interfaces, whether the client container runs in host-network or bridge-network mode.
* **Protocol versions and ciphers are not pinned for FLARE connections.** They are
  negotiated by the BoringSSL library bundled with gRPC, not the system OpenSSL, and
  its defaults allow TLS 1.2 and 1.3. The web interface's nginx pins TLS 1.2 and 1.3
  with `ssl_ciphers HIGH:!aNULL:!MD5`. Do not quote a specific cipher suite in an
  information-security questionnaire without checking the hosts.
* **Separate internal PKI.** PostgreSQL, PgBouncer, Redis and MinIO use TLS with
  server certificates issued by a second, internal CA that each installation
  creates for itself. Clients authenticate with passwords or access keys, not client
  certificates; in the default configuration Django connects to PostgreSQL
  directly. Only the connection to the sandbox Docker daemon uses mutual TLS, with
  certificates generated by the docker:dind image's own CA. None of this is related
  to the NVFlare study PKI.
* **No certificate verification between web interfaces** by default
  ([section 1](#the-hub-to-hub-status-channel)).
* **Transport confidentiality only.** TLS protects each hop. It does not, by itself,
  keep an intermediary from seeing a payload it relays — see the next section.

### Key custody: what the startup package contains

Provisioning runs on the host where the network is created; in the worked example
that host also ran the FL server. It generates the study's root CA and every
participant's key pair, and keeps all of them: the network's workspace
(`workspaces/<project>/<network>/workspace/<study>/`) holds `state/cert.json` with
the root CA's private key and every participant's private key, plus a kit for every
participant. On the provisioning host of the worked example these files have mode
644 (readable by all users), so their protection depends on the permissions of the
directories above them.

The startup package exported from the web interface or with
`swarmed network export-package` always covers the whole study: one kit per client
site (`node-A.zip`, `node-B.zip`, …), each containing:

| Path in a site's kit | Contents |
|---|---|
| `startup/` | The site's own client certificate and private key, and a second key pair in the site's name, which NVFlare creates because SwarmMed gives each client a listening address |
| `admin_startup/` | The site's admin identity, `admin-<site>@nvidia.com`, with NVFlare's `project_admin` role, and its private key |
| `server_startup/` | The FL server's certificate and private key, identical in every kit |
| `.gossip_token` | The network token for the hub-to-hub channel |
| `.participants.json` | Every participant's name, organisation and IP address |

The root CA certificate is included; its private key is not.

Every site imports the whole package, and the import unpacks every kit, so each
site ends up holding every client's private key, every admin identity and the FL
server's private key. In the worked example the same package was imported on all
three client hosts, which were then started in client-only mode
(`SWARMMEDHUB_CLIENT_ONLY_MODE=true` with `SWARMMEDHUB_LOCAL_PARTICIPANT`) so that
they would not also start the FL server and the other sites' clients from those
copies. The import code also accepts a single site's kit taken out of the package.
That leaves out the other sites' client and admin keys, but not the FL server's key
or the `project_admin` identity, and it was not used in the worked example.

Consequences:

* Any participant can present any other participant's identity, or the FL
  server's. A certificate shows that its holder is in the study, not which site it
  is.
* Any participant can submit jobs whose code runs at every site
  ([section 4](#4-adversaries-considered)).
* `SWARMMEDHUB_PRIVATE_P2P` cannot hide model updates from anyone who holds the
  receiving client's key
  ([section 3](#3-the-coordinating-host-sees-model-updates-default-configuration)).
* There is no revocation. A leaked kit or package stays valid until the
  certificates expire, 360 days after provisioning (NVFlare's default, which
  SwarmMed does not change).
* Any member of the project at a site that holds the network can export the whole
  package again from the web interface. Exports from the web interface are written
  to the audit log; exports with `swarmed network export-package` are not.

What an operator should do until SwarmMed can export per-site kits:

1. Treat the package as the study's master credential. Move it only over an
   encrypted, authenticated channel to a named person at each site, and delete
   every copy outside the web interface's workspace once it has been imported.
2. Restrict access to the `workspaces/` directory on every host that holds the
   network, and limit project membership to people who may hold the study's keys.
3. Record in the consortium agreement that every participant holds every identity,
   including the FL server's, and that each site's admin identity can run code at
   every site.
4. If a kit or the package may have leaked, or a site leaves, stop the network and
   provision a new one. Do the same before the certificates expire if the study
   will run longer than 360 days.
5. When the study ends, delete the network in the web interface on every host. This
   removes its workspace and the keys in it, but also the audit-log entries linked
   to the network ([section 6](#6-data-at-rest-and-auditability)), so export what
   must be kept first.

## 3. The coordinating host sees model updates (default configuration)

This is the most important limitation on this page.

NVFlare's swarm workflow is *client-controlled*: training and aggregation run on the
clients, and the aggregating client is drawn at random from all participants at the
start of every round, so any site can be the aggregator, possibly in consecutive
rounds. However, client cells connect to the server cell rather than to one another
(`"allow_adhoc_conns": false` in every kit), so client-to-client messages —
including the model payloads — are **relayed through the FL server**. NVFlare can
additionally encrypt such a message end-to-end with the recipient's certificate, so
that the relay cannot read it, but only when the task is marked secure, which
requires both the `private_p2p` option and NVFlare's secure mode.

**SwarmMed switches `private_p2p` off by default.** NVFlare's
`SwarmServerController` defaults to `private_p2p=True`. SwarmMed passes
`private_p2p=False` unless `SWARMMEDHUB_PRIVATE_P2P` is set to `1`, `true`, `yes` or
`on` in the environment of the process that submits the job. In that configuration
model updates travel through the coordinating host protected only by the per-hop
TLS, which the relay terminates. The institution operating the FL server can
therefore observe model parameters in transit.

Verified on a running deployment: the FL server container's cumulative network
counters reached 9.8 GB in and 9.4 GB out over the 20-round worked example; the same
container had also served a 2-round smoke test. This is consistent with relaying
every inter-site model transfer. Each round, the aggregating client receives two
remote updates and the new global model is sent to the two other clients, i.e.
about 4 × 94 MB through the server in each direction (≈7.7 GB over 20 rounds,
including the final model broadcast). A server that has only registered clients and
run no training shows ~1 MB.

Consequences for a consortium:

* The coordinating institution is inside the trust boundary for model parameters.
  Choose it accordingly, and say so in the consortium agreement.
* Setting `SWARMMEDHUB_PRIVATE_P2P=true` marks the learn, result and final-model
  tasks secure; SwarmMed's start scripts already enable secure mode
  (`secure_train=true`). With stock NVFlare 2.7.1 the whole payload, weights
  included, is then encrypted to the receiving client's certificate. The MediSwarm
  NVFlare build in the runtime image used for the worked example
  (`jefftud/odelia@sha256:35b600ea…`, package `nvflare_mediswarm`
  2.7.2.dev260427) moves model arrays out of the message into a separate download
  that is not encrypted end to end, so with that image the relay can still read the
  weights. Whether this option protects model parameters therefore depends on the
  NVFlare build inside `SWARMMEDHUB_FLARE_IMAGE`. It has not been validated in a
  multi-site SwarmMed deployment.
* Even where that encryption works, the keys it relies on are widely distributed
  ([Key custody](#key-custody-what-the-startup-package-contains)). It does not hide
  model updates from an FL server operator that also holds the clients' keys, which
  is the case when that institution provisioned the study, as in the worked
  example, or imported the whole package.
* Peer-to-peer topology in the sense used throughout this documentation refers to
  *where computation and aggregation happen*, not to the network path the bytes
  take.

## 4. Adversaries considered

| Adversary | Assumed capability | Status |
|---|---|---|
| **Network attacker** between sites | Observes or tampers with traffic on the wire | Mitigated for FLARE traffic: mutual TLS with a study-specific CA on the server's fed-learn and admin ports, and joining requires a startup kit. **Not mitigated for the web interfaces' site-to-site traffic**: the hub-to-hub calls skip certificate verification by default, and the network token they carry also authorises downloading the latest global model from any participant's web interface (section 1). NVFlare's internal parent–job connection at each site is plain TCP (section 2). |
| **Honest-but-curious participant** | Runs the protocol correctly, inspects everything it legitimately receives | **Partially mitigated.** Every participant receives aggregated model weights each round, and the aggregating client receives other sites' updates. Parameter sharing alone does not prevent model inversion, membership inference or gradient leakage. No differential privacy or secure aggregation is configured by default. Every participant also holds every other participant's keys (section 2). |
| **Honest-but-curious coordinator** | Operates the FL server | **Not mitigated.** Sees relayed model updates (section 3), and holds every participant's private key if it provisioned the study or imported the whole package (section 2). |
| **Malicious participant** | Sends crafted or poisoned updates, or misuses the keys and rights it was given | **Not mitigated.** There is no robust aggregation, update-norm clipping, or anomaly detection, so a participant can degrade or backdoor the joint model. Beyond that, every site's admin certificate is provisioned with NVFlare's `project_admin` role, which the generated authorization policy allows to submit jobs with custom code (`byoc`) to all sites. A malicious participant can therefore run arbitrary code in every other site's client container, which has access to that site's data and uses the host's network, and exfiltrate raw data. SwarmMed offers no setting to provision a narrower role. Admission control is social and cryptographic (who receives the package), not behavioural. |
| **Compromised site host** | Root on a participating machine | **Not contained.** Such a host holds its own patient data, which is the site's responsibility to protect. It also holds the whole startup package, so an attacker who compromises one site obtains every participant's identity, the FL server's key and the `project_admin` right to run code at every other site. |
| **Platform user without rights** | Authenticated user of the web interface | Mitigated: project membership checks, including for actions on a network named by its identifier; role checks for user management and the log viewer; optional TOTP two-factor authentication; a login lockout per username and client address; and an HMAC-chained audit log (section 6 states what it covers and its limits). |

The design target is therefore a consortium of institutions that have a legal
agreement, have vetted one another, and treat each other as honest-but-curious
rather than adversarial. Because every site's admin identity can run code at every
other site, each institution must also trust every other participant, and the
security of their hosts, with access to its own data. SwarmMed is not designed for
open participation by untrusted parties.

## 5. What is not implemented

State these plainly when answering an information-security review:

* **No differential privacy.** NVFlare ships DP filters; SwarmMed neither
  configures nor tests them. There is no privacy budget to report.
* **No homomorphic encryption or secure multi-party aggregation.** Likewise
  available in the NVFlare ecosystem, not wired up here.
* **No robustness to malicious updates** (no trimmed mean, median or Krum).
* **No defence against model inversion or membership inference**, beyond not
  sharing raw data.
* **No per-site startup kits, certificate revocation or choice of admin role.** The
  exported package always contains every participant's identity, a leaked
  certificate cannot be revoked, and every site's admin identity is provisioned as
  `project_admin` (section 2).
* **No verification tool for the audit-log chain** (section 6).
* **No formal verification or third-party audit** of the implementation.

## 6. Data at rest and auditability

* Datasets imported through the interface or the CLI, project code, plots and
  result artefacts (the trained model files and logs synced into the store) are
  stored in MinIO with server-side encryption (SSE-S3, AES-256), and so are the
  copies made when a file is renamed. The key (`MINIO_KMS_SECRET_KEY`) is kept in
  the deployment's `.env` file on the same host. Result artefacts and renamed
  copies written before 9 October 2026 were stored unencrypted and stay so until
  they are written again; SwarmMedHub has no command that re-encrypts them, and no
  bucket-default encryption is configured. Selected database fields are encrypted
  with Fernet.
* Backups are encrypted, and archives are inspected for path traversal and symlinks
  before extraction.
* **What the audit log records.** Logins, logouts and failed logins; data-modifying
  web requests (POST, PUT, PATCH and DELETE, except login paths and requests
  answered with 404); downloads of data files, results and startup packages through
  the web interface; and the operational messages the platform's services write,
  including for work started from the CLI. In addition, every creation or update
  of a platform record, such as a project, network, training job or result, writes
  an automatic entry, except saves that only record a training job's progress.
  Page views are not recorded, and CLI commands appear only through the
  operational messages and these automatic entries. Deletions and changes to
  project membership have no automatic entry: the hooks meant to record them are
  never connected.
* **How far it can be trusted.** Each entry carries an HMAC-SHA256 signature over
  its id, timestamp, user, category, message and the signature of the preceding
  entry. Alteration of those fields can be detected by recomputing the chain with
  the database signing key and the deployment's `SECRET_KEY`, but no verification
  tool is shipped. The log is not append-only: entries are removed by the scheduled
  retention purge (by default, project-category entries, which include logins and
  downloads, after 365 days; data, training, results and network entries after
  2190 days), deleted together with their project or network, and deletable by
  Django-admin superusers. IP addresses are blanked in place after 90 days by
  default; the address is not signed. When an account is deleted, its entries lose
  their user link, which is signed, so they no longer verify. `make deinstall`
  deletes the database volume and with it the whole log.
* Training data read from a local directory (`DATA_DIR`) is **not** managed by the
  platform: disk encryption of that directory is the site's responsibility. Hosting
  it, and the platform's own volumes, on encrypted storage is strongly recommended.

## 7. Recommended deployment posture

1. Run the FL server at the institution the consortium is willing to trust with
   model parameters, and record that decision. If it also provisions the study, as
   in the worked example, it holds every participant's keys as well.
2. Restrict the network path between sites (see [Networking](networking.md)). FLARE
   training requires inbound access only to the coordinating server's two ports,
   which SwarmMed publishes on all of that host's interfaces, as it does the web
   interface's ports 80, 443 and 5085. The web interfaces also try HTTPS connections
   to every participant's port 5085 (status gossip and training-state mirroring);
   blocking them does not stop training but leaves remote participants shown as
   offline. Participating hosts should also firewall NVFlare's internal client
   listener.
3. Keep the study PKI for the lifetime of the study only; re-provision for a new
   study rather than reusing certificates. Handle the startup package as described
   under [Key custody](#key-custody-what-the-startup-package-contains).
4. Give each person their own account, enable two-factor authentication, and
   review the audit log after each training campaign.
5. Treat the joint model as potentially carrying information about every site's
   data when deciding who may download it. Any holder of the network token can
   download it from any participant's web interface.
6. Admit only participants that every other participant trusts to run code on its
   hosts, until non-coordinating sites can be given an admin role without
   `submit_job` and `byoc`.
