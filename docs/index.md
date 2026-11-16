---
title: SwarmMed Documentation
description: Documentation for SwarmMed 1.0.0, the SwarmMedHub web interface and the swarmed CLI.
---

# Welcome to SwarmMed Documentation

!!! warning "Research Use Only"
    This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmMed is a platform for **decentralized (swarm) learning** on biomedical data. Several institutions **train a machine-learning model together**: each keeps its data and trains on it locally, and the sites exchange model parameters instead of data. SwarmMed consists of the SwarmMedHub web interface and the `swarmed` command-line interface (CLI), and runs the training on NVIDIA FLARE.

The platform does not interpret a study's data. A study is a code bundle: a `training.py` that reads the site's files through a small adapter, and optional scripts for data checks, plots and per-case explanations. [Templates](templates.md) provides such bundles for several kinds of data; the breast MRI study ([Reproducing the worked example](reproduce.md)) is the worked example.

This documentation describes SwarmMed 1.0.0.

## ✨ Key Features
<div class="grid cards" markdown>

-   :material-assistant:{ .lg .middle } __Swarm Learning__

    ---

    Train models on decentralized data with NVIDIA FLARE's client-controlled swarm workflow.
    Raw data stays at each site; model updates pass through the coordinating FL server, which can read them in the default configuration.

    [:octicons-arrow-right-24: Background](background.md)

    [:octicons-arrow-right-24: Threat Model](threat-model.md)

    [:octicons-arrow-right-24: Security](security.md)

-   :material-apps:{ .lg .middle } __Shared Interfaces__

    ---

    Use the SwarmMedHub web interface or the `swarmed` CLI against the same backend workflow.

    [:octicons-arrow-right-24: Getting Started](getting-started.md)

    [:octicons-arrow-right-24: Usage](usage.md)

-   :material-puzzle:{ .lg .middle } __Templates__

    ---

    Start a study from a template: a folder with the training script, data preparation, data checks, plots and explanations for one kind of data, from tabular records to ECG, heart sounds and sleep EEG.

    [:octicons-arrow-right-24: Templates](templates.md)

    [:octicons-arrow-right-24: Bring your own task](bring-your-own-task.md)

-   :material-scale:{ .lg .middle } __Scalability__

    ---

    Measured with 3 to 50 simulated sites on a single host. Every model update passes through the coordinating host, whose traffic grows with the number of sites.

    [:octicons-arrow-right-24: Scalability](scalability.md)

    [:octicons-arrow-right-24: Hardware](hardware.md)

