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

We are committed to ensuring the security of SwarmCloud and its dependencies.

*   **Open Source Components:** We use well-maintained and reputable open-source components. We continuously monitor these components for security vulnerabilities.
*   **Security Scanning:** We utilize automated security scanning tools to maintain the integrity of our codebase.
*   **Patch Management:** We have a process in place for promptly applying security patches to our platform and its dependencies.

## Compliance

### HIPAA (Health Insurance Portability and Accountability Act)

SwarmCloud is designed to be **HIPAA Capable**, providing the technical safeguards required for organizations handling Protected Health Information (PHI). However, full HIPAA compliance is a programmatic requirement that depends on both the platform's features and the hosting organization's operational practices.

#### Technical Safeguards (Implemented)

*   **Access Control:** 
    *   **Unique User Identification:** Every user is assigned a unique account and UUID.
    *   **MFA Support:** Multi-Factor Authentication is supported and can be enforced.
    *   **Automatic Logoff:** Sessions are configured to expire after 30 minutes of inactivity.
    *   **Encryption and Decryption:** All data at rest in MinIO is encrypted using AES-256 (SSE-S3). All data in transit is encrypted using TLS 1.2/1.3.
*   **Audit Controls:** 
    *   SwarmCloud maintains a detailed, tamper-evident audit log of all access to the system. Logs are signed and chained using HMAC-SHA256 to ensure integrity.
*   **Brute-Force Protection:** Built-in protection against automated login attempts via `django-axes`.
*   **Database Integrity:** Enforced SSL/TLS connections for PostgreSQL and Redis.

#### Remaining Gaps and Shared Responsibility

Achieving HIPAA compliance is a shared responsibility. The following items must be addressed at the deployment and operational levels:

1.  **Administrative Safeguards (Operational):** The hosting organization must implement required administrative controls, including risk analysis, formal security policies, workforce training, incident response procedures, and regular access reviews.
2.  **Infrastructure Encryption (Deployment):** Encryption at rest for the PostgreSQL metadata database is not guaranteed by the application layer. **You must ensure that the underlying host disks or volumes (e.g., EBS, LUKS) are encrypted.**
3.  **Secrets Management:** For production readiness, secrets should be moved from `.env` files to a secure secret management system (e.g., HashiCorp Vault, AWS Secrets Manager) with regular rotation.
4.  **Audit Log Governance:** While the system generates signed logs, the organization is responsible for log review, alerting (e.g., SIEM integration), and maintaining a long-term retention policy.
5.  **Business Associate Agreements (BAA):** You must ensure BAAs are signed with any third-party service providers (e.g., Cloud Providers, SMTP relays).
6.  **High-Privilege Components:** Components like `docker-socket-proxy` provide significant control over the environment. They must be locked down using network isolation and monitored closely.

#### Administrative and Physical Safeguards (HIPAA §164.308 & §164.310)

These safeguards must be implemented by the hosting organization:

*   **Security Management Process:** Conduct regular risk assessments.
*   **Assigned Security Responsibility:** Designate a security official.
*   **Workforce Security:** Implement authorization and supervision procedures.
*   **Facility Access Controls:** Limit physical access to servers and workstations.
*   **Device and Media Controls:** Manage the receipt and removal of hardware containing PHI.

#### Emergency Access Procedures (Break-Glass)

In accordance with HIPAA §164.312(a)(2)(ii), SwarmCloud supports emergency access procedures:

1.  **Administrative Override:** Platform Administrators can grant temporary "Emergency Access" roles to qualified personnel.
2.  **Audit Logging:** All emergency access events are logged with high severity (CRITICAL) in the tamper-evident audit trail, including the justification provided for the access.
3.  **Verification:** Personnel requesting emergency access must be verified via their registered `phone` number or other out-of-band methods before administrative override is performed.

#### Data Retention and Purging

SwarmCloud implements automated data retention policies to comply with HIPAA and GDPR requirements:

*   **PHI Retention:** Records containing PHI are retained for 6 years (2190 days) by default, after which they are automatically purged from the database and associated storage via the `purge_expired_data` task.
*   **Backup Retention:** Database backups are retained in S3 for 30 days.
*   **Configuration:** These periods can be adjusted in `core/settings.py` using `DATA_RETENTION_DAYS` and `BACKUP_RETENTION_DAYS`.

