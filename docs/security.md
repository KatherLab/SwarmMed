---
title: Security
description: Security best practices implemented in SwarmCloud.
---

# Security

Security is a foundational aspect of the SwarmCloud platform, designed to protect the confidentiality, integrity, and availability of your data and machine learning models. This page outlines the security measures implemented at various layers of the platform.

!!! warning "Shared Responsibility"
    While SwarmCloud provides a secure platform, the overall security of your decentralized learning setup also depends on the security of your own infrastructure and the adherence to security best practices by all participants.

## Application Security

### Access Control

SwarmCloud implements a robust role-based access control (RBAC) system to ensure that users only have access to the resources and operations that are necessary for their role.

*   **Admin:** The admin user has full control over the platform. They can manage users, projects, and system settings.
*   **Developer:** A developer can create and manage projects, upload data, run training jobs and view logs.
*   **Project Creator:** A project creator can create and manage their own projects.
*   **User:** A user can view and interact with the projects they are a member of.

### Auditing and Logging

Comprehensive logging and auditing are in place to provide visibility into the activities on the platform.

*   **Audit Trail:** All user actions, such as logins, project creation, data edits, and training job submissions, are logged. This creates a detailed audit trail that can be used for security analysis and compliance purposes.
*   **Log Access:** Audit logs are accessible to administrators and can be exported for analysis in external security information and event management (SIEM) systems.

### Vulnerability Management

We are committed to ensuring the security of our platform and its dependencies.

*   **Open Source Components:** We use well-maintained and reputable open-source components. We continuously monitor these components for security vulnerabilities.
*   **Patch Management:** We have a process in place for promptly applying security patches to our platform and its dependencies.

### _DJANGO_[^1]

The web interface of SwarmCloud is built on the Django framework, which has a strong focus on security and provides built-in protection against many common web vulnerabilities.

*   **Cross-Site Scripting (XSS):** Django's template engine automatically escapes variables, which prevents most XSS attacks.
*   **Cross-Site Request Forgery (CSRF):** Django has built-in CSRF protection that is enabled by default.
*   **SQL Injection:** Django's ORM uses parameterized queries, which prevents SQL injection vulnerabilities.

[^1]: [https://docs.djangoproject.com/en/stable/topics/security/](https://docs.djangoproject.com/en/stable/topics/security/)

## Data Security

### _MINIO_[^2]

All project-related data, including datasets and models, is stored in a self-hosted Minio object storage server. This provides a high level of control and security over your data.

*   **Encryption at Rest:** Minio supports server-side encryption, which means that your data is encrypted before it is written to disk.
*   **Encryption in Transit:** All communication with the Minio server is encrypted using TLS.
*   **Access Control:** Minio has a fine-grained access control system that allows you to control who can access your data.
*   **Auditing:** All operations on the Minio server are logged, providing a detailed audit trail.

[^2]: [https://blog.min.io/s3-security-access-control/](https://blog.min.io/s3-security-access-control/)

## Network Security

### _TAILSCALE_[^3]

SwarmCloud leverages Tailscale to create a secure and private network for the participants in a decentralized learning experiment. This is especially important for swarm learning, which relies on peer-to-peer communication.

*   **End-to-End Encryption:** All traffic on a Tailscale network is end-to-end encrypted using WireGuard.
*   **Zero-Config VPN:** Tailscale is a zero-config VPN, which means that it is easy to set up and does not require complex firewall rules.
*   **SOC 2 Type II certification:** Tailscale has completed a SOC 2 Type II certification, demonstrating its commitment to security and compliance.

[^3]: [https://tailscale.com/security/](https://tailscale.com/security/)

### HTTPS

All communication with the SwarmCloud web interface is encrypted using HTTPS. This ensures that your data is protected from eavesdropping and man-in-the-middle attacks.

## Decentralized Learning Security

### _NVIDIA FLARE_[^4]

The federated learning capabilities of SwarmCloud are powered by NVIDIA FLARE. The swarm learning paradigm implemented in FLARE has a unique security model.

*   **No Raw Data Exchange:** In a swarm learning setup, the raw data never leaves the participant's infrastructure. Only model updates are exchanged between the participants.
*   **Identity Security:** FLARE ensures the authentication and authorization of all communicating parties.
    *   **Authentication:** Utilizes Public Key Infrastructure (PKI) with a Root CA issuing certificates for each member.
    *   **Role-Based Access Control:** Defines roles such as Project Admin, Organization Admin, Lead, and Member to enforce permissions.
    *   **Authorization:** Enforces authorization based on user roles, either centrally or allowing each site to define its own authorization rules.
*   **Secure Peer-to-Peer Communication:** FLARE's secure peer-to-peer messaging is another layer of protection on top of basic communication security, such as SSL. When the system is in secure mode, each pair of peers have their own encryption keys to ensure that their messages can only be read by themselves, even if relayed through the FL server.
*   **Privacy Protection:** FLARE offers multiple approaches to safeguard data privacy:
    *   **Filtering Mechanism:** To enforce data privacy policies.
    *   **Differential Privacy:** To add noise to the data to protect individual privacy.
    *   **Homomorphic Encryption:** To allow computation on encrypted data.
    *   **Private Set Intersection (PSI):** To securely compute the intersection of two datasets.
    *   **Confidential Computing:** To protect data in use.
*   **Auditing:** Provides built-in audit logs for increased transparency and accountability.

[^4]: [https://nvidia.github.io/NVFlare/security/](https://nvidia.github.io/NVFlare/security/)

