# Compliance control mapping

The evidence column names the artifact an auditor can inspect. HIPAA and
HITRUST apply where routed data includes ePHI; EU AI Act references apply
where the platform supports deployers of AI systems in the EU.

| Control objective | HIPAA Security Rule | HITRUST CSF v11 | SOC 2 (TSC 2017) | ISO/IEC 27001:2022 | ISO/IEC 42001:2023 | NIST AI RMF 1.0 / AI 600-1 | NIST SP 800-53 r5 | EU AI Act | Evidence |
|---|---|---|---|---|---|---|---|---|---|
| Data reaches only contractually approved processors | 164.308(b)(1), 164.314(a) | 05.k, 09.e | CC9.2 | 5.19, 5.20 | A.10.3 | GOVERN 6.1; AI 600-1 Data Privacy | SA-9, SR-3 | Art. 26 (deployer obligations) | `configs/compliance.yaml`, Helm values, `excluded` in every decision, `test_baa_required_for_restricted` |
| Data residency | 164.308(b)(1) | 06.c | CC6.7 | 5.34 | A.7.2 | MAP 4.1 | SC-7, PT-2 | Art. 10 (data governance) | Residency exclusions in decisions and audit, `test_residency_never_violated` |
| Least privilege for agents | 164.308(a)(4) | 01.c | CC6.3 | 5.15, 8.2 | A.9.2 | GOVERN 1.4 | AC-3, AC-6 | Art. 14 (human oversight) | Per-action authorization, `policies/agent-access.yaml`, `access.denied` events |
| Unique identity and authentication | 164.312(a)(2)(i), 164.312(d) | 01.q | CC6.1 | 5.16, 5.17 | A.9.2 | GOVERN 2.1 | IA-2, IA-9 | | Fabric decisions in audit (`agent`, delegation chain) |
| Human oversight of model changes | 164.308(a)(8) | 10.k | CC8.1 | 8.32 | A.6.2.5, A.6.2.6 | MANAGE 1.3, GOVERN 4.1 | CM-3, CM-4 | Art. 14, Art. 17 | Promotions with evidence, two-person rule, `promotion.*` events |
| Model evaluation before use | | 10.a | CC7.1 | 8.29 | A.6.2.4 | MEASURE 2.3, 2.5; AI 600-1 Information Integrity | SA-11, SI-7 | Art. 15 (accuracy, robustness) | Evaluation runs, golden-set digests, `docs/evaluation/methodology.md` |
| Measurement validity of automated graders | | | CC4.1 | 9.1 | A.6.2.4 | MEASURE 2.13, MEASURE 4.2 | CA-7 | Art. 15 | Judge calibrations (kappa, isotonic map) |
| Ongoing monitoring of AI performance | 164.308(a)(1)(ii)(D) | 09.ab | CC7.2 | 8.16 | A.6.2.6 | MEASURE 3.1, MANAGE 4.1 | CA-7, SI-4 | Art. 26(5), Art. 72 | Shadow results, reports, Prometheus alerts |
| Input and output content controls | 164.312(c)(1) | 09.j | CC6.8 | 8.7, 8.12 | A.6.2.4 | MANAGE 2.2; AI 600-1 Information Security | SI-3, SI-10, SI-15 | Art. 15(5) | `route.blocked` events, `mre_guard_blocks_total` |
| Data minimisation | 164.502(b) | 13.f | P4.1 | 5.34 | A.7.4 | MAP 2.1; AI 600-1 Data Privacy | PT-3, SI-12 | Art. 10 | Decision records hold digests only (ADR-0006) |
| Audit trail integrity | 164.312(b) | 09.aa | CC7.2 | 8.15 | A.6.2.8 | MEASURE 2.8 | AU-9, AU-10 | Art. 12 (record-keeping) | Signed chains, append-only triggers, `GET /v1/audit/verify` |
| Cost and resource accountability | | | CC3.2 | 5.9 | A.4.2 | MANAGE 2.1 | PM-3 | | Cost ledger, `GET /v1/ledger`, spend alert |
| Change control of policy and configuration | 164.308(a)(8) | 10.h | CC8.1 | 8.32 | A.6.2.5 | GOVERN 1.4 | CM-3, CM-5 | | Git history, CODEOWNERS, policy digest in decisions |
| Credential and key protection | 164.312(a)(2)(iv) | 06.d | CC6.1 | 8.24 | | MANAGE 2.3 | SC-12, SC-13 | | Key Vault, workload identity, External Secrets |
| Incident response | 164.308(a)(6) | 11.a | CC7.3, CC7.4 | 5.24, 5.26 | A.8.4 | MANAGE 4.3 | IR-4, IR-6 | Art. 73 (serious incidents) | Runbooks, alert rules |
| Availability and recovery | 164.308(a)(7) | 12.c | A1.2, A1.3 | 8.13, 8.14 | | | CP-9, CP-10 | | HA database, PDB, HPA, DR runbook |
| Secure development | 164.308(a)(1) | 10.b | CC8.1 | 8.25, 8.28 | A.6.2.3 | GOVERN 1.1 | SA-11, SA-15 | Art. 15 | CI gates: SAST, dependency audit, SBOM, image scans |

Validation evidence: the CI run for a released tag (all test suites, scans
and contract checks) is the operational qualification record for the
functional requirements in `docs/architecture/agent-specification.md`.
