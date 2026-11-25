# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.6] - 2026-11-DD

### Changed

- `swarmed training checkpoints` and `swarmed training resume` work at any
  training site, the standby included, whether or not the site's Hub holds a
  record of the interrupted job
  ([Server failover](docs/failover.md#resuming-the-run)). They find the job by
  this Hub's record, else by the job's folder in the network's workspace, else by
  the job that a checkpoint given with `--checkpoint` names. A copied round-start
  checkpoint names its own job and round, also one written by 1.0.3 to 1.0.5, so
  it needs no `--start-round`; a checkpoint of another job or network is refused.
  `training resume --checkpoint FILE` without a job and without `--network`
  takes the network from the checkpoint, else from the one network at this Hub
  that holds the job's folders, and otherwise asks for `--network`; only a model
  file that names no job is resumed in the current network. Without a job, both
  commands take the job of the newest round-start checkpoint at this site, or the
  Hub's latest job if there is none, and stop when the Hub's latest record of a
  job names another one; records of failed submissions, which name no FLARE job,
  do not count. When the Hub had no record of the interrupted job, the resume
  records it as failed once its checks have passed, and says so in a warning.
  `training checkpoints --json` also gives `network_identifier`, `found_by`,
  `total_rounds` and `total_rounds_from`, gives `job_identifier` as `null` when
  the Hub has no record, and gives each checkpoint's `job_id`, `network` and
  `total_rounds`. The help of both commands says that the job is given by its
  FLARE job ID, which is the same at every site, or by this Hub's record of it;
  the record IDs that `training list` shows differ from Hub to Hub.
- Every job records the run's total number of rounds in each site's app
  (`config/swarmmed_run.json`), and its round-start checkpoints hold it too
  ([Server failover](docs/failover.md#what-swarmmed-provides)).
  `swarmed training resume` takes the total from `--rounds`, else from the job's
  own files at this Hub, else from the checkpoint, and says where it took it
  from. It no longer takes a record's total or this Hub's
  `SWARMMEDHUB_SWARM_ROUNDS` or `SWARM_ROUNDS`, and stops with a message that
  names `--rounds` when it finds no total. A run started before 1.0.6 therefore
  needs `--rounds` when it is resumed at a Hub that did not run its FL server.
  A `--rounds` that differs from the job's total is used, with a warning. Upgrade
  every Hub of a network before relying on a resume without `--rounds`: a Hub
  that runs an earlier version neither writes the file into the jobs it submits
  nor reads it.
- `swarmed training resume` refuses while the network's current FL server runs
  any other job, not only the interrupted one, so that a run resumed at one site
  is not resumed again at another; `--force` skips the check, as its help now
  says ([Server failover](docs/failover.md#resuming-the-run)).
- A site's Hub records the jobs that its own FL client runs from the logs of the
  FL containers it started for the network, which carry the label
  `swarmmedhub.network_id`, also when it does not find the address it was
  provisioned with among its own
  ([Networking](docs/networking.md#how-a-hub-knows-which-site-it-is)). It used
  to pick the containers only by the Hub's addresses and, without one, read the
  logs of every container on the Docker host whose name contains "server",
  "client" or "nvflare", including other deployments' containers on a shared
  host. Containers without the label are no longer read, and each container's
  job is recorded only in its own network.
- Every FL server that SwarmMed starts, a restarted one or a standby, begins with
  a new FLARE job store
  ([Server failover](docs/failover.md#what-happens-when-the-server-is-lost)).
  Earlier versions kept the store in the server kit's `.nvflare_persist` folder
  only where the Hub's worker saw the checkout at the host's path, which a
  standard installation does not; that folder is no longer used and can be
  deleted. A restarted server therefore never lists the interrupted job and never
  dispatches a job that was still queued when it stopped, and the resume after a
  restart in place needs no `--force`.
- `swarmed network start` does not start this Hub's FL server while the Hub is
  switched to a server that another site started, and outside client-only mode
  no FL client either: the network then shows `ERROR`, and its log says why and
  points to the failover guide's return to the coordinating server
  ([Server failover](docs/failover.md#when-the-hub-that-ran-the-server-comes-back)).
  While the Hub is switched, `network start --wait` names the server the Hub
  uses as a possible cause. `swarmed network failover --start-server` and starts
  in client-only mode are not held back. At a Hub whose own FL clients still
  run, a refused start sets `ERROR` too, so the Hub no longer discovers new jobs
  and refuses `training start` and `resume` until `swarmed network failover`
  restarts its clients, which the guide says to do only once the run has
  finished.
- [Server failover](docs/failover.md#testing-status) describes the failover
  rehearsed on 23 November 2026 with SwarmMed 1.0.5 on two lab machines in one
  institution: during round 3 of a 40-round run of the ECG template on CPU, the
  server was moved to the standby, the run was resumed at round 3 with one round
  repeated and none lost, and the server was moved back. It names what the
  rehearsal needed with 1.0.5 (a Celery worker running as root, the resume at a
  site with `SWARMMEDHUB_LOCAL_ADDRESSES` instead of the standby, and `--rounds`)
  and what it does not show, and no longer says that a failover across hosts has
  not been carried out. It also describes the rehearsal repeated on 24 November
  2026 with 1.0.6, which needed none of these: the standby itself resumed the run
  without `--rounds`, and the run completed once it had been resumed a second
  time, after a race in NVIDIA FLARE 2.7.1 had ended the first resumed job.
  [Networking](docs/networking.md#validation-status) lists the machines' own
  addresses, without an overlay, as used in both rehearsals within one
  institution.
- [Server failover](docs/failover.md#before-a-study-choose-the-standby-site) asks
  for `SWARMMEDHUB_LOCAL_PARTICIPANT` for both `app` and `celery_worker` at the
  standby and every training site, for the coordinating server's image settings
  at the standby, and for a check during the first run that the standby lists the
  job and a round-start checkpoint of it. It describes what to do when the Hub
  that ran the server comes back while the standby serves (leave its server down
  and switch it to the standby with `swarmed network failover`), checks before a
  restart in place that no standby has taken over, and says that a resumed job
  chooses its best model among its own rounds only.
- [Networking](docs/networking.md#how-a-hub-knows-which-site-it-is) explains what
  `SWARMMEDHUB_LOCAL_PARTICIPANT` and a Hub's own addresses
  (`SWARMMEDHUB_LOCAL_ADDRESSES`, NetBird, Tailscale) are each used for. They were
  presented as alternatives; they are not. The client-only example, and the
  usage guide, set the site's name for the `app` service too, so that the
  import's warning about another site's kit applies.
- [Security](docs/security.md#script-sandbox) and
  [Installation](docs/installation.md#software-and-network-access) say that the
  privileged script sandbox, `sandbox-dind`, can load modules into the host's
  kernel, and that its Docker daemon tries to load `bridge` and `br_netfilter`
  when it starts.
- The [threat model](docs/threat-model.md#key-custody-what-the-startup-package-contains)
  says that the FL server's log names the same admin identity for every Hub, and
  that deleting a network can leave files that the FL containers wrote as root,
  keys included, on disk unless the Celery worker is restarted first. The
  [installation guide](docs/installation.md#build-and-run) says that the
  PgBouncer container keeps restarting on a new installation, which does not
  affect the Hub, and the [CLI guide](docs/cli.md#5-create-or-import-a-network)
  says to export the startup package before the network first starts, since a
  later export also carries the job folders of the sites that trained at that
  Hub.

### Fixed

- FL server and client starts no longer fail with `Permission denied` when
  `HOST_PROJECT_PATH` lies under a folder that the Celery worker cannot enter,
  such as `/root`. This affected `swarmed network start`, `network failover` and
  `network failover --start-server`: for clients since 1.0.1, and for the server
  since 1.0.4 when `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` is set. The worker no longer
  has to run as root ([Server failover](docs/failover.md#testing-status)).
- A network imported with `swarmed network import`, which runs as root, could
  not be started, switched to another FL server or used as the standby with the
  default Celery worker, which runs as `appuser`: the import left the network's
  folder owned by root and its start scripts not executable. The import now
  hands the folder to `appuser`, also when it fails, and keeps the execute bits
  that the package records ([CLI](docs/cli.md#running-the-cli)). A network
  imported by an earlier version is repaired when the worker restarts, which the
  upgrade does.
- A network created or imported with the CLI as its project's first network at a
  Hub left the project's folder in `workspaces/` owned by root, so that the web
  interface could not add another network to the project. When the worker cannot
  make a start script executable, the error now names the file, its owner and
  `docker compose restart celery_worker`.
- `swarmed training status`, `watch`, `stop`, `checkpoints` and `resume` take a
  job's record identifier, its FLARE job ID, or the first 8 or more characters
  of its FLARE job ID if they name exactly one job; `checkpoints` and `resume`
  also count the job folders in the network's workspace
  ([CLI](docs/cli.md#7-start-and-watch-training)). Anything else names no job:
  the command exits with code 2 and says that the job was not found, or lists the
  jobs that the characters match. Version 1.0.5 failed with
  `Unexpected failure: ... is not a valid UUID` for anything but a whole UUID.
  `training stop <JOB>`, and `training status <JOB>` with `--network`, of a job
  that is not found now exit with code 2; version 1.0.5 stopped the network's
  newest running job, or showed its latest job, instead.
- `swarmed training stop` and **Stop Training** on the Training page record a
  job as stopped only when the FL server took the abort or answered that the job
  had already finished ([CLI](docs/cli.md#7-start-and-watch-training)). When the
  server refuses the abort, for example for a job that it does not know or that
  it lists as running but does not run, or when it does not answer, they show
  the server's answer and leave the job's record as it was; the command exits
  with code 2. A job whose record had already ended keeps its status instead of
  becoming stopped. Version 1.0.5 recorded the job as stopped whatever the
  server answered. When the Hub cannot reach the server at all, the command
  still fails with `Unexpected failure` and exit code 1, and leaves the record
  as it was.
- `swarmed training resume --rounds` must be between 1 and 100000: 0 was
  ignored, and a value too large for the job's record was submitted to every
  site before saving the record failed with `integer out of range`. A total
  above 100000 in a job's files or checkpoints, which code in the FL client
  containers can write, counts as unknown.
- A round-start checkpoint's JSON, or a job's run file, nested too deeply to
  read counts as no data, instead of failing `training checkpoints` and
  `training resume` with `Unexpected failure`.
- `swarmed training stop`, and `swarmed training start` or `resume` after a
  failed submission, no longer hang after their output: the Hub closes its FLARE
  admin session in every case. The job monitor and the Training page's status
  check also close theirs when a command fails.
- A site's Hub stored 10 as the number of rounds of a job it learned of from its
  FL client's logs, and the job monitor and the progress display fell back to 10,
  or to the rounds in another job's server settings, when a record had no total.
  `swarmed training resume` used that number unless `--rounds` was given, so a
  resumed 40-round run would have stopped at round 10. The Hub no longer stores a
  total it does not know, shows no percentage and no ETA without one, and no
  longer replaces its own total with one that another Hub reports. Records
  written by earlier versions keep their numbers.
- The job monitor runs one pass at a time
  ([Server failover](docs/failover.md#when-a-site-drops-out)). Passes that took
  longer than the minute between them, for example while an FL server was down,
  overlapped and could upload a finished job's results twice.
- `swarmed network status`, the Network page and the status that a Hub sends to
  the other Hubs showed the clients of a stopped FL server as joined: they were
  read from the stopped container's log, for example at a coordinating Hub that
  came back while the standby served. The Hub now reads that log only while the
  server's container runs.
- A training job asks its own Hub for its data manifest at the host's loopback
  address before the Docker gateway address, which the Hub's host check refused
  with an error and a traceback in its log at every job start.
- A training job started or resumed with the CLI left its folder owned by root,
  so the web interface could not start the network's next job until the
  containers restarted. The job's folder and `resumed_jobs.json` are now handed
  to `appuser`, also after a failed submission, and what cannot be handed over
  is named in a warning in the training log.
- A job that ended while `swarmed training watch`, `training status` or the
  Training page was open kept the round count of the job monitor's last pass,
  for example 17 of 20 rounds. `training status --json` also gives
  `rounds_finished`.
- The round count that NVIDIA FLARE logs at the end of a job ("number of rounds
  completed") was read as a round number, so the aggregating site reported one
  round more than the others, for example 5 for a job with rounds 0 to 4.
- A Hub that cannot tell which participant it is in a running network says so in
  its log once every six hours, instead of logging a warning every 30 s.
- After a return to the coordinating server, `swarmed network status` shows
  `switched` as `false` at every Hub, and the switch log no longer calls the
  coordinating site's own server a standby or says that it switched from an
  address to the same address. The switch record is kept
  ([Server failover](docs/failover.md#returning-to-the-coordinating-server)).
- `docs/failover.md` said that a restarted FL server marks the interrupted job
  as abandoned. A server that SwarmMed starts has a new job store and does not
  list the job; only a server container brought back by hand with its old store
  marks it. The guide now says to start a server again only with SwarmMed's
  commands, since after a bare `docker restart` NVIDIA FLARE refuses to start.
  `docs/runtime-backend.md` said that SwarmMed mounts FLARE's state directory
  for every server.
- `docs/failover.md` said that any training site can resume a run. With 1.0.5
  only a site whose Hub held a record of the job could, which a Hub that did not
  find its provisioned address among its own never had; with 1.0.6 any training
  site can. It also said that the resume gives the admin session 20 s to log in
  to each server: a server whose admin port does not accept a connection within
  1 s gets 5 s.
- `docs/networking.md` said that the FL client's job processes connect to their
  parent process at the site's provisioned address. They connect to
  `localhost`; FLARE does not use that address. It also said that the
  coordinating Hub shows client status from its FL server's log only if it
  recognises its own address. That holds for the Network page;
  `swarmed network status` reads the log of the network's FL server container at
  the Hub while that container runs.
- `docs/networking.md`, `docs/threat-model.md` and `docs/security.md` said that
  the generated web certificate names only `localhost` and `127.0.0.1`. Since
  1.0.1 it also names `swarmmedhub`, `host.docker.internal` and `172.17.0.1`; it
  names no site's address.

### Security

- The [threat model](docs/threat-model.md#what-job-code-can-reach-at-a-site) and
  the [security page](docs/security.md#fl-runtime-containers) state what job code
  can reach in an FL container of a standard installation, which 1.0.6 does not
  change: every volume of the Hub's application container, including its `.env`
  and `.secrets/` and every network's kits and keys at that Hub, as root; the
  data adapter loads that `.env` into the training process; and a client
  container on the host's network can reach the Hub's unauthenticated Docker API
  proxy. The failover rehearsal with this version showed that reach with a
  read-only request; that job code there can also create and start containers
  through the proxy, and so take over the host, follows from the proxy's
  settings and was not tried.
- Training containers ask only their own Hub for their data manifest
  ([Networking](docs/networking.md#traffic-between-hubs)). They asked the FL
  server's host first, after a server switch the standby site's host; the
  request carried the project's manifest secret, which a host with another
  installation's certificate did not receive. On the host's network the data
  adapter no longer falls back to plain HTTP, whose host names went to the
  site's DNS, also not in a container that an earlier version started; an
  adapter that cannot tell which network its container is on uses no plain
  HTTP. The adapter comes with the job, so this holds for jobs submitted by a Hub
  of this version.
- `swarmed network import` refuses, and writes nothing, when the project's or
  the network's folder in `workspaces/` is a symbolic link, and the hand-over of
  an imported network to the Celery worker never follows a link
  ([CLI](docs/cli.md#5-create-or-import-a-network)). The import runs as root, so
  a link planted there made it write the package wherever the link pointed.
- The [threat model](docs/threat-model.md#key-custody-what-the-startup-package-contains)
  says that the private keys have mode 644 at every Hub that holds the network,
  the importing sites included, and that the files in `workspaces/` belong to
  user ID 1000 on the host. It and the
  [operating systems](docs/os-support.md#container-runtime-features-across-operating-systems)
  page no longer say that the FL client's internal listener binds to
  `localhost`: with NVIDIA FLARE 2.7.1 it accepts connections on all IPv4
  addresses, with host networking the host's, as the networking guide and the
  security page already said.

## [1.0.5] - 2026-11-23

### Added

- A network fingerprint, so that every site can check that it received the same
  startup package
  ([Networking](docs/networking.md#verifying-the-startup-package)). The export
  writes `manifest.json` into the package and the identical file into every site's
  kit: the SHA-256 of every other file in the package, and a summary of what the
  sites share (the SHA-256 fingerprints of the root CA and FL server certificates
  in OpenSSL's format, the FL server's address as the kits' `server_host.txt` give
  it, the FL and admin ports from the signed `fed_server.json`, and the
  participants with their addresses and certificate fingerprints). The network
  fingerprint is the SHA-256 of `manifest.json`, which is canonical JSON and holds
  nothing that depends on when or where it is exported (no time stamp, no name of
  the exporting Hub, no SwarmMed version), so the same network exported twice has
  the same fingerprint. An export whose kits name different FL server addresses,
  or another address than the network's SERVER participant, is refused.
- An import checks the package before it writes anything: every file that the
  manifest lists must be there with that SHA-256, no other file may be there,
  every site's kit must hold the same manifest, and the manifest's summary must
  agree with the files it sums up. A file that the manifest does not list is
  refused before its content is hashed; a `.zip` at the top of the package is
  unpacked first, within the limits, to check its entries. A single site's kit is
  checked against the manifest's entries for that kit and shows the same
  fingerprint; the import names the kit's site, and warns, without refusing the
  kit, when `SWARMMEDHUB_LOCAL_PARTICIPANT` names another site. A refused import
  names the first offending file, with control characters escaped, and writes
  nothing.
- `swarmed network import --expect-fingerprint VALUE` and the import form's
  **Expected fingerprint** refuse a package with another fingerprint, or without
  one; spaces, colons, dashes and case are ignored. An empty `--expect-fingerprint`
  is an error, not a skipped check, so that a script that passes an unset variable
  does not import an unchecked package; an empty form field means no expected
  fingerprint.
- The fingerprint, as 16 groups of four characters: in the message after a web
  export or import and on the Network page, with the package's SHA-256 and the
  root CA certificate's fingerprint (after a download with **Kits**, both show
  once the page is reloaded); and in the text output of
  `swarmed network export-package`, `import`, `show` and `status`.
  `export-package --json` gives `package_fingerprint` and `package_sha256` (of the
  file written); `show`, `status`, `list` and `import` give `package_fingerprint`,
  `package_sha256` and `root_ca_sha256`, and `import` also `package_site`, the site
  of a kit imported on its own. A web export writes the fingerprint and the
  package's SHA-256 to the audit log.
- `SwarmNetwork.package_fingerprint` and `SwarmNetwork.package_sha256` (migration
  `network/0004`). Both have a database default, so that code without them, such
  as 1.0.4, can still create networks after the migration. Each Hub stores the
  values of the package it last exported or imported.
- `swarmed network status` and `swarmed network failover` list the addresses that
  the Hub's admin session, which submits and monitors jobs, tries for the network,
  in order (`fl_server.admin_session_hosts`). After a server switch that is the
  recorded server alone, except at the standby that started it.
- [Server failover](docs/failover.md#restarting-the-server-where-it-ran) now
  describes the simplest recovery first: when the FL server's process or container
  has stopped but its host and the host's network are fine, stop and start the
  network at the coordinating Hub the same way as it was started, with the same
  image settings and `SWARMMEDHUB_SERVER_ONLY_MODE`, and resume the run. The sites
  need do nothing: their FL clients keep trying to reach the server and are taken
  back on the tokens it signed before. In a test across three hospitals, the
  clients reconnected to a restarted server without any action at the sites. A
  server whose FLARE build keeps listing the interrupted job as running after the
  restart, as the one in MediSwarm 1.9.3's image does, needs `--force` for the
  resume. A server that runs at a standby site after a switch is restarted there
  with the command that started it (`swarmed network failover ... --start-server`),
  since `swarmed network start` at a site in client-only mode starts only its
  clients. The standby server is for the loss of the host or its network. The
  guide also names moving the server's IPv4 address to another machine at the
  coordinating institution, for example as a floating IP, as an option that needs
  no switch at the sites but has not been tested: the run still has to be resumed,
  and the old machine must not come back with the same address. The CLI guide
  points to the restart, and the failover guide lists
  `SWARMMEDHUB_FLARE_CONNECT_TIMEOUT`.

### Changed

- Exporting the same network twice gives the same file, as long as its kits have
  not changed: entries are sorted and get the time stamp 1980-01-01 00:00 and
  mode 644, or 755 for files that their owner may execute, and the participants
  are listed by role and name.
- An import refuses entries that are symbolic links or whose path is absolute or
  has a `..`, `.` or empty component, in the package or in any copy of a site's
  kit in it, and entries in a folder named like a site's kit, which it would take
  for that kit, whether or not the package has a manifest. It used to skip only
  entries that resolved outside the network's folder, and wrote symbolic links as
  regular files. A file that is not a ZIP file, or has an entry that cannot be
  read, is refused before a network record is created.
- An import reads the package into memory and refuses a package of more than
  1 GiB, as a file or unpacked, or with a file of more than 256 MiB, before it
  reads the files in it; the sizes are those the package declares, and the import
  unpacks no more of a file than that, a chunk at a time. It also refuses entries
  compressed with another method than deflate, unless stored, since Python's
  zipfile decompresses bzip2 and LZMA data whole, and a package with more than
  100,000 entries, counting those of its site kits, before zipfile reads their
  list. A package exported before its network first runs is far smaller.
- A package exported by SwarmMed 1.0.4 or earlier, which has no manifest, is
  imported as before, with a warning that it has no fingerprint and should be
  verified by other means. A package that SwarmMed 1.0.4 or earlier exported
  again, from a network imported from a later version, carries a manifest in its
  kits but not at the top and is refused, with a message that names this cause;
  import the package that the coordinating site exported.
- A Hub that imported a package with `SWARMMEDHUB_SERVER_HOST` set to another
  address than the packaged one can no longer export it, as 1.0.4 could: its kits
  name another FL server than the network's SERVER participant. The message says
  so and to export the package at the coordinating site.
- The import keeps the package's `manifest.json` in the network's folder, out of
  the sites' FLARE workspaces.
- The Network page shows the outcome of an export, an import or a deletion, which
  it did not show before; a web export that fails shows the reason there instead
  of an error page.
- `swarmed training resume` refuses, unless `--force` is given, when it cannot ask
  the network's FL server whether the interrupted job is still active, and says
  why; it used to go ahead without a word. Its admin session gets as long to log
  in to each server it asks as a job submission does (20 s, or
  `SWARMMEDHUB_FLARE_CONNECT_TIMEOUT`, instead of 10 s), and the server as long
  again to answer `list_jobs` (instead of the admin API's 5 s). It refuses a
  network that the Hub does not run before it asks any server.
- After a server switch, `swarmed training resume` also asks the server the Hub
  used before: `previous_server_host` in `prod_00/.active_server.json` or, for a
  record without it, the address in `SWARMMEDHUB_SERVER_HOST` or the kits'
  `server_host.txt`. It refuses while that server still runs the job, and resumes
  with a warning (a `warning:` line, with `--json` in `warnings`, and in the Hub's
  log) to keep that server down when it cannot be reached. `--force` skips both
  checks. Without a switch only the current server is asked, as before.
- A switch record names `SWARMMEDHUB_SERVER_HOST` as the previous server when the
  Hub used that address before the switch, and switching again to the same server
  keeps the server used before it instead of naming the same server as previous.
  When no switch is recorded, `fl_server.server_host` in `swarmed network status`
  names `SWARMMEDHUB_SERVER_HOST` as set where the CLI runs (the `app` container),
  whose admin session uses it too; the FL clients use the variable as set for the
  Celery worker.

### Fixed

- Startup packages now include `project.yml` and `runtime_requirements.txt`. The
  export looked for them in `<network>/workspace/`, but provisioning writes them to
  `<network>/`. With `project.yml`, a participating site's Hub recognises its own
  site by the host's address, as [Networking](docs/networking.md) describes, also
  without `SWARMMEDHUB_LOCAL_PARTICIPANT`.
- `swarmed network export-package` at a Hub that imported a single site's kit
  wrote a package without any kit. It now exits with an error that says to export
  the package at the coordinating site, and leaves the stored fingerprint as it
  was.
- An import of a package that had a site's kit as a folder named like it, or,
  without a manifest, an entry that could not be read (a wrong CRC-32, a local
  header that names another file), failed after it had created the network
  record: the record and part of the files stayed, the CLI reported an unexpected
  failure and the web form an error page. Such a package is now refused before
  anything is written.
- Importing a network that already exists at the Hub failed with a database error
  after the package had been checked. It is now refused before anything is
  written; delete the network first to import its package again, for example to
  get the fingerprint of a network imported from a package of SwarmMed 1.0.4 or
  earlier.
- A manifest that agreed with its package could list a site's kit as one file, or
  a ZIP archive at the top of a single site's kit. The import then unpacked it as
  a site's kit without checking it, and failed after creating the network record
  when one of its entries could not be read. Such a package, whose fingerprint
  differs from the coordinating site's, is now refused before anything is
  written.
- Deleting a network queues the removal of its containers and folder. A network
  imported again before that ran lost its newly imported kits; the removal now
  leaves a network that exists again alone. Import a network's package again only
  once its old folder is gone.
- NVIDIA FLARE's verifier (`python -m nvflare.tool.verify_startup_kits`) passes on
  the `startup` folders of newly provisioned kits. FLARE signs each kit's `startup`
  folder before SwarmMed adds `.all_participants.json`, `server_host.txt` and
  `server_aliases.txt`, so the verifier rejected every kit; provisioning now signs
  those folders again with the root CA's key, with FLARE's own helper. FLARE's
  start-up check in secure mode still passes. An import with
  `SWARMMEDHUB_SERVER_HOST` set to another address than the packaged one changes
  `server_host.txt`, after which the verifier fails for those kits.
- The threat model no longer says that a site's `startup` folder holds a second key
  pair: since 1.0.1, clients are provisioned without a listening address, and
  NVFlare creates that pair only for one.
- After `swarmed network failover`, a Hub's admin session could submit and monitor
  jobs on a stale FL server at that Hub while `swarmed network status` showed the
  standby. The session tried the kits' server name `server` first, which inside
  the Hub's containers reaches an FL server container of the network at the Hub,
  such as the coordinator's own server started again or one that a site started
  without client-only mode, and when the standby did not answer it fell back to
  the network's server container, the Docker gateway, `host.docker.internal`,
  `127.0.0.1` and `localhost`. With a switch record the session now connects only
  to the recorded server, and the probes logged before a submission reach only
  that server, except at the standby, which runs the server and keeps its local
  addresses (`server_started_here`, which records since 1.0.3 carry, so records
  written by 1.0.3 and 1.0.4 are read as they are). Without a switch the order is
  unchanged.
- After a failover, `swarmed training resume` could start a second job while the
  old server still ran the interrupted one: it asked only the server the admin
  session reached, which after a switch is the standby, whose job store starts
  empty. It now also asks the previous server (see Changed).
- `swarmed training resume` never saw that an FL server still ran the interrupted
  job. It looked for the job list in the data of NVIDIA FLARE's reply to
  `list_jobs`, which holds a table for display, while FLARE 2.7.1 gives the list
  in the reply's meta. The refusal while the server still runs the job, there
  since 1.0.3, therefore never applied, and a reply without a job list, such as
  FLARE's "Server did not respond" when the server did not answer within the
  admin API's 5 s, counted as a server that no longer runs the job. The check now
  reads the job list from the reply's meta and takes a reply without one as a
  server that it could not ask.
- `swarmed network failover --wait` returned only once the network was running or
  had failed. A Hub without client containers keeps a stopped or provisioned
  network's status, so there the command waited until `--timeout`, or for ever
  without one. It now returns once the switch is recorded.
- `swarmed network failover --server-host` accepted IPv4 addresses with leading
  zeros, such as `010.0.0.1`, which the C library reads as octal and so not as
  `10.0.0.1`, and digits that are not ASCII. It now refuses both, as it does for a
  new network's server address.
- `docs/failover.md` said that after a switch the Hub's job submission and status
  polling use the new address; in 1.0.3 and 1.0.4 they tried a local server of the
  network first. It now describes the admin session as it is.
- `docs/failover.md` now says that the standby needs the network's whole startup
  package, since a Hub that imported only its own site's kit cannot start the
  server, and that `swarmed training resume` leaves an interrupted job that has
  already ended as it is (`swarmed network stop` marks it as stopped) instead of
  marking it as failed.
- `docs/networking.md` said to set `SWARMMEDHUB_SERVER_HOST` for the Celery worker
  when the coordinator's address changes. The `app` container, where the CLI and
  the web interface submit jobs and `swarmed network status` reports the address,
  needs it as well.

## [1.0.4] - 2026-11-19

### Added

- A warning in SwarmMedHub and the CLI when a running training job has stalled
  ([Server failover](docs/failover.md#when-a-site-drops-out)). The job monitor
  flags a running job when the FL server reports a site as dropped out (its
  training job as missing in reports that span a minute, or its FL client as dead
  or disconnected) and the site has not taken part in the job since, which only
  the Hub that runs the FL server can read in the server's logs, or when no round
  has finished for `SWARMMEDHUB_STALL_WARNING_MINUTES` (default 60; `0` turns this
  check off; an invalid value means 60 and a warning in the log). The warning names
  the site and what to do: check the site, then stop the job and resume it
  (`swarmed training resume`). It does not change the job's status. A warning
  about a site disappears when the site takes part in the job again, one about
  rounds when a round finishes; both disappear when the job ends.
  `.env.template` sets the variable to 60.
- The warning appears under the progress bar on the Training page, as a "Stalled"
  badge on the dashboard, in the admin's list and page of training jobs, on the
  Logs page, and in `swarmed training status`, `list` and `watch` (a `warning:`
  line; with `--json` in `warnings` and in the job's `stall_warning`). The
  training status API and the CLI's job output also give `last_round_at`, the time
  a round last finished or the job started running. The training-state endpoint
  that Hubs read from each other gives both fields; the Hubs that read it do not
  show them yet.
- `TrainingJob.last_round_at` and `TrainingJob.stall_warning` (migration
  `training/0003`). `stall_warning` has a database default, so that code without
  the field, such as 1.0.3, can still create training jobs after the migration.
- `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` (seconds) sets the FL server's
  `admin_timeout`: how long the server waits for each site's reply, for example
  when it deploys a job (10 s as provisioned). Before SwarmMed starts an FL
  server, the standby server included, it writes the value into the server's
  `local/resources.json`, which FLARE reads before `resources.json.default`. The
  file is made from itself, from `resources.json.default` or from the
  provisioning defaults, and keeps every other setting. Set the variable for the
  process that starts the FL server. For a start from the CLI or the web
  interface, that is the Hub's Celery worker: put the variable in
  `docker-compose.override.yml` under `celery_worker`, not in `.env`, from which
  `make env` drops variables that `.env.template` does not list. It applies from
  the next start of the server. Start the server the same way as before, or it
  can come back in another image with other time limits
  ([Server failover](docs/failover.md#resuming-over-a-slow-link)).
- `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` (whole seconds) sets how long the FL
  server waits for the starting site to start the swarm workflow
  (`start_task_timeout`, 10 s in NVIDIA FLARE 2.7.1). Like the other time limits,
  it is read by the process that submits the job.
- `swarmed training resume` warns that a resumed job ships its checkpoint and,
  should the job fail over a slow link, names the two settings to change before
  resuming the interrupted job again. The warning is left out when
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` is set in the environment of the command
  itself.
- `experiments/duke_mst_cli/explain/run_explainability.py` takes `--split` and
  `--site-institution`, so that a training site can explain cases of its own share,
  for example its validation cases, with the global model it holds
  (`--site-institution node_A --split val --cohorts site`). These cases have no
  published AUROC, so the AUROC check is skipped with a warning. A test across
  three hospitals used these options to explain each site's validation cases. The
  script checks the cohorts before it loads the model (`site` needs
  `--site-institution`), and stops with a message that names the split when a
  cohort has no cases in it.

### Changed

- A resumed job waits 600 s for the swarm workflow to start instead of the default
  of the FL server's NVIDIA FLARE build. That is longer than NVIDIA FLARE 2.7.1's
  10 s but shorter than the 360 000 s of the worked example's image; set
  `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` to keep a longer limit. The FL server
  also checks a resumed job's status reports every 10 s instead of every 2 s
  (`job_status_check_interval`), so that after a long start every site has
  reported before the first check: the status-report limit (90 s in NVIDIA FLARE
  2.7.1) counts from the start of the job, a site reports only once its first
  learn task has started, and in a test across three hospitals the starting
  site's first report arrived 0.26 s before FLARE's default first check, 2 s after
  the start.
- `docs/nvflare-comparison.md` lists `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` and
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` with the other time limits and states that the
  defaults of the workflow's deadlines come from the NVIDIA FLARE build in the
  image the FL server runs in: 360 000 s (100 h) for the status-report and
  progress limits in the worked example's image, and NVIDIA FLARE 2.7.1's 90 s and
  3 600 s in the image SwarmMed builds.
- `swarmed training watch` reads the job again before every poll, so that it shows
  what the worker's job monitor records, the stall warning included.
- The breast-MRI worked example's scripts (`evaluate_global_model.py`,
  `explain/run_explainability.py`, `explain/recompute_cam.py` and
  `explain/render_from_npy.py`) default to `/mnt/data/DUKE_iid` and
  `/mnt/data/medswarmdata` instead of folders of the host they were first run on,
  and `run_explainability.py --ref-json-dir` defaults to
  `results/duke_mst_cli/repetition_1` in the working directory.
  `experiments/duke_mst_cli/explain/README.md` describes the inputs with
  placeholders. Tests use an address from the documentation range (`192.0.2.10`)
  for example hosts.

### Fixed

- Resuming a run could fail over a slow link, because a resumed job carries its
  checkpoint (94 MB for the breast MRI model). In a test across three hospitals,
  the starting site did not accept the job within the FL server's 10 s
  `admin_timeout`, so the job started without it and failed (`invalid value ...
  in 'participating_clients'`); 300 s fixed this. The server, restarted through
  the CLI for that setting, now ran in SwarmMed's own image with NVIDIA FLARE
  2.7.1's limits (the worked example's image waits 360 000 s), and the workflow
  did not start within its 10 s `start_task_timeout`: the starting site confirms
  the start only once it has sent the first learn task to the other sites, and in
  a resumed run that task carries the checkpoint (110 s there; new runs take 1 to
  2.4 s). A resumed job now waits 600 s for the workflow to start unless
  `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` is set; for the deployment, set
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` and restart the server, with the settings it
  was started with, before resuming
  ([Server failover](docs/failover.md#resuming-over-a-slow-link)).
- A time limit set to NVIDIA FLARE 2.7.1's default
  (`SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL=90` or
  `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT=3600`) did not reach the job: FLARE's job
  export leaves out arguments equal to the Hub's defaults, so the FL server used
  the default of its own image, 360 000 s in the worked example's image. SwarmMed
  now writes every time limit it sets, the new start timeout included, into the
  job's server configuration.
- `SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL` and
  `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT` accepted `nan` and `inf`, which NVIDIA
  FLARE takes as limits that never expire. Such values are now ignored with a
  warning, like other invalid values.
- A running job that a site had dropped out of showed no sign of it. In a test
  across three hospitals, one site's training process was killed and the other
  sites waited for it; the FL server reported the site's job as missing every
  10 s, its time limits did not end the job, and SwarmMedHub showed the job as
  "Running" for five hours. Such a job now carries the warning above.
- On the Training page and in the CLI, a running job's progress counted its first
  round as finished before it had finished: a job of 10 rounds showed 10 % from
  the start, with an ETA based on that. Its progress now stays at 0 %, with the
  ETA "Calculating...", until the first round has finished. `rounds_finished` in
  the CLI's `--json` job output and the training-state endpoint stays empty
  (null) until the job's first round has finished; the job monitor used to set it
  to 0 on its first pass.
- The breast-MRI worked example could run out of memory on hosts with many CPUs.
  MediSwarm's data module in the training image starts one data-loader worker per
  CPU of the host, and in a test across three hospitals, on servers with 20 to 36
  CPUs and an installation limited to 16 CPUs and 28 GiB, a training process was
  stopped for running out of memory. `experiments/duke_mst_cli/training.py` now
  starts no more workers than the CPUs available to the process and at most
  `SWARMMED_MAX_LOADER_WORKERS` (default 16), and logs the count when it starts
  (`Data-loader workers: 16 (host CPUs: 36, available: 36, cap 16)`). The CPUs
  available are the host's CPUs, limited by the process's CPU affinity and the
  client container's own CPU quota as Docker shows it by default (with a private
  cgroup namespace on cgroup v2, or on cgroup v1); a quota on an enclosing
  container is not seen. The paper's runs used hosts with 16 CPUs and so 16
  workers, which the default reproduces. The runs in that test used an earlier
  form of the cap, which gave 16 workers on those hosts too. SwarmMed passes
  `SWARMMED_MAX_LOADER_WORKERS` to the FL client containers like the example's
  other site settings. The helper that does this is `swarmmed_loader_workers.py`,
  named so that it does not replace the `loader_workers` module of MediSwarm 1.8.0
  and later. Projects created from `experiments/duke_mst_cli` before 1.0.4 keep the
  code stored with them. Replace it, as the project's author, with
  `swarmed project update --user <hub-user> <PROJECT_UUID>
  --code-dir ./experiments/duke_mst_cli`, which uploads `swarmmed_loader_workers.py`
  together with `training.py`; `training.py` no longer runs without it.
- `experiments/duke_mst_cli/explain/README.md` gave the occlusion settings of the
  first round of explanations (48×48 patches at stride 32 on every slice) and
  about 20 minutes for a run, and said that a failed AUROC check stops the run
  before any figures are produced. Like the explainability guide, it now gives the
  defaults (16×16 patches at stride 8 on every fourth slice), the 83 minutes of the
  recorded run of 16 cases, and that the maps and figures of the cohorts checked
  before a failed check are already written. It also says that
  `recompute_cam.py` and `render_from_npy.py` cannot process a run of a site's own
  cases, its output tree lists the files the script writes, and it names its
  checkpoint's FLARE job instead of linking to a file outside the repository.

## [1.0.3] - 2026-11-16

### Added

- A standby FL server and resuming an interrupted run ([Server failover](docs/failover.md)).
  `swarmed network failover --server-host IPV4_ADDRESS [--start-server]`
  switches a Hub's FL clients and admin connection to another server and, at
  the standby site, starts the server from the server kit in the site's startup
  package. `swarmed training resume` submits a new job that starts at the
  interrupted round from the newest checkpoint at the site and keeps the
  interrupted run's round numbers; `swarmed training checkpoints` lists the
  checkpoints. Only PyTorch jobs can be resumed.
- Every training site saves the model each round starts from
  (`swarmmed_resume_checkpoint.pt`); `SWARMMEDHUB_RESUME_CHECKPOINTS=false`
  turns this off.
- Startup packages also contain the FL server's local settings
  (`server_local/`), so that a participating site can start the server.
- `start_swarm_network_task` accepts a role (`server` or `client`) and a list of
  client participants for one call.
- A test that runs a job and a resumed job in NVIDIA FLARE's simulator
  (`SWARMMEDHUB_RUN_FLARE_SIMULATOR=1`).
- Six templates in `examples/` for other kinds of biomedical data, each a complete
  project folder (`training.py`, `prepare_data.py`, `validation.py`,
  `visualization.py`, `results_visualization.py`, `explainability.py`, `evaluate.py`
  and a README that records what was run): `tabular_heart_disease` (UCI Heart
  Disease), `ehr_eicu_mortality` (eICU-CRD Demo), `ecg_ptbxl` (PTB-XL),
  `multimodal_ptbxl` (PTB-XL ECG with age, sex, height and weight),
  `audio_heart_sounds` (PhysioNet/CinC Challenge 2016) and `eeg_sleep_staging`
  (Sleep-EDF Expanded). Each trained to completion on a local-test network of a
  fresh installation. `docs/templates.md` and `examples/README.md` list them;
  `docs/bring-your-own-task.md` describes how to write a template for a new task.
- Explanations in SwarmMedHub. A project's optional `explainability.py` runs in the
  sandbox on a job's results (by default 2 CPUs, 2 GB and 15 minutes:
  `SWARMMEDHUB_EXPLAIN_MEM_LIMIT`, `SWARMMEDHUB_EXPLAIN_CPUS`,
  `SWARMMEDHUB_EXPLAIN_TIMEOUT`). The Results page shows the run information, the
  run-level figures and the panels of each case, and project members review each
  case. In blinded review, which a reviewer starts in, a case shows only its ID, its
  label and its input panels until the reviewer has saved a read of it; cases are
  stored in random order. Migrations `project/0003` and `results/0002`.
- The `swarmmed_explain` package: explanation methods (occlusion, integrated
  gradients, gradient × input, Grad-CAM, feature ablation, permutation importance),
  case selection, agreement between methods, plotting helpers, the bundle format with
  its checks, and `python -m swarmmed_explain.run_local` for running an
  `explainability.py` outside the Hub.
- CLI: `swarmed results explain`, `swarmed results explanations` and
  `swarmed results explain-import`.
- `experiments/duke_mst_cli/explain/export_hub_bundle.py` converts the breast MRI
  explainability outputs into a bundle that the Hub can import.
- NetBird as the recommended overlay network, with Tailscale as an alternative. The
  helper `scripts/overlay_watcher.py` records this host's NetBird or Tailscale
  address in `tmp/overlay_status.json` every 30 s; `make overlay` starts it,
  `make overlay-stop` stops it, and `make start` runs `make overlay`.
  `apps/network/overlay.py` reads the status for the web interface, the CLI and the
  worker.
- Settings: `SWARMMEDHUB_OVERLAY` (`auto`, `netbird`, `tailscale` or `none`; `auto`
  uses NetBird if it is connected, otherwise Tailscale), `SWARMMEDHUB_LOCAL_ADDRESSES`
  (further addresses of this host, for example a WireGuard address) and
  `SWARMMEDHUB_OVERLAY_STALE_SECONDS` (default 120).
- An optional FL server address field in the web form for a new network.
- `docs/networking.md` and `docs/installation.md`: setting up NetBird with a
  self-hosted management server, NetBird Cloud for a trial, Tailscale and Headscale,
  WireGuard, OpenVPN and IPSec, restricting peers to the FL ports, and what has been
  tested.
- `examples/ecg_ptbxl/README.md` records runs of the template across three hospitals
  and a coordinating host, each on its own institutional network (13 November 2026,
  and three 20-round runs with seeds 0 to 2 on 16 November 2026), with deployment
  notes; `docs/networking.md` lists that deployment in its validation status.

### Changed

- The README and the documentation describe SwarmMed as a framework for biomedical
  data with templates; the breast MRI study remains the worked example.
- The default `ALLOWED_EXTENSIONS` (`core/settings/base.py` and `.env.template`)
  adds `.wav`, `.flac`, `.edf`, `.bdf`, `.dat`, `.hea`, `.mat` and `.tsv`.
  Installations set up earlier keep their list in `.env`; add the new extensions
  there.
- `swarmed project create --code-dir` and code uploads skip `__pycache__` folders
  and `.pyc` files.
- The personal-data export lists the user's explanation runs (without their script
  output) and reviews.
- The Hub recognises itself by every overlay address and by
  `SWARMMEDHUB_LOCAL_ADDRESSES`, not only by its Tailscale address.
- A new network needs a current overlay status: if the status is missing, older than
  the stale limit, dated in the future or disconnected, the web form and
  `swarmed network create` ask for the FL server address instead of guessing it.
- Breaking: an invalid `--server-ip` for `swarmed network create` is now an error
  (exit code 2); it used to be ignored.
- `make tailscale` and `scripts/tailscale_watcher.sh` remain as old names; replace
  cron or systemd entries for the old script with `make overlay`.
- The prerequisites list `python3` (3.8 or later) instead of `jq`.
- The example overlay address in the documentation is `100.64.0.10`.

### Fixed

- Stopping a network from the web interface or with `swarmed network stop`
  failed in 1.0.0 to 1.0.2: the stop task had lost its Celery task decorator.
- New installations: the Celery worker restarted in a loop. kombu 5.7.0 (released on
  3 November 2026) needs redis-py 6.4 or later, SwarmMed pins redis 5.0.1, and the
  Dockerfiles install without `uv.lock`, so every image built since then got kombu
  5.7.0. kombu is now pinned to 5.6.2. This also affects 1.0.2.
- FL clients could fail to train after a startup package with the server's local
  settings (`server_local/`) was imported. The import copied the server's and
  the admin's folders out of each site's kit but also left them in the site's
  folder, which is the site's FLARE workspace. FLARE takes the first
  configuration file of a name anywhere in that workspace, so a client could use
  the server's `comm_config.json`; its job process then looked for its parent
  process at the FL server, and the client never started training. The import
  now moves these folders to `prod_00/server/` and
  `prod_00/admin-<site>@nvidia.com/` instead. A single site's kit imported on its
  own, where `prod_00` itself is the site's workspace, keeps the server's folders
  in the network's folder. Starting a client moves them out of networks imported
  before. The internal host is now set to `localhost` in the `comm_config.json`
  that FLARE reads, not only in `local/comm_config.json`. No release contained
  `server_local/`, which came with the standby server above, but imports by
  1.0.0 to 1.0.2 left `server_startup/` and `admin_startup/` in each site's
  folder; starting a client moves these too.
- Exporting the startup package from a Hub that had imported it wrote some files
  of each site's kit twice: `.network_id`, `.participants.json` and
  `.gossip_token`, and the server's and the admin's kits that imports by 1.0.0
  to 1.0.2 left in the site's folder. Each file is now written once.
- The web interface's "Select project folder" stored the files below the folder's
  own name, so the job found no `training.py` at the root. An upload without a
  root-level `training.py` is now refused.
- The error pages failed when a request had no user, for example with a host name
  that is not in `DJANGO_ALLOWED_HOSTS`, so such a request got HTTP 500 instead of 400.
- The Results page: Sync Results and the result downloads did not work when the
  project had no results visualization script or no job was selected.
- The Results page inserts the visualization status as text, not as HTML.
- The README said that sandbox scripts can reach the Docker socket proxy; they
  cannot, nor PostgreSQL or Redis. `docs/security.md` now says that they can reach
  the internet while they run.
- `make env`, and so `make start` and `make restart`, failed with a traceback when
  no answer could be read (no terminal, closed input). It now keeps the existing
  values, uses the template's default for a new setting and says so, and stops with
  a message, without changing `.env`, if a placeholder such as the host name still
  needs a value.

## [1.0.2] - 2026-11-05

Documentation, metadata and analysis scripts. The only change to the platform is
the text of the privacy page.

### Added

- `experiments/duke_mst_cli/train_local_baseline.sh`, which trains the matched
  single-site and centralised baselines of the breast MRI worked example in the
  image and with the settings of the swarm runs.
- `experiments/duke_mst_cli/build_matched_baseline_report.py`, which compares the
  swarm runs with the matched baselines, and the single runs (FedProx, the run
  without node C, the web-interface run and the negative control) with the swarm
  runs, from per-case predictions.

### Changed

- `docs/reproduce.md` describes the matched baselines, the FedProx run and the
  new report, and that the Duke split files are published.
- `docs/os-support.md` states which operating systems have been tested.
- `docs/nvflare-comparison.md`: how the sandboxed site scripts read a site's data.
- `CITATION.cff`: version, release date, affiliations, abstract and message.

### Fixed

- The privacy page no longer claims differential privacy, secure aggregation or
  Byzantine-robust aggregation, gives TLS 1.2 or 1.3 for the web interface, and
  lists startup kits with their private keys among the stored data.
- `examples/breast_mri/README.md` matches the current scripts and uses made-up
  example rows.
- `examples/breast_mri/data_splits/README.md`: the Duke dataset DOI, licence
  (CC BY-NC 4.0) and citations, and the split counts.
