---
title: SwarmMed Documentation
description: Documentation for SwarmMed 1.0.0, the SwarmMedHub web interface and the swarmed CLI.
---

# Welcome to SwarmMed Documentation

!!! warning "Research Use Only"
    This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmMed is a platform for **decentralized (swarm) learning** on medical data. Several institutions **train a machine-learning model together**: each keeps its data and trains on it locally, and the sites exchange model parameters instead of data. SwarmMed consists of the SwarmMedHub web interface and the `swarmed` command-line interface (CLI), and runs the training on NVIDIA FLARE.

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

-   :material-scale:{ .lg .middle } __Scalability__

    ---

    Measured with 3 to 20 simulated sites on a single host. Every model update passes through the coordinating host, whose traffic grows with the number of sites.

    [:octicons-arrow-right-24: Scalability](scalability.md)

    [:octicons-arrow-right-24: Hardware](hardware.md)

-   :material-scale-balance:{ .lg .middle } __Apache-2.0 License__

    ---

    SwarmMed is open source, licensed under the Apache License 2.0 and available on GitHub.

    [:octicons-arrow-right-24: Github](https://github.com/KatherLab/SwarmMed)
    
    [:octicons-arrow-right-24: Contribution](contribution.md)

</div>

## 🧠 Frameworks

The example training scripts exchange model weights with NVIDIA FLARE through a small adapter (`flare_adapter`) that converts them to NumPy arrays, which keeps the exchange independent of the machine-learning library. The repository includes example projects for:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **Hugging Face Transformers**
- **MONAI**

Only PyTorch has been run end to end with SwarmMed so far: the tabular example in `examples/pytorch`, in the [scalability benchmark](scalability.md), and the breast-MRI worked example with PyTorch Lightning ([Reproducing the worked example](reproduce.md)). The other examples are provided but not validated. Jobs for TensorFlow and Keras are generated with NVFlare's PyTorch model persistor ([SwarmMed and NVIDIA FLARE](nvflare-comparison.md)).

## ❓ FAQ

### What is SwarmMed?

SwarmMed is a platform for decentralized learning on medical data. It combines the SwarmMedHub web interface with the `swarmed` CLI, which runs inside the web interface's application container, so teams can collaborate without sharing raw data.

### How does Swarm Learning work?

Each site trains the current model on its own data and shares only the resulting model parameters. In each round one site, drawn at random, averages the sites' updates into the next model. In SwarmMed every update is relayed through the coordinating FL server, which can read it in the default configuration. Sharing parameters reduces data movement but does not by itself make a collaboration private: see the [Threat model](threat-model.md).

### How has SwarmMed been validated?

With one public dataset: the Duke breast-MRI cohort, split by patient into three label-balanced shares on three GPU hosts of one institution, with the FL server on a fourth host. The final models were also scored on test sets of six other cohorts. This shows the platform running a swarm across separate hosts; it does not show a deployment across institutions and their networks, or training on data from different institutions. See [Reproducing the worked example](reproduce.md) and [Scalability](scalability.md).

### What are the system requirements for SwarmMed?

Every host that runs the FL server or trains needs Linux, Docker Engine with the Compose plugin and, for imaging models such as the breast-MRI example, an NVIDIA GPU with the NVIDIA Container Toolkit. People who only use the web interface can use any operating system with a current browser. Networks created in the web interface rely on Tailscale; without it, create networks with the CLI. See [Installation](installation.md), [Operating systems](os-support.md), [Hardware](hardware.md) and [Networking](networking.md).

### How can I contribute to SwarmMed?
We welcome contributions to the SwarmMed project. Please refer to the [Contribution Guide](contribution.md) for more information on how to get involved.
