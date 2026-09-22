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

During training, a participating site emits exactly three kinds of traffic:

| Category | Content | Size per round (worked example) |
|---|---|---|
| **Model parameters** | The full model weight tensors after local training, and the aggregated weights received back. No gradients are sent in the default configuration: local training runs to completion and the resulting weights are exchanged. | ~94 MB (MST breast-MRI model) |
| **Optimisation metadata** | Round number, local sample count used for weighted averaging, validation metric reported by the training script, task and job identifiers, site name. | < 1 kB |
| **Orchestration messages** | Task assignment and acknowledgement, heartbeats, client registration, run lifecycle events, admin commands. | < 1 kB, continuous |

Raw images, DICOM headers, patient identifiers, labels and local file paths are
never transmitted. Data loading, preprocessing and training happen entirely
inside the site's own container against a local directory.

## 2. Transport and identity

* **Mutual TLS everywhere.** At provisioning, NVFlare issues an X.509 certificate
  to every participant (server, each client, each admin identity) from a project
  root CA generated for that study. Every connection presents a certificate and
  verifies the peer's; an unenrolled host cannot join.
* **Protocol versions and ciphers are negotiated** by gRPC and the platform's
  TLS library; the deployment does not pin a version. In practice this is TLS 1.2
  or higher with the defaults of the installed OpenSSL. Do not quote a specific
  cipher suite in an information-security questionnaire without checking the hosts.
* **Separate internal PKI.** The platform's own services (PostgreSQL, Redis,
  MinIO, the sandbox daemon) use a second, internal CA with mutual TLS; this is
  unrelated to the NVFlare study PKI.
* **Transport confidentiality only.** TLS protects each hop. It does not, by
  itself, keep an intermediary from seeing a payload it relays — see the next
  section.

## 3. The coordinating host sees model updates (default configuration)

This is the most important limitation on this page.

NVFlare's swarm workflow is *client-controlled*: training and aggregation run on
the clients, and the aggregating client rotates each round. However, client cells
connect to the server cell rather than to one another, so client-to-client
messages — including the model payloads — are **relayed through the FL server**.
NVFlare can additionally encrypt such a message end-to-end with the recipient's
certificate, so that the relay cannot read it, but only when the task is marked
secure, which requires the `private_p2p` option.

**SwarmMed leaves `private_p2p` off by default** (`SWARMMEDHUB_PRIVATE_P2P`
unset). In that configuration model updates travel through the coordinating host
protected only by the per-hop TLS, which the relay terminates. The institution
operating the FL server can therefore observe model parameters in transit.

Verified on a running deployment: across the 20-round worked example the FL
server container transferred 9.8 GB in and 9.4 GB out, consistent with relaying
every model update (20 rounds × 3 clients × 94 MB), while a server that has only
registered clients and run no training shows ~1 MB.

Consequences for a consortium:

* The coordinating institution is inside the trust boundary for model parameters.
  Choose it accordingly, and say so in the consortium agreement.
* Setting `SWARMMEDHUB_PRIVATE_P2P=true` marks the learn and result tasks secure,
  which makes NVFlare encrypt the payload to the receiving client's certificate so
  the relay cannot read it. This option is available but is **not yet validated in
  a multi-site SwarmMed deployment**; validate it in a pilot before relying on it.
* Peer-to-peer topology in the sense used throughout this documentation refers to
  *where computation and aggregation happen*, not to the network path the bytes take.

## 4. Adversaries considered

| Adversary | Assumed capability | Status |
|---|---|---|
| **Network attacker** between sites | Observes or tampers with traffic on the wire | Mitigated: mutual TLS with a study-specific CA; joining requires an issued certificate. |
| **Honest-but-curious participant** | Runs the protocol correctly, inspects everything it legitimately receives | **Partially mitigated.** Every participant receives aggregated model weights each round, and the aggregating client receives other sites' updates. Parameter sharing alone does not prevent model inversion, membership inference or gradient leakage. No differential privacy or secure aggregation is configured by default. |
| **Honest-but-curious coordinator** | Operates the FL server | **Not mitigated by default** — sees relayed model updates (section 3). |
| **Malicious participant** | Sends crafted or poisoned updates | **Not mitigated.** There is no robust aggregation, update-norm clipping, or anomaly detection. A participant can degrade or backdoor the joint model. Admission control is social and cryptographic (who receives a certificate), not behavioural. |
| **Compromised site host** | Root on a participating machine | Out of scope. Such a host holds its own patient data; protecting it is the site's responsibility. |
| **Platform user without rights** | Authenticated user of the web interface | Mitigated: role-based access control, optional TOTP two-factor authentication, brute-force lockout, tamper-evident audit log. |

The design target is therefore a consortium of institutions that have a legal
agreement, have vetted one another, and treat each other as honest-but-curious
rather than adversarial. SwarmMed is not designed for open participation by
untrusted parties.

## 5. What is not implemented

State these plainly when answering an information-security review:

* **No differential privacy.** NVFlare ships DP filters; SwarmMed neither
  configures nor tests them. There is no privacy budget to report.
* **No homomorphic encryption or secure multi-party aggregation.** Likewise
  available in the NVFlare ecosystem, not wired up here.
* **No robustness to malicious updates** (no trimmed mean, median or Krum).
* **No defence against model inversion or membership inference**, beyond not
  sharing raw data.
* **No formal verification or third-party audit** of the implementation.

## 6. Data at rest and auditability

* Objects in the platform's store (datasets registered through the interface,
  result artefacts) are encrypted server-side with AES-256; selected database
  fields are encrypted with Fernet.
* Backups are encrypted, and archives are inspected for path traversal and
  symlinks before extraction.
* All user actions are written to an append-only audit log whose entries are
  chained with HMAC-SHA256, so tampering is detectable.
* Site-local training data is **not** managed by the platform: disk encryption of
  the data directory is the site's responsibility. Hosting it on an encrypted
  volume is strongly recommended.

## 7. Recommended deployment posture

1. Run the FL server at the institution the consortium is willing to trust with
   model parameters, and record that decision.
2. Restrict the network path between sites (see [Networking](networking.md));
   the platform does not require inbound access to a site other than to the
   coordinating server's two ports.
3. Keep the study PKI for the lifetime of the study only; re-provision for a new
   study rather than reusing certificates.
4. Give each person their own account, enable two-factor authentication, and
   review the audit log after each training campaign.
5. Treat the joint model as potentially carrying information about every site's
   data when deciding who may download it.
