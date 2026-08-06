# Agent PKB migration manifest

이 문서는 agent 카테고리의 개념·교재·연구·원본 레이어 분리 파일럿을 위한 실행 명세다. 경로 이동 전후 canonical_id는 변하지 않아야 한다.

## 정책

- concepts/: 개념별 정본, 번호 없음
- guides/: 읽기 순서·종합 설명
- research/: 조사 근거, 현재 경로 유지
- _origin/: 원본 PDF, 기본 검색 제외
- _archive/: 구버전, 기본 검색 제외
- 모든 curated Markdown은 schema_version/title/doc_type/canonical_id/status/authority/tags를 갖는다.

## Markdown (77)

| 현재 경로 | 목표 경로 | 유형 | canonical_id | 작업 |
|---|---|---|---|---|
| agent/00. 시작점/1. MOC.md | agent/00_MOC.md | moc | agent.moc | move+metadata |
| agent/00. 시작점/AI Agent 진화사.md | agent/guides/overview/AI Agent 진화사.md | guide | agent.guide.overview.ai-agent-진화사 | move+metadata |
| agent/00. 시작점/Agent 학습 로드맵.md | agent/guides/overview/Agent 학습 로드맵.md | guide | agent.guide.overview.agent-학습-로드맵 | move+metadata |
| agent/00. 시작점/LLM Agent Survey.md | agent/guides/overview/LLM Agent Survey.md | guide | agent.guide.overview.llm-agent-survey | move+metadata |
| agent/01. 추론과 탐색/Chain of Thought (CoT).md | agent/concepts/reasoning/Chain of Thought (CoT).md | concept | agent.reasoning.chain-of-thought-cot | move+metadata |
| agent/01. 추론과 탐색/Chain of Thought 논문 분석.md | agent/guides/reasoning/Chain of Thought 논문 분석.md | guide | agent.guide.reasoning.chain-of-thought-paper-analysis | move+metadata |
| agent/01. 추론과 탐색/LATS.md | agent/concepts/reasoning/LATS.md | concept | agent.reasoning.lats | move+metadata |
| agent/01. 추론과 탐색/ReAct.md | agent/concepts/reasoning/ReAct.md | concept | agent.reasoning.react | move+metadata |
| agent/01. 추론과 탐색/Tree of Thought (ToT).md | agent/concepts/reasoning/Tree of Thought (ToT).md | concept | agent.reasoning.tree-of-thought-tot | move+metadata |
| agent/02. 도구와 환경/RAG.md | agent/concepts/tool-use/RAG.md | concept | agent.tool-use.rag | move+metadata |
| agent/02. 도구와 환경/Self-Driven Grounding.md | agent/concepts/tool-use/Self-Driven Grounding.md | concept | agent.tool-use.self-driven-grounding | move+metadata |
| agent/02. 도구와 환경/Toolformer.md | agent/concepts/tool-use/Toolformer.md | concept | agent.tool-use.toolformer | move+metadata |
| agent/03. 자기개선과 검증/CRITIC.md | agent/concepts/evaluation/CRITIC.md | concept | agent.evaluation.critic | move+metadata |
| agent/03. 자기개선과 검증/Reflexion.md | agent/concepts/evaluation/Reflexion.md | concept | agent.evaluation.reflexion | move+metadata |
| agent/03. 자기개선과 검증/Self-Refine.md | agent/concepts/evaluation/Self-Refine.md | concept | agent.evaluation.self-refine | move+metadata |
| agent/04. 다중 에이전트/MADKE.md | agent/concepts/multi-agent/MADKE.md | concept | agent.multi-agent.madke | move+metadata |
| agent/04. 다중 에이전트/Multi-Agent Debate.md | agent/concepts/multi-agent/Multi-Agent Debate.md | concept | agent.multi-agent.multi-agent-debate | move+metadata |
| agent/04. 다중 에이전트/knowledge-graph-shared-memory-pattern.md | agent/guides/multi-agent/knowledge-graph-shared-memory-pattern.md | guide | agent.guide.multi-agent.knowledge-graph-shared-memory-pattern | move+metadata |
| agent/04. 다중 에이전트/mas-evaluation.md | agent/guides/multi-agent/mas-evaluation.md | guide | agent.guide.multi-agent.mas-evaluation | move+metadata |
| agent/05. 프레임워크와 최신 동향/DeepSeek-R1.md | agent/concepts/reasoning-models/DeepSeek-R1.md | concept | agent.reasoning-models.deepseek-r1 | move+metadata |
| agent/05. 프레임워크와 최신 동향/LangChain과 LangGraph.md | agent/guides/frameworks/LangChain과 LangGraph.md | guide | agent.guide.frameworks.langchain과-langgraph | move+metadata |
| agent/05. 프레임워크와 최신 동향/OpenAI o1.md | agent/concepts/reasoning-models/OpenAI o1.md | concept | agent.reasoning-models.openai-o1 | move+metadata |
| agent/05. 프레임워크와 최신 동향/Test-Time Compute Scaling.md | agent/concepts/reasoning-models/Test-Time Compute Scaling.md | concept | agent.reasoning-models.test-time-compute-scaling | move+metadata |
| agent/foundations/README.md | agent/guides/foundations/README.md | guide | agent.guide.foundations.readme | move+metadata |
| agent/foundations/agent-evolution-and-reasoning.md | agent/guides/foundations/agent-evolution-and-reasoning.md | guide | agent.guide.foundations.agent-evolution-and-reasoning | move+metadata |
| agent/foundations/multi-agent-collaboration-patterns.md | agent/guides/foundations/multi-agent-collaboration-patterns.md | guide | agent.guide.foundations.multi-agent-collaboration-patterns | move+metadata |
| agent/foundations/self-improvement-and-verification.md | agent/guides/foundations/self-improvement-and-verification.md | guide | agent.guide.foundations.self-improvement-and-verification | move+metadata |
| agent/foundations/test-time-compute-reasoning-models.md | agent/guides/foundations/test-time-compute-reasoning-models.md | guide | agent.guide.foundations.test-time-compute-reasoning-models | move+metadata |
| agent/foundations/tool-use-and-grounding.md | agent/guides/foundations/tool-use-and-grounding.md | guide | agent.guide.foundations.tool-use-and-grounding | move+metadata |
| agent/reference/README.md | agent/guides/reference/README.md | guide | agent.guide.reference.readme | move+metadata |
| agent/reference/agent-loop-loop-engineering.md | agent/guides/reference/agent-loop-loop-engineering.md | guide | agent.guide.reference.agent-loop-loop-engineering | move+metadata |
| agent/reference/agent-memory-architectures.md | agent/guides/reference/agent-memory-architectures.md | guide | agent.guide.reference.agent-memory-architectures | move+metadata |
| agent/reference/agent-operations-token-economics.md | agent/guides/reference/agent-operations-token-economics.md | guide | agent.guide.reference.agent-operations-token-economics | move+metadata |
| agent/reference/agent-workflow-selection.md | agent/guides/reference/agent-workflow-selection.md | guide | agent.guide.reference.agent-workflow-selection | move+metadata |
| agent/reference/ai-agent-glossary.md | agent/guides/reference/ai-agent-glossary.md | guide | agent.guide.reference.ai-agent-glossary | move+metadata |
| agent/reference/context-state-memory-checkpoint.md | agent/guides/reference/context-state-memory-checkpoint.md | guide | agent.guide.reference.context-state-memory-checkpoint | move+metadata |
| agent/reference/enterprise-agent-platform.md | agent/guides/reference/enterprise-agent-platform.md | guide | agent.guide.reference.enterprise-agent-platform | move+metadata |
| agent/reference/harness-production-checklist.md | agent/guides/reference/harness-production-checklist.md | guide | agent.guide.reference.harness-production-checklist | move+metadata |
| agent/reference/harness-runtime-contract.md | agent/guides/reference/harness-runtime-contract.md | guide | agent.guide.reference.harness-runtime-contract | move+metadata |
| agent/reference/llmops-agent-operations.md | agent/guides/reference/llmops-agent-operations.md | guide | agent.guide.reference.llmops-agent-operations | move+metadata |
| agent/reference/mcp-a2a-protocol-architecture.md | agent/guides/reference/mcp-a2a-protocol-architecture.md | guide | agent.guide.reference.mcp-a2a-protocol-architecture | move+metadata |
| agent/reference/orchestrator-selection-airflow-temporal-langgraph.md | agent/guides/reference/orchestrator-selection-airflow-temporal-langgraph.md | guide | agent.guide.reference.orchestrator-selection-airflow-temporal-langgraph | move+metadata |
| agent/reference/probabilistic-core-deterministic-shell.md | agent/guides/reference/probabilistic-core-deterministic-shell.md | guide | agent.guide.reference.probabilistic-core-deterministic-shell | move+metadata |
| agent/reference/rag-tool-calling-mcp.md | agent/guides/reference/rag-tool-calling-mcp.md | guide | agent.guide.reference.rag-tool-calling-mcp | move+metadata |
| agent/reference/task-graph-vs-knowledge-graph.md | agent/guides/reference/task-graph-vs-knowledge-graph.md | guide | agent.guide.reference.task-graph-vs-knowledge-graph | move+metadata |
| agent/reference/verifier-eval-observability.md | agent/guides/reference/verifier-eval-observability.md | guide | agent.guide.reference.verifier-eval-observability | move+metadata |
| agent/research/2026-06-12-mcp-architecture-multiagent-orchestration.md | agent/research/2026-06-12-mcp-architecture-multiagent-orchestration.md | research | agent.research.2026-06-12-mcp-architecture-multiagent-orchestration | metadata |
| agent/research/2026-06-26-multi-agent-orchestration-topology.md | agent/research/2026-06-26-multi-agent-orchestration-topology.md | research | agent.research.2026-06-26-multi-agent-orchestration-topology | metadata |
| agent/research/2026-07-03-ai-agent-benchmark-reliability.md | agent/research/2026-07-03-ai-agent-benchmark-reliability.md | research | agent.research.2026-07-03-ai-agent-benchmark-reliability | metadata |
| agent/research/2026-07-10-mcp-tool-poisoning-security.md | agent/research/2026-07-10-mcp-tool-poisoning-security.md | research | agent.research.2026-07-10-mcp-tool-poisoning-security | metadata |
| agent/research/2026-07-17-mcp-protocol-security-vulnerabilities.md | agent/research/2026-07-17-mcp-protocol-security-vulnerabilities.md | research | agent.research.2026-07-17-mcp-protocol-security-vulnerabilities | metadata |
| agent/research/a2a-agentic-ai-2026.md | agent/research/a2a-agentic-ai-2026.md | research | agent.research.a2a-agentic-ai-2026 | metadata |
| agent/research/agent-dev-automation-survey-2026-07.md | agent/research/agent-dev-automation-survey-2026-07.md | research | agent.research.agent-dev-automation-survey-2026-07 | metadata |
| agent/research/agent-harness-bench-평가방법론.md | agent/research/agent-harness-bench-평가방법론.md | research | agent.research.agent-harness-bench-평가방법론 | metadata |
| agent/research/agentic-token-cost-optimization.md | agent/research/agentic-token-cost-optimization.md | research | agent.research.agentic-token-cost-optimization | metadata |
| agent/research/ai-agent-harness-종합조사-2026-07.md | agent/research/ai-agent-harness-종합조사-2026-07.md | research | agent.research.ai-agent-harness-종합조사-2026-07 | metadata |
| agent/research/ai-dev-comprehensive-survey-2026-07.md | agent/research/ai-dev-comprehensive-survey-2026-07.md | research | agent.research.ai-dev-comprehensive-survey-2026-07 | metadata |
| agent/research/ecosystem-survey-2026-07.md | agent/research/ecosystem-survey-2026-07.md | research | agent.research.ecosystem-survey-2026-07 | metadata |
| agent/research/finsaber-benchmark.md | agent/research/finsaber-benchmark.md | research | agent.research.finsaber-benchmark | metadata |
| agent/research/harness-concept-three-layers.md | agent/research/harness-concept-three-layers.md | research | agent.research.harness-concept-three-layers | metadata |
| agent/research/harness-rag-survey-2025H2-2026H1.md | agent/research/harness-rag-survey-2025H2-2026H1.md | research | agent.research.harness-rag-survey-2025h2-2026h1 | metadata |
| agent/research/knowledge-graph-engineering-anthropic-playbook-정리-2026-07.md | agent/research/knowledge-graph-engineering-anthropic-playbook-정리-2026-07.md | research | agent.research.knowledge-graph-engineering-anthropic-playbook-정리-2026-07 | metadata |
| agent/research/knowledge-graph/knowledge-graph-extraction-prompts.md | agent/research/knowledge-graph/knowledge-graph-extraction-prompts.md | research | agent.research.knowledge-graph-knowledge-graph-extraction-prompts | metadata |
| agent/research/knowledge-graph/knowledge-graph-production-operations.md | agent/research/knowledge-graph/knowledge-graph-production-operations.md | research | agent.research.knowledge-graph-knowledge-graph-production-operations | metadata |
| agent/research/knowledge-graph/knowledge-graph-query-implementation.md | agent/research/knowledge-graph/knowledge-graph-query-implementation.md | research | agent.research.knowledge-graph-knowledge-graph-query-implementation | metadata |
| agent/research/knowledge-graph/knowledge-graph-resolution-and-profiles.md | agent/research/knowledge-graph/knowledge-graph-resolution-and-profiles.md | research | agent.research.knowledge-graph-knowledge-graph-resolution-and-profiles | metadata |
| agent/research/knowledge-graph/knowledge-graph-sources-and-limitations.md | agent/research/knowledge-graph/knowledge-graph-sources-and-limitations.md | research | agent.research.knowledge-graph-knowledge-graph-sources-and-limitations | metadata |
| agent/research/llm-메모리-아키텍처-비교.md | agent/research/llm-메모리-아키텍처-비교.md | research | agent.research.llm-메모리-아키텍처-비교 | metadata |
| agent/research/llm-활용-동향-리서치.md | agent/research/llm-활용-동향-리서치.md | research | agent.research.llm-활용-동향-리서치 | metadata |
| agent/research/loop-engineering-anthropic-playbook.md | agent/research/loop-engineering-anthropic-playbook.md | research | agent.research.loop-engineering-anthropic-playbook | metadata |
| agent/research/loop-graph-harness-engineering-provenance.md | agent/research/loop-graph-harness-engineering-provenance.md | research | agent.research.loop-graph-harness-engineering-provenance | metadata |
| agent/research/managed-agents-brain-hands-anthropic.md | agent/research/managed-agents-brain-hands-anthropic.md | research | agent.research.managed-agents-brain-hands-anthropic | metadata |
| agent/research/mas-evaluation-benchmarks-2026.md | agent/research/mas-evaluation-benchmarks-2026.md | research | agent.research.mas-evaluation-benchmarks-2026 | metadata |
| agent/research/개발자동화-전체지도.md | agent/research/개발자동화-전체지도.md | research | agent.research.개발자동화-전체지도 | metadata |
| agent/research/멀티에이전트-시스템-구축설계-MAS.md | agent/research/멀티에이전트-시스템-구축설계-MAS.md | research | agent.research.멀티에이전트-시스템-구축설계-mas | metadata |
| agent/research/멀티에이전트-시스템-적용-분석.md | agent/research/멀티에이전트-시스템-적용-분석.md | research | agent.research.멀티에이전트-시스템-적용-분석 | metadata |
| agent/research/멀티에이전트_아키텍처_타당성_검토보고서.md | agent/research/멀티에이전트_아키텍처_타당성_검토보고서.md | research | agent.research.멀티에이전트-아키텍처-타당성-검토보고서 | metadata |

## 원본 파일

| 현재 경로 | 목표 경로 | 검색 |
|---|---|---|
| agent/papers/Loop-Engineering-IEEE.pdf | agent/_origin/papers/Loop-Engineering-IEEE.pdf | 기본 제외 |
| agent/papers/ReAct-2210.03629v3.pdf | agent/_origin/papers/ReAct-2210.03629v3.pdf | 기본 제외 |
| agent/papers/SHIELDS-OS-hardening-2606.05476v1.pdf | agent/_origin/papers/SHIELDS-OS-hardening-2606.05476v1.pdf | 기본 제외 |

## 적용 게이트

1. 목표 경로 충돌 0건
2. canonical_id 중복 0건
3. 이동 전 원본 해시 manifest 보존
4. WikiLink·Markdown 상대 링크 검사 통과
5. 새 인덱스에서 curated 검색 평가 통과
6. 이전 인덱스와 Git diff를 롤백 수단으로 유지