-   :material-scale-balance:{ .lg .middle } __Apache-2.0 License__

    ---

    SwarmMed is open source, licensed under the Apache License 2.0 and available on GitHub.

    [:octicons-arrow-right-24: Github](https://github.com/KatherLab/SwarmMed)
    
    [:octicons-arrow-right-24: Contribution](contribution.md)

</div>

## 🧠 Frameworks and templates

The example training scripts exchange model weights with NVIDIA FLARE through a small adapter (`flare_adapter`) that converts them to NumPy arrays, which keeps the exchange independent of the machine-learning library. The repository includes example projects for:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **Hugging Face Transformers**
- **MONAI**

Templates for six kinds of data (tabular clinical data, intensive-care EHR, 12-lead ECG, ECG with patient metadata, heart sound recordings and sleep EEG) are complete project folders: a PyTorch training script, a script that prepares a public dataset, data checks, plots and per-case explanations. [Templates](templates.md) lists them with what was run for each.

Only PyTorch has been run end to end with SwarmMed so far: the tabular example in `examples/pytorch`, in the [scalability benchmark](scalability.md), and the breast-MRI worked example with PyTorch Lightning ([Reproducing the worked example](reproduce.md)). The six templates were each run to completion in NVIDIA FLARE's simulator with the job SwarmMed builds for them, on one host with simulated sites (11 November 2026). On 11 and 12 November 2026 each template also trained to completion on a SwarmMed local-test network (all sites as containers on one host, CPU) on a fresh installation of SwarmMed, through the CLI steps in its README, and its data checks, result plots and explanations ran in the Hub's sandbox. The other framework examples are provided but not validated. Jobs for TensorFlow and Keras are generated with NVFlare's PyTorch model persistor ([SwarmMed and NVIDIA FLARE](nvflare-comparison.md)).

## ❓ FAQ

### What is SwarmMed?

SwarmMed is a platform for decentralized learning on biomedical data. It combines the SwarmMedHub web interface with the `swarmed` CLI, which runs inside the web interface's application container, so teams can collaborate without sharing raw data.

### What kinds of data can SwarmMed train on?

Any data that a Python training script can read from the site's files. Each site's registered files reach the project's `training.py` through the platform's data adapter, and the sites exchange model weights; the platform does not interpret the data. The worked example trains on 3D breast MRI, and the [templates](templates.md) train on tabular clinical data, intensive-care EHR, 12-lead ECG, ECG with patient metadata, heart sound recordings and sleep EEG, each converted by the template's `prepare_data.py` into compact `.npz` files first. Files imported into SwarmMedHub need an extension listed in `ALLOWED_EXTENSIONS`. SwarmMed's built-in default (`core/settings/base.py`) covers tables (CSV, TSV, Parquet), arrays (NumPy, HDF5), DICOM, NIfTI, common image formats, WAV and FLAC audio, EDF and BDF recordings, WFDB records (`.hea`, `.dat`) and MATLAB files. A value in `.env` overrides the default: an installation set up with an earlier version keeps its old list there and has to add `.wav,.flac,.edf,.bdf,.dat,.hea,.mat,.tsv` to import these files.

### How does Swarm Learning work?

Each site trains the current model on its own data and shares only the resulting model parameters. In each round one site, drawn at random, averages the sites' updates into the next model. In SwarmMed every update is relayed through the coordinating FL server, which can read it in the default configuration. Sharing parameters reduces data movement but does not by itself make a collaboration private: see the [Threat model](threat-model.md).

### How has SwarmMed been validated?

With one public dataset: the Duke breast-MRI cohort, split by patient into three label-balanced shares on three GPU hosts of one institution, with the FL server on a fourth host. The final models were also scored on test sets of six other cohorts. This shows the platform running a swarm across separate hosts; it does not show a deployment across institutions and their networks, or training on data from different institutions. See [Reproducing the worked example](reproduce.md) and [Scalability](scalability.md). The six templates for other kinds of data were each run to completion in NVIDIA FLARE's simulator, with the job SwarmMed builds, on one host with simulated sites; [Templates](templates.md) lists what was run for each, with dates. These runs show that the templates' training runs to completion; they do not validate the models or a deployment across sites. One of them, `ecg_ptbxl`, also trained across three hospitals, each on its own server and institutional network, joined by a VPN, three times with different seeds, with a development version of SwarmMed (changes now in 1.0.3), installed and operated remotely by the SwarmMed team in an added container on each server, and with public data placed at the hospitals ([its README](https://github.com/KatherLab/SwarmMed/blob/main/examples/ecg_ptbxl/README.md#what-was-run)). These runs show a technical deployment across institutions, not training on data of the institutions themselves.

### What are the system requirements for SwarmMed?

Every host that runs the FL server or trains needs Linux, Docker Engine with the Compose plugin and, for imaging models such as the breast-MRI example, an NVIDIA GPU with the NVIDIA Container Toolkit. People who only use the web interface can use any operating system with a current browser. The web interface proposes this host's overlay address (NetBird or Tailscale) as the FL server's address; any other IPv4 address that every site can reach can be entered instead. See [Installation](installation.md), [Operating systems](os-support.md), [Hardware](hardware.md) and [Networking](networking.md).

### How can I contribute to SwarmMed?
We welcome contributions to the SwarmMed project. Please refer to the [Contribution Guide](contribution.md) for more information on how to get involved.
