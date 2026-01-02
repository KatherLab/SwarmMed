# Record of Processing Activities (ROPA)

**Date:** January 2, 2026  
**Version:** 1.0  
**Status:** Internal Use Only

In accordance with Article 30 of the General Data Protection Regulation (GDPR), [ORGANIZATION NAME] maintains this record of processing activities for which it is responsible.

## 1. Controller Details
- **Organization:** [ORGANIZATION NAME]
- **Address:** [ORGANIZATION ADDRESS]
- **Email:** [CONTACT EMAIL]
- **DPO:** [DPO EMAIL or "Not Appointed"]

## 2. Processing Activity: User Account Management
- **Purpose of Processing:** To manage user accounts, provide access to the platform, and deliver core services.
- **Categories of Data Subjects:** Platform users (medical researchers, data scientists, healthcare IT professionals).
- **Categories of Personal Data:** 
    - Identity Data: Username, Full Name.
    - Contact Data: Email address.
    - Technical Data: Hashed passwords, UUID.
- **Recipient Categories:** 
    - Internal IT and Support teams.
    - Sub-processors: [INSERT HOSTING/DB PROVIDERS].
- **International Transfers:** [INSERT DATA LOCATION, e.g., EU Frankfurt].
- **Retention Period:** Duration of the account plus [INSERT PERIOD, e.g., 30 days] after deletion.

## 3. Processing Activity: Audit Logging and Security
- **Purpose of Processing:** To ensure system security, prevent fraud, and maintain an audit trail for compliance (HIPAA/GDPR).
- **Categories of Data Subjects:** Platform users.
- **Categories of Personal Data:** 
    - Usage Data: Login times, IP addresses, action logs (project creation, data access).
- **Recipient Categories:** 
    - Security and Compliance teams.
- **International Transfers:** [INSERT DATA LOCATION].
- **Retention Period:** [INSERT PERIOD, e.g., 1 year] for standard security logs.

## 4. Processing Activity: Swarm Learning Coordination
- **Purpose of Processing:** To coordinate decentralized model training between participants.
- **Categories of Data Subjects:** Platform users.
- **Categories of Personal Data:** 
    - Identity Data: Organization name, project membership.
    - Technical Data: Public keys for mTLS, training job metadata.
- **Recipient Categories:** 
    - Participating nodes in the specific swarm project (only public keys and metadata).
- **International Transfers:** Transfers between nodes may occur across borders depending on the location of project participants. Protected by mTLS and WireGuard/Tailscale.
- **Retention Period:** Duration of the training project.

## 5. Technical and Organizational Security Measures
[ORGANIZATION NAME] implements the following measures to protect processed data:
- **Encryption at Rest:** AES-256 for all stored data.
- **Encryption in Transit:** TLS 1.3 for all web traffic; mTLS for peer-to-peer training.
- **Access Control:** Role-Based Access Control (RBAC) and Multi-Factor Authentication (MFA) support.
- **Data Minimization:** Decentralized architecture ensures raw medical data is never processed or stored by the central platform.
- **Anonymization/Pseudonymization:** Use of UUIDs and hashed identifiers where possible. Logs are anonymized upon account deletion.
