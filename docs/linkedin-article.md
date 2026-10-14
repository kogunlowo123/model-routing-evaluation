Many enterprises now pay for several foundation models, while each application team still hard-codes one of them.

The problem: model choice is made once, by habit, and never revisited. Compliance lives in a wiki. Spend shows up on next month's invoice.

Why it matters: a BAA or a residency clause is a contract, not a preference. Sending restricted data to an endpoint that lacks one is a breach, however good the answer. And a model that was best last quarter may not be best today.

We built an open reference platform that puts one governed decision point in front of every model call:

Task → Identity → Governance → Compliance filter → Learned router → GPT / Claude / Gemini / Nova / Llama / Phi → Guardrails → Output → Ledger and observability → Evaluation loop

How we implemented it:
1. Every agent call is authorized per action through an identity fabric.
2. Residency, BAA, classification and cost ceilings remove providers before any optimisation.
3. A Thompson-sampling bandit orders the rest by quality, latency, cost and compliance posture per task family, and keeps learning from schema checks, feedback and shadow traffic.
4. An offline harness grades golden sets; LLM judges must agree with human scores before their grades gate anything.
5. Adding a provider to a pool needs passing evidence and an approver who is not the requester.

Practices we kept: hard filters before scoring, prices as configuration, digests instead of stored prompts, signed audit chains, MCP and A2A endpoints authorized like the API.

Tradeoffs: routing adds latency, shadow evaluation costs money, judges need calibration work, and learned behaviour is harder to explain than a fixed rule. Using several vendors reduces lock-in but multiplies contracts to track.

The value: spend attributable to a task and an agent, and model decisions backed by evidence an auditor can follow.

As vendors ship new models every few months, the advantage shifts to teams that can measure and switch safely.

Question for architects: should a learned router ever choose a provider your evaluation data has not yet seen?

Code: https://github.com/kogunlowo123/model-routing-evaluation

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #ModelRouting #LLMEvaluation