#### Encryption Key Rotation

To maintain high security standards, SwarmCloud supports cryptographic key rotation:

*   **Audit Log Signing:** The keys used to sign audit logs should be rotated periodically. This can be performed using the management command:
    ```bash
    python manage.py rotate_signing_key
    ```
*   **Storage Encryption:** It is recommended to rotate S3/MinIO SSE-S3 keys at the infrastructure level annually.

#### Data De-identification

While SwarmCloud supports HIPAA-compliant workflows for PHI, we strongly recommend following the **HIPAA Safe Harbor** method for de-identification (removing 18 specific identifiers) before uploading datasets to the platform whenever possible to minimize risk.

### GDPR (General Data Protection Regulation)

SwarmCloud implements technical and organizational measures to support GDPR compliance for users within the European Union.

#### Key GDPR Features

*   **Data Minimization (Article 5):** The decentralized Swarm Learning architecture ensures that raw personal/medical data never leaves the local infrastructure of the participant.
*   **Right to Erasure (Article 17):** Users have a self-service "Delete Account" option in their profile settings, which permanently removes their account, profile, and associated projects from the system.
*   **Right to Rectification (Article 16):** Users can update their personal information at any time via the User Settings dashboard.
*   **Consent (Article 7):** Mandatory acceptance of the Terms and Conditions and Privacy Policy is required during the registration process.
*   **Data Protection by Design (Article 25):** Privacy-preserving technologies (Differential Privacy, Secure Aggregation) are integrated into the core training workflows via NVIDIA FLARE.

#### Documentation

The full Privacy Policy is available within the application and outlines how we handle personal data in accordance with GDPR principles.

---

## Security Scans

You can run these security scans locally to identify potential vulnerabilities in the codebase or dependencies:

### Dependency & Vulnerability Scanning (Snyk)
Snyk identifies known vulnerabilities in dependencies and provides security analysis for the source code.
```bash
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json
```

### Python Static Analysis (Bandit)
Bandit is used to find common security issues in Python code.
```bash
bandit -r apps core home manage.py -f json -o bandit_report.json    
```

### _DJANGO_[^1]

The web interface of SwarmCloud is built on the Django framework, which has a strong focus on security and provides built-in protection against many common web vulnerabilities.

*   **Cross-Site Scripting (XSS):** Django's template engine automatically escapes variables, which prevents most XSS attacks.
*   **Cross-Site Request Forgery (CSRF):** Django has built-in CSRF protection that is enabled by default.
*   **SQL Injection:** Django's ORM uses parameterized queries, which prevents SQL injection vulnerabilities.

[^1]: [https://docs.djangoproject.com/en/stable/topics/security/](https://docs.djangoproject.com/en/stable/topics/security/)

## Data Security

### _MINIO_[^2]

All project-related data, including datasets and models, is stored in a self-hosted Minio object storage server.

*   **Encryption at Rest:** Minio is configured with Server-Side Encryption (SSE-S3) using a unique KMS secret key. This ensures that all objects are encrypted before being persisted to disk.
*   **Encryption in Transit:** All communication with the Minio server (both public and internal) is encrypted using TLS.
*   **Access Control:** Minio has a fine-grained access control system that allows you to control who can access your data.
*   **Auditing:** All operations on the Minio server are logged, providing a detailed audit trail.

### Database and Cache

*   **PostgreSQL SSL:** Communication between the application and the PostgreSQL database is enforced to use SSL with certificate verification.
*   **Redis TLS:** Communication with Redis is encrypted using TLS, and access is protected by a strong password.

### Storage Encryption (At Rest)

For maximum security, it is strongly recommended to host the following directories on encrypted volumes (e.g., LUKS, FileVault, or cloud-provider encrypted EBS):
*   `postgres_data/`: Contains all metadata and project information.
*   `minio_data/`: Contains all datasets and models.
*   `workspaces/`: Contains NVFlare job data, logs, and temporary training artifacts.

## Network Security

### Internal Service Mesh

SwarmCloud employs an internal PKI (Public Key Infrastructure) to secure communication between all backend services.
*   Each service (MinIO, Postgres, Redis) has its own TLS certificate issued by a private internal Root CA.
*   The application and worker containers trust this internal Root CA, ensuring secure and verified connections across the internal Docker network.

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

