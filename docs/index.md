---
title: SwarmMed Documentation
description: Installation and setup instructions for SwarmMedHub and the swarmed CLI.
---

# Welcome to SwarmMed Documentation

!!! warning "Research Use Only"
    This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

MedSwarmHub is a **secure** and **scalable** platform for **decentralized learning** on medical data. It allows multiple parties to **collaboratively train machine learning** models **without sharing sensitive data**.

## ✨ Key Features
<div class="grid cards" markdown>

-   :material-assistant:{ .lg .middle } __Swarm Learning__

    ---

    Train models on decentralized data using NVIDIA FLARE.
    Keep sensitive medical data secure and private.

    [:octicons-arrow-right-24: Background](background.md)

    [:octicons-arrow-right-24: Security](security.md)

-   :material-apps:{ .lg .middle } __Shared Interfaces__

    ---

    Use the SwarmMedHub web interface or the local `swarmed` CLI against the same backend workflow.

    [:octicons-arrow-right-24: Getting Started](getting-started.md)

    [:octicons-arrow-right-24: Usage](usage.md)

-   :material-scale:{ .lg .middle } __Scalability__

    ---

    Scale your decentralized learning experiments from a few to many participants via Docker.

    [:octicons-arrow-right-24: Installation](installation.md)
    
    [:octicons-arrow-right-24: Developer](developer.md)

-   :material-scale-balance:{ .lg .middle } __Non-Commercial License__

    ---

    SwarmMedHub is licensed under PolyForm Noncommercial 1.0.0 and available on GitHub.

    [:octicons-arrow-right-24: Github](https://github.com/KatherLab/SwarmMed)
    
    [:octicons-arrow-right-24: Contribution](contribution.md)

</div>

## 🧠 Supported Frameworks

SwarmMedHub is framework-agnostic and provides a built-in adapter for all major machine learning libraries:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **HuggingFace Transformers**
- **MONAI**

## ❓ FAQ

### What is SwarmMed?

SwarmMed is a secure and scalable platform for decentralized learning on medical data. It combines the SwarmMedHub web interface with the local `swarmed` CLI so teams can collaborate without sharing raw data.

### How does Swarm Learning work?

Swarm Learning enables decentralized model training by allowing participants to train models on their local data and share only the model updates, rather than the data itself. This approach helps to maintain data privacy and security.

### What are the system requirements for SwarmMed?

SwarmMed requires Docker and Docker Compose for deployment. A VPN connection such as Tailscale is recommended for secure communication between participants, and the local `swarmed` CLI is currently intended primarily for Linux hosts.

### How can I contribute to SwarmMed?
We welcome contributions to the SwarmMed project. Please refer to the [Contribution Guide](contribution.md) for more information on how to get involved.
