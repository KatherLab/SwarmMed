# Data Protection Impact Assessment (DPIA)

**Project:** SwarmCloud Decentralized Medical AI Platform  
**Date:** January 2, 2026  
**Status:** Periodic Review

## 1. Description of the Processing
SwarmCloud facilitates collaborative machine learning on medical datasets. The platform manages user accounts, project metadata, and coordinates training "jobs" between distributed nodes.

### 1.1 Nature of the Processing
The platform uses a decentralized "Swarm Learning" architecture. Raw data (including PHI) is never centralized. Only model updates (gradients/weights) are exchanged, protected by Differential Privacy and Secure Aggregation.

### 1.2 Scope of the Processing
- **Personal Data:** Account info (email, name), usage logs.
- **Sensitive Data:** Raw medical data (processed only at the source node), potentially sensitive model updates.
- **Geography:** Global, with default hosting in EU.

## 2. Necessity and Proportionality
Processing is necessary to enable privacy-preserving medical research. Traditional centralization of medical data is often prohibited by law or poses extreme security risks. SwarmCloud's "data stays local" approach is the most proportional and privacy-respecting way to achieve these research goals.

## 3. Assessment of Risks to Rights and Freedoms
| Risk | Likelihood | Severity | Mitigation |
| :--- | :--- | :--- | :--- |
| Unauthorized access to account data | Low | Medium | MFA, RBAC, Encryption. |
| Model inversion attack (re-identifying data from updates) | Low | High | **Differential Privacy (DP)** adds noise to gradients; **Secure Aggregation** prevents access to individual updates. |
| Data breach of metadata/logs | Low | Medium | Audit log signing, TLS 1.3, managed DB security. |
| Malicious participant in training | Medium | Medium | **Byzantine-robust** aggregation algorithms. |

## 4. Measures to Address Risks
1.  **Privacy by Design:** Differential Privacy integrated via NVIDIA FLARE.
2.  **Privacy by Default:** Decentralized architecture as the only mode of operation.
3.  **Technical Controls:** mTLS for all P2P traffic, AES-256 for storage.
4.  **Organizational Controls:** DPO appointment, ROPA maintenance, Breach Procedure implementation.

## 5. Conclusion
The residual risk is assessed as **Low**. The benefits of enabling collaborative medical research while strictly maintaining data sovereignty and minimizing data collection outweigh the potential risks, which are heavily mitigated by the platform's core architecture.

## 6. DPO Approval
- **Approved by:** [DPO Name/Signature]
- **Date:** January 2, 2026
