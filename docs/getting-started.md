---
title: Getting Started
description: Getting started with SwarmMedHub and the swarmed CLI.
---

# Getting Started

This guide will help you get started with the SwarmMed repository, including installation, initial setup, code adaptation, and the choice between the SwarmMedHub web interface and the `swarmed` CLI.

## 🛠️ 1. Installation

!!! info "Proceed to Usage"
    If you or your team have already installed the platform and adapted your code, you can skip this section and proceed to the [Usage](usage.md) guide.

Before installing, check what each host needs and what the sites of a study have to agree on:

* [Operating systems](os-support.md) and [Hardware](hardware.md): Linux on every host that runs the FL server or trains, and the resources for the platform and for your model.
* [Networking](networking.md): the address and ports the coordinating site must provide, and the settings each participating site needs.
* [Threat model](threat-model.md): the coordinating site can read model updates in the default configuration, and every site receives every participant's keys. Choose the coordinating site, and agree on these points, before the study starts.

Follow the instructions in the [Installation](installation.md) guide to install and configure the shared SwarmMed environment. That setup gives you both the SwarmMedHub web interface and the `swarmed` CLI, which runs inside the web interface's application container.

## 💻 2. Code Creation and Adaptation

Once the platform is installed, you need to create or adapt your code to work with the shared SwarmMed project contract. Follow the instructions in the [Developer](developer.md) guide to prepare your code bundle for data validation, visualization, training, and results analysis.

## 📖 3. Usage

Once your code is ready, you can start using SwarmMed for decentralized (swarm) learning. Follow the [Usage](usage.md) guide for the web interface workflow or the dedicated [CLI](cli.md) guide for command-line operation.

A study with several sites also needs settings that the web interface does not offer, such as client-only mode at the participating sites; [Networking](networking.md) describes them. [Reproducing the worked example](reproduce.md) lists every command of a three-site run.
