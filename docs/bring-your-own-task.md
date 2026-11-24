---
title: Bring your own task
description: From your own data and model to a SwarmMed code bundle, step by step.
---

# Bring your own task

This page takes you from your own data and model to a code bundle that trains on
SwarmMed. Every step has a worked example in the [templates](templates.md); the
[code bundle reference](developer.md) documents each interface in full.

## 1. Choose the data layout

Each site registers its files in its own SwarmMedHub, and `training.py` reads them
through the platform's data adapter. Use one of the two layouts of the templates:

* **One site's files at the root** of its data: a real deployment, in which each
  site registers only its own data.
* **One folder per FLARE site name** (`fl-client-1/`, `fl-client-2/`, ...): for a
  local-test network on one host, where every client sees the same project data
  and reads only the folder named after it.

Supporting both costs a few lines: take the site name from
`nvflare.client.get_site_name()` (import `nvflare.client` after
`flare_adapter.init_flare()`), use the keys that start with `<site>/` if there are
any, and fall back to the root ([Templates](templates.md#data-layout)).

Import skips files whose extension is not in `ALLOWED_EXTENSIONS`. SwarmMed's built-in
default (`core/settings/base.py`) covers tables (CSV, TSV, Parquet), NumPy and HDF5
arrays, DICOM, NIfTI, common image formats, WAV and FLAC audio, EDF and BDF recordings,
WFDB records (`.hea`, `.dat`) and MATLAB files. The value in a site's `.env` overrides
the default, and a site can change it there. `make env` writes the default into a new
`.env`; an installation set up with an earlier version keeps its old list and has to add
`.wav,.flac,.edf,.bdf,.dat,.hea,.mat,.tsv` there to import these files.

## 2. Prepare the data at each site (optional)

If the raw data need converting (resampling, cutting into windows, extracting
features), write a `prepare_data.py` that each site runs before it imports its data.
Split by patient, subject or recording, so that no one appears in two splits, and
write a `site_info.json` with the source, the counts and the preprocessing. Compact
files (`.npz` or CSV) make the import and the data checks fast. Keep the output
outside the code folder: everything in that folder is uploaded as code.

## 3. Write `training.py`

The loop is the same for every kind of data. SwarmMed copies `flare_adapter.py` next
to `training.py` in every job and replaces `main(project_id="default_project")` with
the project's ID, so keep that line exactly as it is.

```python
import torch

import flare_adapter

SWARM_ROUNDS = 10  # read by SwarmMed when the job is built


def main(project_id: str):
    flare_adapter.init_flare()
    site = site_name()  # your code, see step 1
    with flare_adapter.get_data_filesystem(project_id) as fs:
        train, val = load_site_data(fs, site)  # your code: fs.glob, fs.open, fs.read_bytes
    model = build_model()  # your code
    while True:
        global_model = flare_adapter.receive_model()
        if global_model is None:  # the job has ended
            break
        if global_model.params:  # round 0 has no weights yet
            model.load_state_dict(flare_adapter.get_pytorch_state_dict(global_model.params))
        metrics = evaluate(model, val)  # score the received model before training
        print(
            f"SWARMMED_METRIC site={site} round={global_model.current_round} split=val "
            + " ".join(f"{key}={value:.4f}" for key, value in metrics.items()),
            flush=True,
        )
        train_one_round(model, train)  # your code
        flare_adapter.send_model(
            params=model.state_dict(),
            metrics={key: float(value) for key, value in metrics.items()},
            meta={"NUM_STEPS_CURRENT_ROUND": len(train), "accuracy": float(metrics["accuracy"])},
        )


if __name__ == "__main__":
    main(project_id="default_project")
```

* `NUM_STEPS_CURRENT_ROUND` becomes the site's weight in the average; the templates
  use the number of training cases. The job SwarmMed builds keeps the best global model
  as `best_FL_global_model.pt`. NVIDIA FLARE's `SimpleIntimeModelSelector` chooses it by
  the number under the key `accuracy` in the `meta` given to `send_model`, not in
  `metrics`: the mean of the sites' values in a round, from round 1 on, where higher is
  better. Put a score there that grows as the model improves, even if your task's main
  metric is another one. A resumed run chooses its best model again, from its first
  resumed round on ([Server failover](failover.md#what-swarmmed-provides)).
* The number of rounds comes from `SWARMMEDHUB_SWARM_ROUNDS` if set, else from
  `SWARM_ROUNDS` in `training.py`, else 10. The job carries this number to every site
  (`config/swarmmed_run.json` in each app), so `swarmed training resume` knows it at
  any site. <!-- r106-check: write_run_file for every job (SubmitWithResumeTests::test_every_app_describes_the_run) -->
* Import the machine-learning library at the top of the file: SwarmMed detects the
  framework from the imports of `training.py` when it builds the job.
* Fix the seeds, limit the threads (`torch.set_num_threads`), do not write to
  relative paths, and do not print the environment: it holds the secret with which
  the adapter fetches the site's data manifest.
* Score the received model on the validation split before training, and print one
  `SWARMMED_METRIC` line per round, so that the run can be followed in the client
  logs. Keep the test split for `evaluate.py` and the result scripts.

## 4. Pin `requirements.txt`

List the packages `training.py` imports, pinned to the versions of the runtime image
(for example `numpy==2.4.3`, `scikit-learn==1.8.0`, `torch==2.10.0`). NVIDIA FLARE
and the adapter's own packages are installed already.

## 5. Add the sandbox scripts

`validation.py`, `visualization.py`, `results_visualization.py` and
`explainability.py` run in the Hub's sandbox, a separate Docker daemon, against the
site's registered data; the [code bundle reference](developer.md) describes their
helper objects.

* Validation and visualization scripts get 1 CPU, 1 GB of memory and 5 minutes.
* `explainability.py` gets 2 CPUs, 2 GB and 15 minutes by default
  (`SWARMMEDHUB_EXPLAIN_MEM_LIMIT`, `SWARMMEDHUB_EXPLAIN_CPUS`,
  `SWARMMEDHUB_EXPLAIN_TIMEOUT`); a model too large for that can be explained at the
  site with `python -m swarmmed_explain.run_local` and imported
  ([Explainability](explainability.md#explanations-computed-elsewhere)).
* Explanations are patient data. Use the site's pseudonymous case IDs and keep
  direct identifiers out of a case's `details`. The
  [guidelines](explainability.md#guidelines-for-explanations) cover the choice of
  methods and cases.
* The sandbox image has numpy, pandas, matplotlib, seaborn, scikit-learn, torch and
  a few other libraries, but not your `requirements.txt`
  ([Sandbox environment](developer.md#sandbox-environment)).

## 6. Test on one host

Create a local-test network with as many clients as you have site folders and run
the whole study on one host:

```bash
swarmed network create --user alice --project <PROJECT_UUID> --name "Local" --local-test --clients 3
```

The clients are named `fl-client-1` to `fl-client-N` and all read the same project
data, so give each its own folder. Networks created in the web interface with **Test
in local environment** have two clients. Read the `SWARMMED_METRIC` lines in the
client logs, then run the result scripts and the explanations on the finished job.

## 7. Move to several sites

Each site installs SwarmMed, imports the same code bundle and only its own data, at
the root of its data or in a folder named after its site name in the network. The
coordinating site creates the network with the participants' addresses and passes the
startup package to the other sites ([Networking](networking.md)). Agree beforehand on
the points in the [Threat model](threat-model.md): the coordinating server can read
the model updates, and every site receives every participant's keys.
