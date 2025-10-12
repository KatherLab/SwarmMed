---
title: Background
description: Background information about MediSwarmCloud and Swarm Learning.
---


# Background

MediSwarmCloud was developed to address the challenges of applying **secure machine learning** to **large scale sensitive medical data** with **simple user interface**. Traditional machine learning requires centralizing data, which is often not feasible or desirable in the medical field due to privacy concerns and data governance regulations.

Decentralized learning offers a solution by allowing machine learning models to be trained on decentralized data. Instead of moving the data to a central server, the model is sent to the data. This way, sensitive information never leaves the premises of the data owner.

MediSwarmCloud provides the infrastructure and tools to facilitate decentralized learning in a **secure** and **user-friendly** manner. It leverages _NVIDIA FLARE_[^1] to handle the complexities of the swarm learning process, _MINIO_[^2] for object storage, and provides a _DJANGO_[^3]-based web interface for managing projects, data, and training jobs while securely connected peer via _TAILSCALE_[^4] VPN.

[^1]: [https://developer.nvidia.com/flare](https://developer.nvidia.com/flare)
[^2]: [https://min.io/](https://min.io/)
[^3]: [https://www.djangoproject.com/](https://www.djangoproject.com/)
[^4]: [https://tailscale.com/](https://tailscale.com/)

## Swarm Learning

### What is Swarm Learning?

Swarm learning is decentralized form of **federated learning** solution, wherein the responsibilities of **aggregation and model training control are distributed to all peers** rather than consolidated in a central server. It uses edge computing and blockchain technology to enable **peer-to-peer collaboration**. Multiple collaborators can **share data insights without sharing the data itself**, protecting data privacy and security while allowing all contributors to **benefit from collective learnings and improved performances**.[^5]

!!! tip "Original Research Paper"

    You can find the original research paper about Swarm Learning here: [Swarm Learning for decentralized and confidential clinical machine learning](https://doi.org/10.1038/s41586-021-03583-3)

### Why is Swarm Learning important?

As more data is collected and processed at the intelligent edge, its true value can only be realized by sharing it and turning it into a collective understanding. However, sharing data like this introduces security risks and in some cases is prohibited by government regulations. Because Swarm Learning shares insights rather than source data, the learnings derived from protected data can be **safely shared across locations and even across organizations**.[^6]

### What are the benefits of Swarm Learning?

Swarm learning offers several key advantages that address major challenges in traditional machine learning approaches. Organizations can combine their proprietary data with learnings from other organizations, **increasing accuracy** and **reducing bias**, while **keeping sensitive data completely on-site**. All cooperating partners can learn from each other without having to share personal patient data, as swarm learning keeps all research data on site, **exchanging only the algorithms and parameters**.
The approach preserves data privacy and sovereignty while reducing bandwidth and storage requirements, eliminating the need to move large datasets to central locations. This is particularly valuable as deep learning models have raised privacy and security concerns due to their reliance on large datasets on central servers. Instead of exposing raw data, users can share learnings at the edge, or at distributed sites, without moving or exposing data.
Swarm learning also enhances the diversity and quality of learning while adapting to dynamic and heterogeneous environments. With no central authority, blockchain is integrated to add control, privacy, and security, creating a trustless system where multiple organizations can collaborate effectively.[^6]

[^5]: Warnat-Herresthal, S., Schultze, H., Shastry, K.L. et al. Swarm Learning for decentralized and confidential clinical machine learning. Nature 594, 265–270 (2021). [https://doi.org/10.1038/s41586-021-03583-3](https://doi.org/10.1038/s41586-021-03583-3)
[^6]: [https://www.hpe.com/us/en/what-is/swarm-learning.html](https://www.hpe.com/us/en/what-is/swarm-learning.html)

### How does Swarm Learning work?

With Swarm Learning, training is done in multiple rounds. In each round, an aggregator client is randomly chosen from all clients, and then all training clients perform the training task on the current global model params. Once completed, all clients send their training results to the designated client for aggregation. The aggregated results are then applied to the current global model, which will become the base for the next round training. This process repeats until the configured number of rounds are completed.[^7]

``` mermaid
graph TD;
    A[Start] --> B{Randomly Choose Aggregator Client};
    B --> C[Send Training Task on Global Model to All Clients];
    C --> D[Clients Send Training Results to Aggregator];
    D --> E[Aggregator Updates Global Model];
    E --> F((Next Round Begins));
    F --> B;
```

#### _NVIDIA FLARE_ Swarm Learning architecture

_NVIDIA FLARE_ implementation of swarm learning leverages the newly added **secure peer-to-peer communication** between clients. The `server` is simply responsible for the job lifecycle management (health of client sites and monitoring of job status), while the `clients` are now responsible for training logic and aggregation management (where tasks are assigned via peer-to-peer communication).

Algorithmically, swarm learning is identical to federated averaging with the main differences being that the server will no longer control the aggregation process and will not have access to any sensitive information, such as trained model weights.

- The workflow is started from the `starting_client`. It loads the initial model using the `persistor`, and prepares the initial training params using the `shareable generator`.
- Randomly selects a `client` as the `aggregator` for the next round.
- Broadcast the “learn” task with training params to all `training_clients` and the `aggregation_client`. The task header contains the aggregation client name, the current round number, among other things.
- All `training_clients` do training by invoking the executor configured for the train task.
- Once completed, all `training_clients` send their results to the `aggregation_client`.
- When the “learn” task is received, the `aggregation_client`:
    - Calls the `shareable generator` to compute the current global model based.
    - sets up a Gatherer object to wait for results from `training_clients`. Note that the `aggregation_client` could also be a `training_client`.
- When a training result is received from another client, the Gatherer object of the `aggregation_client` calls the configured aggregator to accept the result.
- After all results are received (or other exit conditions occur such as timeout), the `aggregation_client`:
    - Calls the aggregate method of the aggregator to get the aggregation result.
    - Calls the shareable generator to apply the aggregated result to the current global model.
    - If not all rounds are completed, prepare for next round:
        - Randomly selects the `aggregation_client` for the next round.
        - Calls the `shareable generator` to prepare the training params.
        - Broadcast the “learn” task to other clients for the new round.
    - If all rounds are completed:
          - Broadcast the last result to all `result_clients`
          - Check which client has the best result, and ask that client to distribute the best model to all `result_clients`. [^6]
  
``` mermaid
sequenceDiagram
    participant SC as Starting Client
    participant Persistor
    participant SG as Shareable Generator
    participant TCs as Training Clients
    participant AC as Aggregator Client
    participant RCs as Result Clients
    participant BestClient as Best Result Client

    %% Initialization
    SC->>Persistor: Load initial model
    Persistor-->>SC: Returns initial model
    SC->>SG: learnable_to_shareable (prepare params)
    SG-->>SC: Returns initial training params
    SC->>SC: Randomly select Aggregator Client (AC) for Round 1

    %% Round Starts
    SC->>TCs: Broadcast "learn" task (params, round #, AC info)
    SC->>AC: Broadcast "learn" task (params, round #, AC info)

    %% Aggregator Client Setup
    AC->>SG: shareable_to_learnable (get current global model)
    SG-->>AC: Returns current global model
    AC->>AC: Setup Gatherer to wait for results

    %% Training and Contribution
    par Training
        TCs->>TCs: Execute local training
    and Aggregator Waits
        AC->>AC: (Can also perform training if configured)
    end

    TCs-->>AC: Send training result
    loop For each training result received
        AC->>AC: Fire BEFORE_CONTRIBUTION_ACCEPT event
        AC->>AC: aggregator.accept(result)
        AC->>AC: Fire AFTER_CONTRIBUTION_ACCEPT event
    end
    note right of AC: Loop continues until all results are received or a timeout occurs.

    %% Aggregation
    AC->>AC: Fire BEFORE_AGGREGATION event
    AC->>AC: aggregator.aggregate()
    AC-->>AC: Receives aggregation result
    AC->>AC: Fire AFTER_AGGREGATION event

    AC->>SG: shareable_to_learnable (apply aggregated result)
    SG-->>AC: Returns new global model

    %% Check for Completion
    alt Not all rounds completed
        AC->>AC: Randomly select next Aggregator Client
        AC->>SG: learnable_to_shareable (prepare next round params)
        SG-->>AC: Returns next round params
        AC->>TCs: Broadcast "learn" task for new round
        note over AC, TCs: Workflow repeats for the next round.
    else All rounds completed
        AC->>RCs: Broadcast final result
        AC->>AC: Determine which client has the best result
        AC->>BestClient: Request distribution of best model
        BestClient->>RCs: Distribute best model
    end
```

[^7]: [https://nvflare.readthedocs.io/en/2.4/programming_guide/controllers/client_controlled_workflows.html#swarm-learning](https://nvflare.readthedocs.io/en/2.4/programming_guide/controllers/client_controlled_workflows.html#swarm-learning)

## _MINIO_ Object Storage

## _DJANGO_ Web Interface

## _TAILSCACLE_ VPN

