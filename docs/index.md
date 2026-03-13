---
title: SwarmCloud Documentation
description: Installation and setup instructions for the SwarmCloud platform.
---

# Welcome to SwarmCloud Documentation

!!! warning "Research Use Only"
    This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmCloud is a **secure** and **scalable** platform for **decentralized learning** on medical data. It allows multiple parties to **collaboratively train machine learning** models **without sharing sensitive data**.

---

## 🧠 Supported Frameworks

SwarmCloud is framework-agnostic and provides a built-in adapter for all major machine learning libraries:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **HuggingFace Transformers**
- **MONAI** (Medical Open Network for AI)

The platform handles the streaming of data from S3-compatible storage (MinIO) and the secure loading of model weights across all common formats (`.pt`, `.pth`, `.ckpt`, `.h5`, `.keras`, `.npy`, `.pkl`, `.joblib`).

---

## Key Features
<div class="grid cards" markdown>

-   :material-assistant:{ .lg .middle } __Swarm Learning__

    ---

    Train models on decentralized data using NVIDIA FLARE.
    Keep sensitive medical data secure and private.

    [:octicons-arrow-right-24: Background](/background)

    [:octicons-arrow-right-24: Security](/security)

-   :material-apps:{ .lg .middle } __User-Friendly Interface__

    ---

    Manage your projects, data, networks, training jobs and results through a simple web interface.

    [:octicons-arrow-right-24: Getting Started](/getting-started)

    [:octicons-arrow-right-24: Usage](/usage)

-   :material-scale:{ .lg .middle } __Scalability__

    ---

    Scale your decentralized learning experiments from a few to many participants via Docker.

    [:octicons-arrow-right-24: Installation](/installation)
    
    [:octicons-arrow-right-24: Developer](/developer)

-   :material-scale-balance:{ .lg .middle } __Non-Commercial License__

    ---

    SwarmCloud is licensed under PolyForm Noncommercial 1.0.0 and available on GitHub.

    [:octicons-arrow-right-24: Github](https://github.com/pfeifferis/SwarmCloud)
    
    [:octicons-arrow-right-24: Contribution](/contribution)

</div>

## FAQ

### What is SwarmCloud?

SwarmCloud is a secure and scalable platform for decentralized learning on medical data. It allows multiple parties to collaboratively train machine learning models without sharing sensitive data via a user-friendly web interface.

### How does Swarm Learning work?

Swarm Learning enables decentralized model training by allowing participants to train models on their local data and share only the model updates, rather than the data itself. This approach helps to maintain data privacy and security.

### What are the system requirements for SwarmCloud?

SwarmCloud requires Docker and Docker Compose for deployment. Additionally, a VPN connection (e.g., Tailscale) is recommended for secure communication between participants.

### How can I contribute to SwarmCloud?
We welcome contributions to the SwarmCloud project! Please refer to the [Contribution Guide](/contribution) for more information on how to get involved.