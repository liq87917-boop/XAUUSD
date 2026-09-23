"""合规授权数据 Evidence Intake Gateway（GOLD-005）。

本包提供**独立于** Collector / Scheduler / Alpha 的证据接收与审计入口：

- :mod:`src.evidence.contracts`：版本化输入契约（``evidence-intake-v1``，Author / News）；
- :mod:`src.evidence.validation`：逐行机械校验（授权 / 来源身份 / 时间语义 / 内容完整性）；
- :mod:`src.evidence.intake`：validate-first / dry-run-first 的导入引擎
  （幂等、坏行隔离、append-only 落 ``raw_items`` + ``processed_items``）；
- :mod:`src.evidence.report`：Markdown 报告 + quarantine JSONL 载荷（全部脱敏）；
- :mod:`src.evidence.templates`：Author / News 的 operator-ready 输入模板（GOLD-006），
  模板行带显式 synthetic/example 标记，导入时判 ``SYNTHETIC_EVIDENCE`` 隔离、绝不计入；
- :mod:`src.evidence.ledger`：只读台账，供 GOLD-004 资格观测层统计
  "经证据入口认证且具备独立历史可用证据"的记录；
- :mod:`src.evidence.workflow`：operator 工作流的写入门禁与隔离摘要（GOLD-007），
  显式写入前二次验证；只允许 ``ACCEPTED`` 行 append-only 落库；
- :mod:`src.evidence.author_chain`：**gateway-only** 作者归属链（GOLD-007），
  只消费已通过证据入口的 Author 证据，普通 CSV / 历史样本无法绕过；
- :mod:`src.evidence.inbox`：**纯本地只读**的 Evidence Inbox 发现与预检（GOLD-011）：
  只扫描**显式** ``--inbox-dir``，候选包必须由 ``manifest.json`` 显式关联证据文件
  （含内容 SHA-256），复用 gateway 的契约 / 引用校验 / 逐行机械校验 / 原因码 / 脱敏，
  生成**脱敏**的 discovered / preflight_pass / quarantined / requires_human_action artifact，
  **绝不**移动或删除原始 evidence、绝不自动 intake、绝不解除 ``PHASE3_3_DATA``；
- :mod:`src.evidence.review`：**纯本地**人工复核决策与审计闭环（GOLD-012）：
  只对**显式给出的内容级指纹**记录 ``APPROVE`` / ``REJECT`` / ``NEEDS_CHANGES``，
  ``APPROVE`` 必须当前指纹一致且 ``PREFLIGHT_PASS``（模板 / 示例 / Mock 永不可批准），
  追加式 ledger（确定性 ``decision_id``、幂等、冲突必须显式 revision + override、原子写），
  并可生成**脱敏**的 approved-for-explicit-intake 清单；它**不是**资格判定器，
  绝不写数据库、绝不调用 intake / commit、绝不解除 ``PHASE3_3_DATA``。
- :mod:`src.evidence.intake_plan`：**纯本地只读**的最终写入前 intake plan 门禁（GOLD-013）：
  把 GOLD-012 的批准清单与**当前** inbox / review ledger **重新绑定核验**（结构自洽 + 每条批准
  仍是 ledger 上该指纹的**最新有效**决策 + 候选仍在且仍 ``PREFLIGHT_PASS`` 且非合成 + 清单与
  重新验证结果完全一致），任一不一致 → **fail-closed**；输出**确定性脱敏**、内容级 ``plan_id``
  的只读计划（默认零写入，只有显式 ``--out`` 才写计划本身），并给出**显式** operator 命令模板
  （必带 ``--no-dry-run``）；它**不自动 intake**、**不写数据库**、**不解除** ``PHASE3_3_DATA``。
- :mod:`src.evidence.intake_receipt`：**纯本地只读**的执行后收据与资格复核审计（GOLD-014）：
  把 GOLD-013 plan、**当前** inbox / review 状态、人工**显式** Evidence Operator 执行结果
  （``--no-dry-run --manifest``）与随后的 qualification recheck 绑定为**确定性、脱敏、内容寻址**
  的 receipt；plan stale / fingerprint drift / review override / 执行结果缺失或失败或被改写 /
  recheck 缺失或被改写或早于显式执行 一律 **fail-closed**；明确区分 ``intake_executed`` 与
  ``receipt_verified``（**绝不**蕴含 ``data_qualification_passed`` / ``phase_transition_allowed``，
  两者**恒为** false），默认零写入（只有显式 ``--out`` 才原子落盘 receipt 本身）；
  Phase 切换仍是 **L3 人工 Gate**。
- :mod:`src.evidence.decision_packet`：**纯本地只读**的 L3 人工决策包（GOLD-015）：
  把最新 readiness / handoff（GOLD-008/010）、批准清单与 GOLD-013 intake plan、
  GOLD-014 verified receipt 与 qualification recheck 聚合为**确定性、脱敏、内容寻址**的
  人工 Gate packet；**复用** GOLD-013/014 的重新绑定口径（**不复制、不降低**资格规则），
  handoff 内部算术 / ``thresholds`` / 安全字段、readiness 快照**指纹同源性**、plan stale /
  fingerprint drift / review override / 执行结果缺失或被改写 / recheck 缺失或早于执行或结论
  不一致、以及**可选**收据文件的 ``receipt_id`` 内容寻址与**当前**重新绑定结果不一致 →
  一律 **fail-closed**；显式区分 ``evidence_ready_for_human_review`` / ``receipt_verified`` /
  ``qualification_recheck_ready``，且 ``data_qualification_passed`` /
  ``phase_transition_allowed`` **恒为** false、``blocker_active`` / ``human_gate_required``
  **恒为** true（**硬编码**），只能给出 ``submit_to_l3_human_gate`` 与缺口 / 稳定原因码；
  默认零写入（只有显式 ``--out`` 才原子落盘 packet 本身），**绝不**自动 intake、**绝不**写
  数据库、**绝不**解除 ``PHASE3_3_DATA``；Phase 切换仍是 **L3 人工 Gate**。
- :mod:`src.evidence.decision_record`：**纯本地、显式人工输入**的 L3 人工决策记录（GOLD-016）：
  把**人工显式**给出的 ``approve`` / ``reject`` / ``needs_changes`` 绑定到**具体**的 GOLD-015
  决策包（``packet_id`` + 内容摘要 + 产物摘要），生成**确定性、脱敏、内容寻址**（``record_id``）
  的人工决策记录；工具**绝不**自行生成批准、**绝不**判断数据资格、**绝不**修改 ``PROJECT_STATE``；
  ``approve`` 只允许落在 packet **本身** ``submit_to_l3_human_gate=true`` 且完整性核验
  （文档身份 / 安全字段 / 缺口算术 / 三个布尔合取 / readiness 同源 / 收据与 recheck 绑定 /
  计数自洽 / **禁止证据时间键** / **禁止未来时间**）**全部**通过时；任一不一致 →
  **fail-closed**（稳定原因码 + 零写入）；同 packet + 同人工决策幂等（同 ``record_id``、
  同审计时点逐字节稳定），写下的记录**绝不**被静默覆盖（必须显式 ``revision`` + ``supersedes``）；
  ``verify_decision_record`` 提供**防伪核验**（重新推导 ``record_id``、与**当前** packet 比对
  ``packet_id`` / 内容摘要、判定当初的 ``approve`` 是否仍成立）；默认零写入（只有显式 ``--out``
  才先取单实例锁再原子落盘记录本身），**绝不**修改 ``PROJECT_STATE``、**绝不**解除
  ``PHASE3_3_DATA``；Phase 切换仍是 **L3 人工 Gate**。
- :mod:`src.evidence.package_builder`：**纯本地、显式人工输入、默认 dry-run** 的 evidence package
  manifest builder（GOLD-017）：把**人工显式**给出的 ``evidence_type`` / ``source`` /
  ``authorization_reference`` / ``time_semantics`` / ``availability_semantics`` /
  ``historical_oos_applicable`` 与**人工指定**的现有 evidence 文件（package 目录内的**单级**文件名）
  整理成 GOLD-011 inbox 可直接消费的 ``manifest.json``（**复用** GOLD-011 的
  ``MANIFEST_REQUIRED_FIELDS`` / ``ALLOWED_MANIFEST_KEYS`` / ``FILE_ENTRY_REQUIRED_FIELDS`` /
  ``SUPPORTED_FILE_FORMATS`` 与 ``evidence-intake-v1`` 契约，**不复制、不降低**任何规则）；
  每个文件按**原始字节**计算 SHA-256、``manifest.files`` 按规范化相对路径确定性排序（同输入 →
  **byte-stable**，manifest 里**没有任何时间字段**）；写 manifest 前先做与 GOLD-005 / GOLD-011
  **同源**的逐行预检（``read_input_file`` + ``assess_row``）；拒绝绝对路径 / ``..`` / 多级路径 /
  符号链接 / 目录 / ``manifest.json`` 自引用 / 未支持格式 / 重复或大小写冲突路径 / 包内未声明文件 /
  敏感值与示例名称；默认**零写入**（只有显式 ``--out`` 才写，且**必须正好**是
  ``<package-dir>/manifest.json``，先取单实例锁再**原子写 + 写后复读自检**），既有 manifest
  **逐字节一致** → 幂等，**内容不同** → ``MANIFEST_CONFLICT`` fail-closed（**没有**
  ``--force`` / ``--overwrite``）；**绝不**自动 intake、**绝不**写数据库、**绝不**移动 / 删除 /
  改写原始 evidence、**绝不**解除 ``PHASE3_3_DATA``（生成 manifest ≠ 授权已核验 ≠ 资格通过）。
- :mod:`src.evidence.human_verification_attestation`：**纯本地、显式人工输入、默认零写入**的
  材料级人工核验凭证（GOLD-028）：把 GOLD-027 的结构完整预检进一步转换为**可审计的材料级
  人工核验凭证** —— 每个必核验材料记录受控决策（``VERIFIED`` / ``REJECTED`` /
  ``NEEDS_CHANGES``）、稳定 reason code、显式 reviewer label、带时区的 ``reviewed_at`` 与
  **独立 evidence reference**，并**硬绑定**当前候选 package fingerprint + GOLD-027
  handoff / manifest 内容身份 + scope（``attestation_id`` 内容寻址；任何 package / manifest /
  content fingerprint 漂移都使旧凭证失效，**绝不静默继承**）；``all_required_verified``
  **只**代表材料级人工核验完成，``data_qualification_passed`` / ``phase_transition_allowed`` /
  ``l3_l4_auto_advance_allowed`` / ``evidence_qualified`` / ``advance_allowed`` **恒为** false、
  ``blocker_active`` / ``human_gate_required`` / ``gate_blocked`` **恒为** true（**硬编码**）；
  Mock / 模板 / 示例 / 合成、``preflight_pass=false``、非 ``HUMAN_VERIFICATION_REQUIRED`` 材料
  一律**拒绝**被声明为 ``VERIFIED``（fail-closed）；``verify_attestation`` 提供**防伪核验**
  （重新推导 ``attestation_id``、与**当前**候选目录比对 package / handoff 内容身份）。
- :mod:`src.evidence.submission_readiness`：**纯本地只读**的人工证据**提交就绪包**（GOLD-033）：
  把 GOLD-026 ~ GOLD-030 已有的只读结论（缺口诊断 / handoff / intake handoff 契约 / 材料级
  人工核验 / 端到端链审计）汇总成**单一、确定、可操作**的"业务方还缺什么"清单，输出五个
  **互相独立**的结论（``engineering_ready`` / ``submission_materials_complete`` /
  ``human_verification_complete`` / ``data_qualification_passed`` /
  ``phase_transition_allowed``；后两者**恒为** false、``l3_gate_pending`` 恒为 true）、
  **稳定 missing reason codes** 与人工动作清单（每条**只引用**既有契约字段 / 既有阈值 /
  既有命令）；**绝不生成或推断** ``published_at`` / ``collected_at`` / ``effective_at`` /
  ``available_at`` / OOS 值，Mock / fixture / 模板**永不计**真实材料（演练模式禁止与真实输入
  同时出现），默认零写入（唯一写开关是显式 ``--out``），**绝不**解除 ``PHASE3_3_DATA``。

红线（与 `.clinerules` 一致）：

- 不联网、不抓取、不绕过 robots / 条款 / 证书；不把"公开可访问"当作采集或训练授权；
- 不伪造 ``published_at`` / ``collected_at`` / ``effective_at`` / 历史可用时间；
- 不覆盖历史事实（只 append-only；内容冲突判 ``IDENTITY_CONFLICT`` 并隔离）；
- 不因代码完成或 Mock 测试解除 ``PHASE3_3_DATA``。

入口：``scripts/intake_evidence.py``（单步 intake）与 ``scripts/evidence_operator.py``
（GOLD-007 单入口 operator workflow；两者均默认 dry-run、默认零写入、默认零网络）；
``scripts/evidence_inbox.py``（GOLD-011 只读发现 + 预检；唯一写入口是显式 ``--out``）；
``scripts/evidence_decision_packet.py``（GOLD-015 L3 人工决策包；默认只读，唯一写入口是显式
``--out``，且**没有**任何 intake 参数）；
``scripts/evidence_decision_record.py``（GOLD-016 L3 人工决策记录；**必须**显式给出
``--decision`` / ``--reviewer``，默认只读预检，唯一写入口是显式 ``--out``，``--verify-record``
为纯只读防伪核验模式，且**没有**任何 intake / 写库参数）；
``scripts/evidence_package.py``（GOLD-017 本地 package / manifest builder；元数据与文件清单
**必须**显式给出，默认 dry-run，唯一写入口是显式 ``--out``，且只能写
``<package-dir>/manifest.json``；**没有**任何 intake / 写库参数）；
``scripts/evidence_human_verification_attestation.py``（GOLD-028 材料级人工核验凭证；**必须**
显式给出 ``--verification``，默认只读，唯一写入口是显式 ``--out``，``--verify-attestation``
为纯只读重新绑定核验模式，且**没有**任何 intake / 写库参数）。
"""

# src.evidence 的公开导出改用 PEP 562 **惰性导出**（GOLD-018）。
#
# 为什么必须惰性：本包在 ``__init__`` 里 eager import 整个依赖图时，
# ``from src.evidence.<子模块> import ...`` 会**立即**加载 decision_packet / handoff 等
# 重型子模块；而这些子模块反过来依赖 ``src.monitoring.evidence_readiness``，
# 于是 ``import src.monitoring`` → 本包 ``__init__`` → ``handoff`` → 尚未初始化完的
# ``src.monitoring.evidence_readiness`` 会抛 ``ImportError: cannot import name
# 'BatchQuantification' from partially initialized module
# 'src.monitoring.evidence_readiness'``。
#
# 现在包初始化阶段**不加载任何子模块**：只有**真正被访问**的名字才 import 其定义
# 子模块，因此 monitoring-first / evidence-first / 直接子模块 first 等任意导入顺序都稳定。
#
# 下表**只登记名字 -> 定义所在子模块**，**不复制**任何业务类 / 阈值 / 枚举 / 常量：
# 单一事实源仍是各子模块；``from <pkg> import X`` / ``from <pkg> import <子模块>`` 的
# 既有用法**完全不变**（含 ``import <pkg>`` 后再取属性）。

from __future__ import annotations

import importlib
from typing import Any, Final

#: 公开名 -> 定义所在子模块（src.evidence 的子模块级导出索引）。
_LAZY_EXPORTS_BY_MODULE: Final[dict[str, tuple[str, ...]]] = {
    "author_chain": (
        "AUTHOR_CHAIN_NOTE", "AUTHOR_CHAIN_SCHEMA_VERSION", "AuthorChainReport",
        "GatewayAuthorEvidence", "attribute_gateway_author_evidence",
        "gateway_author_evidence", "load_gateway_author_evidence",
    ),
    "chain_audit": (
        "CHAIN_AUDIT_CONTRACT_VERSION", "CHAIN_AUDIT_EXECUTION_MODE", "CHAIN_AUDIT_FILE_NAME",
        "CHAIN_AUDIT_KIND", "CHAIN_AUDIT_LOCK_SUFFIX", "CHAIN_AUDIT_NOTE",
        "CHAIN_AUDIT_REPORT_NAME", "CHAIN_AUDIT_SCHEMA_VERSION", "CHAIN_AUDIT_SEMANTICS_NOTE",
        "CHAIN_REHEARSAL_SEMANTICS_NOTE", "CHAIN_STAGE_ORDER", "ChainAuditArgumentError",
        "ChainAuditCode", "ChainAuditError", "ChainAuditInputs", "ChainAuditPathError",
        "ChainAuditWriteError", "ChainStage", "ChainStageResult", "ChainStageStatus",
        "EVIDENCE_SOURCES", "EVIDENCE_SOURCE_OPERATOR_PROVIDED",
        "EVIDENCE_SOURCE_REHEARSAL_FIXTURE", "EvidenceChainAudit", "audit_evidence_chain",
        "chain_audit_exit_code_for", "compute_chain_facts_digest", "compute_chain_id",
        "main_audit_exit_code", "render_chain_audit_summary", "run_chain_audit",
    ),
    "contracts": (
        "ALLOWED_AUTHORIZATION_BASES", "AuthorizationDeclaration",
        "COMMON_REQUIRED_FIELDS", "EVIDENCE_CONTRACT_VERSION", "EVIDENCE_SCHEMA_VERSION",
        "EXAMPLE_MARKER_FLAG_FIELDS", "EXAMPLE_MARKER_KIND_FIELDS",
        "EXAMPLE_MARKER_VALUES", "EvidenceScope", "QUARANTINE_REASON_CODES", "ReasonCode",
        "RowStatus", "required_field_names", "synthetic_marker_fields",
    ),
    "decision_packet": (
        "DECISION_PACKET_FILE_NAME", "DECISION_PACKET_KIND", "DECISION_PACKET_LOCK_SUFFIX",
        "DECISION_PACKET_NOTE", "DECISION_PACKET_REPORT_NAME",
        "DECISION_PACKET_SCHEMA_VERSION", "DecisionPacket", "DecisionPacketArgumentError",
        "DecisionPacketError", "DecisionPacketPathError", "DecisionPacketStateError",
        "DecisionPacketStatus", "DecisionPacketVerificationError",
        "DecisionPacketWriteError", "HandoffDocument", "PACKET_EXECUTION_MODE",
        "PACKET_NEXT_STEP_NOTE", "PACKET_TIME_SEMANTICS_NOTE", "PacketVerificationCode",
        "PacketViolation", "READINESS_CHECK_KEYS", "RECEIPT_ENTRY_KEYS",
        "ReadinessStateDocument", "ReceiptDocument", "SCOPE_GAP_KEYS",
        "build_decision_packet", "compute_packet_id", "load_handoff_document",
        "load_intake_receipt_document", "load_readiness_state_document",
        "render_decision_packet_summary", "run_decision_packet", "verify_decision_packet",
    ),
    "decision_record": (
        "DECISION_RECORD_ACTIONS", "DECISION_RECORD_EXECUTION_MODE",
        "DECISION_RECORD_FILE_NAME", "DECISION_RECORD_KIND", "DECISION_RECORD_LOCK_SUFFIX",
        "DECISION_RECORD_NOTE", "DECISION_RECORD_REPORT_NAME",
        "DECISION_RECORD_SCHEMA_VERSION", "DECISION_RECORD_TIME_SEMANTICS_NOTE",
        "DECISION_SCOPE", "DecisionRecordArgumentError", "DecisionRecordCode",
        "DecisionRecordError", "DecisionRecordNotSubmittableError",
        "DecisionRecordPathError", "DecisionRecordStateError",
        "DecisionRecordVerification", "DecisionRecordVerificationError",
        "DecisionRecordWriteError", "HumanDecision", "HumanDecisionRecord",
        "MAX_RECORD_NOTE_CHARS", "MAX_RECORD_NOTE_INPUT_CHARS",
        "MAX_RECORD_REASON_CODE_CHARS", "MAX_RECORD_REVIEWER_CHARS", "MAX_RECORD_REVISION",
        "PACKET_CHECKS", "PacketBinding", "REVIEWER_KIND", "build_decision_record",
        "compute_record_id", "decision_record_exit_code_for", "load_decision_packet",
        "main_verification_exit_code", "render_decision_record_summary",
        "run_decision_record", "verify_decision_record",
    ),
    "handoff": (
        "BLOCKED_STATUS", "CHECK_CATEGORIES", "ChecklistItem", "EvidenceHandoffReport",
        "ExcludedEvidence", "HANDOFF_NOTE", "HANDOFF_REPORT_NAME",
        "HANDOFF_SCHEMA_VERSION", "PENDING_HUMAN_REVIEW_STATUS", "ScopeGap",
        "build_evidence_checklist", "build_excluded_evidence", "build_handoff_report",
        "render_handoff_markdown",
    ),
    "human_verification_attestation": (
        "ALL_REQUIRED_VERIFIED_SEMANTICS", "ATTESTATION_CONTRACT_VERSION",
        "ATTESTATION_EXECUTION_MODE", "ATTESTATION_FILE_NAME", "ATTESTATION_KIND",
        "ATTESTATION_NOTE", "ATTESTATION_REPORT_NAME", "ATTESTATION_SCHEMA_VERSION",
        "ATTESTATION_SCOPE", "ATTESTATION_TIME_SEMANTICS_NOTE",
        "ATTESTATION_VERIFICATION_KIND", "AttestationArgumentError", "AttestationCode",
        "AttestationError", "AttestationNotAttestableError", "AttestationPathError",
        "AttestationStateError", "AttestationVerification", "AttestationWriteError",
        "HumanVerificationAttestation", "MaterialAttestation", "MaterialDecision",
        "VerificationDecision", "attestable_material_keys", "attestation_schema",
        "build_attestation", "compute_attestation_id", "handoff_content_sha256",
        "load_attestation_document", "load_verification_input", "package_content_sha256",
        "render_attestation_summary", "run_attestation", "verify_attestation",
    ),
    "inbox": (
        "CandidatePackage", "EXIT_NO_CANDIDATES", "EXIT_UNUSABLE",
        "FILE_ENTRY_REQUIRED_FIELDS", "INBOX_NOTE", "INBOX_REPORT_KIND",
        "INBOX_REPORT_NAME", "INBOX_SCHEMA_VERSION", "InboxArtifactWriteError",
        "InboxDirError", "InboxError", "InboxPreflightReport", "InboxReasonCode",
        "InboxStatus", "MANIFEST_FILE_NAME", "MANIFEST_REQUIRED_FIELDS", "MAX_PATH_CHARS",
        "ManifestFileRef", "PENDING_FILE_NAME", "PENDING_KIND", "PENDING_LOCK_SUFFIX",
        "PendingEntry", "PendingRegister", "PendingStateError", "SUPPORTED_FILE_FORMATS",
        "ensure_outside_inbox", "load_pending_register", "render_inbox_summary",
        "run_inbox_scan", "scan_inbox", "write_pending_register",
    ),
    "intake": (
        "EvidenceIntakeReport", "InputFile", "InputRow", "IntakeCounts", "RowOutcome",
        "intake_evidence", "load_input_rows", "read_input_file", "resolve_format",
    ),
    "intake_handoff": (
        "INTAKE_HANDOFF_CONTRACT_VERSION", "INTAKE_HANDOFF_KIND",
        "INTAKE_HANDOFF_LAYOUT_NOTE", "INTAKE_HANDOFF_NOTE",
        "INTAKE_HANDOFF_SCHEMA_VERSION", "INTAKE_STATUS_ORDER", "MATERIAL_SPECS",
        "PREFLIGHT_PASS_SEMANTICS", "STATUS_MEANINGS", "TIME_SEMANTICS_REQUIREMENTS",
        "HandoffMaterial", "IntakeHandoffDocument", "IntakeStatus", "MaterialSpec",
        "PackageHandoff", "build_intake_handoff", "intake_handoff_schema",
        "load_intake_handoff", "render_intake_handoff_markdown", "unknown_contract_fields",
    ),
    "intake_plan": (
        "APPROVED_ENTRY_KEYS", "ApprovedListDocument", "INTAKE_PLAN_FILE_NAME",
        "INTAKE_PLAN_HANDOFF_NOTE", "INTAKE_PLAN_KIND", "INTAKE_PLAN_LOCK_SUFFIX",
        "INTAKE_PLAN_NOTE", "INTAKE_PLAN_REPORT_NAME", "INTAKE_PLAN_SCHEMA_VERSION",
        "IntakePlan", "IntakePlanArgumentError", "IntakePlanError",
        "IntakePlanInconsistentError", "IntakePlanPathError", "IntakePlanStateError",
        "IntakePlanStatus", "IntakePlanWriteError", "OPERATOR_EXPLICIT_FLAG",
        "OperatorHandoffStep", "PLAN_EXECUTION_MODE", "PlanEntry", "PlanVerificationCode",
        "PlanViolation", "build_intake_plan", "compute_plan_id", "intake_plan_handoff",
        "load_approved_intake_list", "render_intake_plan_summary", "run_intake_plan",
        "verify_intake_plan_inputs",
    ),
    "intake_receipt": (
        "INTAKE_RECEIPT_FILE_NAME", "INTAKE_RECEIPT_KIND", "INTAKE_RECEIPT_LOCK_SUFFIX",
        "INTAKE_RECEIPT_REPORT_NAME", "INTAKE_RECEIPT_SCHEMA_VERSION",
        "IntakePlanDocument", "IntakeReceipt", "IntakeReceiptArgumentError",
        "IntakeReceiptError", "IntakeReceiptPathError", "IntakeReceiptStateError",
        "IntakeReceiptStatus", "IntakeReceiptVerificationError", "IntakeReceiptWriteError",
        "OPERATOR_MANIFEST_REPORT", "OperatorResult", "PLAN_ENTRY_DOCUMENT_KEYS",
        "PlanEntryDocument", "QUALIFICATION_RECHECK_REPORT", "QualificationRecheck",
        "RECEIPT_EXECUTION_MODE", "ReceiptEntry", "ReceiptVerificationCode",
        "ReceiptViolation", "build_intake_receipt", "compute_receipt_id",
        "load_intake_plan_document", "load_operator_result", "load_qualification_recheck",
        "render_intake_receipt_summary", "run_intake_receipt", "verify_intake_receipt",
    ),
    "ledger": (
        "EvidenceLedger", "ScopeLedger", "ledger_from_raw_json", "load_evidence_ledger",
    ),
    "package_builder": (
        "EVIDENCE_TYPES", "EXIT_INPUT_INVALID", "EXIT_MANIFEST_CONFLICT",
        "EvidencePackageArgumentError", "EvidencePackageCode",
        "EvidencePackageConflictError", "EvidencePackageError",
        "EvidencePackageInputError", "EvidencePackageInternalError",
        "EvidencePackagePathError", "EvidencePackagePreview", "EvidencePackageWriteError",
        "MANIFEST_LOCK_SUFFIX", "MAX_FILE_COUNT", "MAX_NOTES_CHARS",
        "MAX_NOTES_INPUT_CHARS", "MAX_REFERENCE_CHARS", "MAX_SEMANTICS_CHARS",
        "MAX_SOURCE_CHARS", "ManifestFileEntry", "ManifestWriteStatus",
        "PACKAGE_BUILDER_EXECUTION_MODE", "PACKAGE_BUILDER_KIND", "PACKAGE_BUILDER_NOTE",
        "PACKAGE_BUILDER_REPORT_NAME", "PACKAGE_BUILDER_SCHEMA_VERSION", "PREFLIGHT_NOTE",
        "PreflightOutcome", "RowPreflight", "build_manifest_preview", "manifest_bytes",
        "manifest_digest", "preflight_rows", "render_package_summary",
        "run_package_builder",
    ),
    "readiness_runner": (
        "ArtifactWriteError", "DEFAULT_JOURNAL_LIMIT", "EVENTS_FILE_NAME", "EXIT_BLOCKED",
        "EXIT_CONFIG_ERROR", "EXIT_LOCK_CONFLICT", "EXIT_OK", "EXIT_QUALIFICATION_FAILED",
        "EXIT_STATE_INVALID", "EXIT_WORKDIR_UNUSABLE", "LOCK_FILE_NAME", "LOCK_KIND",
        "LockConflictError", "LockInfo", "LockUnavailableError", "QualificationError",
        "RUNNER_NOTE", "RUNNER_REPORT_NAME", "RUNNER_SCHEMA_VERSION", "RunnerError",
        "STATE_FILE_NAME", "STATUS_FILE_NAME", "STATUS_KIND", "SingleInstanceLock",
        "SnapshotBuilder", "TickPaths", "TickReport", "TickStateError", "WorkDirError",
        "build_snapshot_from_session", "exit_code_for", "load_event_journal",
        "render_tick_summary", "run_tick", "write_event_journal", "write_status",
    ),
    "readiness_watch": (
        "ReadinessSnapshot", "SCOPE_STATE_FIELDS", "SNAPSHOT_KIND",
        "SNAPSHOT_SCHEMA_VERSION", "ScopeChange", "ScopeSnapshot", "SnapshotStateError",
        "WATCH_EVENT_KIND", "WATCH_NOTE", "WATCH_REPORT_NAME", "WATCH_SCHEMA_VERSION",
        "WatchEvent", "WatchEventType", "atomic_write_text", "build_snapshot",
        "detect_changes", "load_snapshot_state", "render_watch_summary", "write_events",
        "write_snapshot_state",
    ),
    "rehearsal": (
        "ALLOWED_REPO_SUBDIRS", "EVIDENCE_FILE_NAME", "PACKAGE_DIR_NAME",
        "REHEARSAL_APPROVE_REASON", "REHEARSAL_FIXTURE_NOTE", "REHEARSAL_MATERIAL_REASON",
        "REHEARSAL_OPERATOR_LABEL", "REHEARSAL_PACKAGE_SOURCE", "REHEARSAL_RECHECK_REPORT",
        "REHEARSAL_SCENARIOS", "REHEARSAL_SCOPE", "RehearsalArgumentError", "RehearsalChain",
        "RehearsalError", "RehearsalPathError", "RehearsalScenario", "build_rehearsal_chain",
        "ensure_rehearsal_work_dir",
    ),
    "report": (
        "quarantine_payload", "render_intake_report",
    ),
    "review": (
        "APPROVAL_SCOPE", "APPROVED_KIND", "APPROVED_NOTE",
        "ATTESTATION_BINDING_KEYS", "ATTESTATION_BINDING_VERSION", "ApprovedEntry",
        "ApprovedIntakeList", "DecidedReview", "EXIT_NO_DECISION",
        "FORBIDDEN_EVIDENCE_FIELDS", "InvalidatedApproval", "InvalidationReason",
        "LEDGER_FILE_NAME", "LEDGER_KIND", "LEDGER_LOCK_SUFFIX", "MAX_NOTE_CHARS",
        "MAX_REVIEWER_CHARS", "NEXT_STEP_NOTE", "PACKAGE_SUMMARY_KEYS",
        "REASON_CODES_BY_DECISION", "REVIEW_NOTE", "REVIEW_REPORT_KIND",
        "REVIEW_REPORT_NAME", "REVIEW_SCHEMA_VERSION", "ReviewArgumentError",
        "ReviewAttestationError", "ReviewConflictError", "ReviewDecision", "ReviewError",
        "ReviewLedger", "ReviewLedgerStateError", "ReviewLedgerWriteError",
        "ReviewPathError", "ReviewReasonCode", "ReviewRecord", "ReviewReport",
        "ReviewTargetError",
        "build_approved_intake_list", "compute_decision_id", "load_review_ledger",
        "record_review_decision", "render_review_summary", "run_review",
        "write_approved_intake_list", "write_review_ledger",
    ),
    "templates": (
        "EXAMPLE_MARKER_COLUMNS", "TEMPLATE_FORMATS", "TEMPLATE_ROOT",
        "TEMPLATE_SCHEMA_VERSION", "example_rows", "render_template", "template_columns",
        "template_output_name", "write_template",
    ),
    "submission_readiness": (
        "SUBMISSION_PACK_FILE_NAME", "SUBMISSION_PACK_KIND", "SUBMISSION_PACK_NOTE",
        "SUBMISSION_PACK_SCHEMA_VERSION", "SUBMISSION_PACK_SEMANTICS_NOTE", "ChainBinding",
        "HumanAction", "MaterialRequirement", "ScopeSubmission", "SubmissionCode",
        "SubmissionReadinessPack", "build_submission_pack", "load_submission_pack",
        "render_submission_pack_markdown", "run_submission_pack",
    ),
    "validation": (
        "EvidenceRecord", "NormalizedRow", "RowAssessment", "assess_row",
        "normalize_input_row",
    ),
    "workflow": (
        "OPERATOR_STEPS", "OPERATOR_WORKFLOW_SCHEMA_VERSION", "QuarantineEntry",
        "QuarantineSummary", "WRITE_GATE_NOTE", "WriteGateDecision",
        "build_quarantine_summary", "evaluate_write_gate", "render_quarantine_summary",
        "render_write_gate",
    ),
}

#: 公开名与定义处属性名不同的别名：``公开名 -> (子模块, 属性名)``。
_LAZY_EXPORT_ALIASES: Final[dict[str, tuple[str, str]]] = {
    "decision_packet_exit_code_for": ("decision_packet", "exit_code_for"),
    "inbox_exit_code_for": ("inbox", "exit_code_for"),
    "intake_plan_exit_code_for": ("intake_plan", "exit_code_for"),
    "intake_receipt_exit_code_for": ("intake_receipt", "exit_code_for"),
    "package_builder_exit_code_for": ("package_builder", "exit_code_for"),
    "review_exit_code_for": ("review", "exit_code_for"),
}

#: 惰性导出总表：``公开名 -> (子模块, 属性名)``。
_LAZY_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    **{
        name: (module, name)
        for module, names in _LAZY_EXPORTS_BY_MODULE.items()
        for name in names
    },
    **_LAZY_EXPORT_ALIASES,
}


def __getattr__(name: str) -> Any:
    """PEP 562 惰性导出：语义与旧版 eager ``from ... import ...`` **完全一致**。

    只有**真正被访问**的名字才会 import 其定义子模块，因此包初始化阶段**不会**加载
    整个依赖图，也就不会与其它包形成初始化期循环导入（GOLD-018）。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attribute = target
        value = getattr(importlib.import_module(f"{__name__}.{module_name}"), attribute)
        globals()[name] = value  # 缓存：后续访问不再进入 __getattr__
        return value
    if name in _LAZY_EXPORTS_BY_MODULE:
        # 子模块名同样惰性可见（``from src.evidence import <子模块>`` 等既有用法保持可用）。
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """``dir()`` = 模块字典 ∪ 公开导出（与 eager 版一致）。"""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "ALLOWED_AUTHORIZATION_BASES",
    "ALLOWED_REPO_SUBDIRS",
    "ALL_REQUIRED_VERIFIED_SEMANTICS",
    "APPROVAL_SCOPE",
    "APPROVED_ENTRY_KEYS",
    "APPROVED_KIND",
    "APPROVED_NOTE",
    "ATTESTATION_BINDING_KEYS",
    "ATTESTATION_BINDING_VERSION",
    "ATTESTATION_CONTRACT_VERSION",
    "ATTESTATION_EXECUTION_MODE",
    "ATTESTATION_FILE_NAME",
    "ATTESTATION_KIND",
    "ATTESTATION_NOTE",
    "ATTESTATION_REPORT_NAME",
    "ATTESTATION_SCHEMA_VERSION",
    "ATTESTATION_SCOPE",
    "ATTESTATION_TIME_SEMANTICS_NOTE",
    "ATTESTATION_VERIFICATION_KIND",
    "AUTHOR_CHAIN_NOTE",
    "AUTHOR_CHAIN_SCHEMA_VERSION",
    "ApprovedEntry",
    "ApprovedIntakeList",
    "ApprovedListDocument",
    "ArtifactWriteError",
    "AttestationArgumentError",
    "AttestationCode",
    "AttestationError",
    "AttestationNotAttestableError",
    "AttestationPathError",
    "AttestationStateError",
    "AttestationVerification",
    "AttestationWriteError",
    "AuthorChainReport",
    "AuthorizationDeclaration",
    "BLOCKED_STATUS",
    "CHAIN_AUDIT_CONTRACT_VERSION",
    "CHAIN_AUDIT_EXECUTION_MODE",
    "CHAIN_AUDIT_FILE_NAME",
    "CHAIN_AUDIT_KIND",
    "CHAIN_AUDIT_LOCK_SUFFIX",
    "CHAIN_AUDIT_NOTE",
    "CHAIN_AUDIT_REPORT_NAME",
    "CHAIN_AUDIT_SCHEMA_VERSION",
    "CHAIN_AUDIT_SEMANTICS_NOTE",
    "CHAIN_REHEARSAL_SEMANTICS_NOTE",
    "CHAIN_STAGE_ORDER",
    "CHECK_CATEGORIES",
    "COMMON_REQUIRED_FIELDS",
    "CandidatePackage",
    "ChainAuditArgumentError",
    "ChainAuditCode",
    "ChainAuditError",
    "ChainAuditInputs",
    "ChainAuditPathError",
    "ChainAuditWriteError",
    "ChainBinding",
    "ChainStage",
    "ChainStageResult",
    "ChainStageStatus",
    "ChecklistItem",
    "DECISION_PACKET_FILE_NAME",
    "DECISION_PACKET_KIND",
    "DECISION_PACKET_LOCK_SUFFIX",
    "DECISION_PACKET_NOTE",
    "DECISION_PACKET_REPORT_NAME",
    "DECISION_PACKET_SCHEMA_VERSION",
    "DECISION_RECORD_ACTIONS",
    "DECISION_RECORD_EXECUTION_MODE",
    "DECISION_RECORD_FILE_NAME",
    "DECISION_RECORD_KIND",
    "DECISION_RECORD_LOCK_SUFFIX",
    "DECISION_RECORD_NOTE",
    "DECISION_RECORD_REPORT_NAME",
    "DECISION_RECORD_SCHEMA_VERSION",
    "DECISION_RECORD_TIME_SEMANTICS_NOTE",
    "DECISION_SCOPE",
    "DEFAULT_JOURNAL_LIMIT",
    "DecidedReview",
    "DecisionPacket",
    "DecisionPacketArgumentError",
    "DecisionPacketError",
    "DecisionPacketPathError",
    "DecisionPacketStateError",
    "DecisionPacketStatus",
    "DecisionPacketVerificationError",
    "DecisionPacketWriteError",
    "DecisionRecordArgumentError",
    "DecisionRecordCode",
    "DecisionRecordError",
    "DecisionRecordNotSubmittableError",
    "DecisionRecordPathError",
    "DecisionRecordStateError",
    "DecisionRecordVerification",
    "DecisionRecordVerificationError",
    "DecisionRecordWriteError",
    "EVENTS_FILE_NAME",
    "EVIDENCE_CONTRACT_VERSION",
    "EVIDENCE_FILE_NAME",
    "EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_SOURCES",
    "EVIDENCE_SOURCE_OPERATOR_PROVIDED",
    "EVIDENCE_SOURCE_REHEARSAL_FIXTURE",
    "EVIDENCE_TYPES",
    "EXAMPLE_MARKER_COLUMNS",
    "EXAMPLE_MARKER_FLAG_FIELDS",
    "EXAMPLE_MARKER_KIND_FIELDS",
    "EXAMPLE_MARKER_VALUES",
    "EXIT_BLOCKED",
    "EXIT_CONFIG_ERROR",
    "EXIT_INPUT_INVALID",
    "EXIT_LOCK_CONFLICT",
    "EXIT_MANIFEST_CONFLICT",
    "EXIT_NO_CANDIDATES",
    "EXIT_NO_DECISION",
    "EXIT_OK",
    "EXIT_QUALIFICATION_FAILED",
    "EXIT_STATE_INVALID",
    "EXIT_UNUSABLE",
    "EXIT_WORKDIR_UNUSABLE",
    "EvidenceChainAudit",
    "EvidenceHandoffReport",
    "EvidenceIntakeReport",
    "EvidenceLedger",
    "EvidencePackageArgumentError",
    "EvidencePackageCode",
    "EvidencePackageConflictError",
    "EvidencePackageError",
    "EvidencePackageInputError",
    "EvidencePackageInternalError",
    "EvidencePackagePathError",
    "EvidencePackagePreview",
    "EvidencePackageWriteError",
    "EvidenceRecord",
    "EvidenceScope",
    "ExcludedEvidence",
    "FILE_ENTRY_REQUIRED_FIELDS",
    "FORBIDDEN_EVIDENCE_FIELDS",
    "GatewayAuthorEvidence",
    "HANDOFF_NOTE",
    "HANDOFF_REPORT_NAME",
    "HANDOFF_SCHEMA_VERSION",
    "HandoffDocument",
    "HandoffMaterial",
    "HumanAction",
    "HumanDecision",
    "HumanDecisionRecord",
    "HumanVerificationAttestation",
    "INBOX_NOTE",
    "INBOX_REPORT_KIND",
    "INBOX_REPORT_NAME",
    "INBOX_SCHEMA_VERSION",
    "INTAKE_HANDOFF_CONTRACT_VERSION",
    "INTAKE_HANDOFF_KIND",
    "INTAKE_HANDOFF_LAYOUT_NOTE",
    "INTAKE_HANDOFF_NOTE",
    "INTAKE_HANDOFF_SCHEMA_VERSION",
    "INTAKE_PLAN_FILE_NAME",
    "INTAKE_PLAN_HANDOFF_NOTE",
    "INTAKE_PLAN_KIND",
    "INTAKE_PLAN_LOCK_SUFFIX",
    "INTAKE_PLAN_NOTE",
    "INTAKE_PLAN_REPORT_NAME",
    "INTAKE_PLAN_SCHEMA_VERSION",
    "INTAKE_RECEIPT_FILE_NAME",
    "INTAKE_RECEIPT_KIND",
    "INTAKE_RECEIPT_LOCK_SUFFIX",
    "INTAKE_RECEIPT_REPORT_NAME",
    "INTAKE_RECEIPT_SCHEMA_VERSION",
    "INTAKE_STATUS_ORDER",
    "InboxArtifactWriteError",
    "InboxDirError",
    "InboxError",
    "InboxPreflightReport",
    "InboxReasonCode",
    "InboxStatus",
    "InputFile",
    "InputRow",
    "IntakeCounts",
    "IntakeHandoffDocument",
    "IntakePlan",
    "IntakePlanArgumentError",
    "IntakePlanDocument",
    "IntakePlanError",
    "IntakePlanInconsistentError",
    "IntakePlanPathError",
    "IntakePlanStateError",
    "IntakePlanStatus",
    "IntakePlanWriteError",
    "IntakeReceipt",
    "IntakeReceiptArgumentError",
    "IntakeReceiptError",
    "IntakeReceiptPathError",
    "IntakeReceiptStateError",
    "IntakeReceiptStatus",
    "IntakeReceiptVerificationError",
    "IntakeReceiptWriteError",
    "IntakeStatus",
    "InvalidatedApproval",
    "InvalidationReason",
    "LEDGER_FILE_NAME",
    "LEDGER_KIND",
    "LEDGER_LOCK_SUFFIX",
    "LOCK_FILE_NAME",
    "LOCK_KIND",
    "LockConflictError",
    "LockInfo",
    "LockUnavailableError",
    "MANIFEST_FILE_NAME",
    "MANIFEST_LOCK_SUFFIX",
    "MANIFEST_REQUIRED_FIELDS",
    "MATERIAL_SPECS",
    "MAX_FILE_COUNT",
    "MAX_NOTES_CHARS",
    "MAX_NOTES_INPUT_CHARS",
    "MAX_NOTE_CHARS",
    "MAX_PATH_CHARS",
    "MAX_RECORD_NOTE_CHARS",
    "MAX_RECORD_NOTE_INPUT_CHARS",
    "MAX_RECORD_REASON_CODE_CHARS",
    "MAX_RECORD_REVIEWER_CHARS",
    "MAX_RECORD_REVISION",
    "MAX_REFERENCE_CHARS",
    "MAX_REVIEWER_CHARS",
    "MAX_SEMANTICS_CHARS",
    "MAX_SOURCE_CHARS",
    "ManifestFileEntry",
    "ManifestFileRef",
    "ManifestWriteStatus",
    "MaterialAttestation",
    "MaterialDecision",
    "MaterialRequirement",
    "MaterialSpec",
    "NEXT_STEP_NOTE",
    "NormalizedRow",
    "OPERATOR_EXPLICIT_FLAG",
    "OPERATOR_MANIFEST_REPORT",
    "OPERATOR_STEPS",
    "OPERATOR_WORKFLOW_SCHEMA_VERSION",
    "OperatorHandoffStep",
    "OperatorResult",
    "PACKAGE_BUILDER_EXECUTION_MODE",
    "PACKAGE_BUILDER_KIND",
    "PACKAGE_BUILDER_NOTE",
    "PACKAGE_BUILDER_REPORT_NAME",
    "PACKAGE_BUILDER_SCHEMA_VERSION",
    "PACKAGE_DIR_NAME",
    "PACKAGE_SUMMARY_KEYS",
    "PACKET_CHECKS",
    "PACKET_EXECUTION_MODE",
    "PACKET_NEXT_STEP_NOTE",
    "PACKET_TIME_SEMANTICS_NOTE",
    "PENDING_FILE_NAME",
    "PENDING_HUMAN_REVIEW_STATUS",
    "PENDING_KIND",
    "PENDING_LOCK_SUFFIX",
    "PLAN_ENTRY_DOCUMENT_KEYS",
    "PLAN_EXECUTION_MODE",
    "PREFLIGHT_NOTE",
    "PREFLIGHT_PASS_SEMANTICS",
    "PackageHandoff",
    "PacketBinding",
    "PacketVerificationCode",
    "PacketViolation",
    "PendingEntry",
    "PendingRegister",
    "PendingStateError",
    "PlanEntry",
    "PlanEntryDocument",
    "PlanVerificationCode",
    "PlanViolation",
    "PreflightOutcome",
    "QUALIFICATION_RECHECK_REPORT",
    "QUARANTINE_REASON_CODES",
    "QualificationError",
    "QualificationRecheck",
    "QuarantineEntry",
    "QuarantineSummary",
    "READINESS_CHECK_KEYS",
    "REASON_CODES_BY_DECISION",
    "RECEIPT_ENTRY_KEYS",
    "RECEIPT_EXECUTION_MODE",
    "REHEARSAL_APPROVE_REASON",
    "REHEARSAL_FIXTURE_NOTE",
    "REHEARSAL_MATERIAL_REASON",
    "REHEARSAL_OPERATOR_LABEL",
    "REHEARSAL_PACKAGE_SOURCE",
    "REHEARSAL_RECHECK_REPORT",
    "REHEARSAL_SCENARIOS",
    "REHEARSAL_SCOPE",
    "REVIEWER_KIND",
    "REVIEW_NOTE",
    "REVIEW_REPORT_KIND",
    "REVIEW_REPORT_NAME",
    "REVIEW_SCHEMA_VERSION",
    "RUNNER_NOTE",
    "RUNNER_REPORT_NAME",
    "RUNNER_SCHEMA_VERSION",
    "ReadinessSnapshot",
    "ReadinessStateDocument",
    "ReasonCode",
    "ReceiptDocument",
    "ReceiptEntry",
    "ReceiptVerificationCode",
    "ReceiptViolation",
    "RehearsalArgumentError",
    "RehearsalChain",
    "RehearsalError",
    "RehearsalPathError",
    "RehearsalScenario",
    "ReviewArgumentError",
    "ReviewAttestationError",
    "ReviewConflictError",
    "ReviewDecision",
    "ReviewError",
    "ReviewLedger",
    "ReviewLedgerStateError",
    "ReviewLedgerWriteError",
    "ReviewPathError",
    "ReviewReasonCode",
    "ReviewRecord",
    "ReviewReport",
    "ReviewTargetError",
    "RowAssessment",
    "RowOutcome",
    "RowPreflight",
    "RowStatus",
    "RunnerError",
    "SCOPE_GAP_KEYS",
    "SCOPE_STATE_FIELDS",
    "SNAPSHOT_KIND",
    "SNAPSHOT_SCHEMA_VERSION",
    "STATE_FILE_NAME",
    "STATUS_FILE_NAME",
    "STATUS_KIND",
    "STATUS_MEANINGS",
    "SUBMISSION_PACK_FILE_NAME",
    "SUBMISSION_PACK_KIND",
    "SUBMISSION_PACK_NOTE",
    "SUBMISSION_PACK_SCHEMA_VERSION",
    "SUBMISSION_PACK_SEMANTICS_NOTE",
    "SUPPORTED_FILE_FORMATS",
    "ScopeChange",
    "ScopeGap",
    "ScopeLedger",
    "ScopeSnapshot",
    "ScopeSubmission",
    "SingleInstanceLock",
    "SnapshotBuilder",
    "SnapshotStateError",
    "SubmissionCode",
    "SubmissionReadinessPack",
    "TEMPLATE_FORMATS",
    "TEMPLATE_ROOT",
    "TEMPLATE_SCHEMA_VERSION",
    "TIME_SEMANTICS_REQUIREMENTS",
    "TickPaths",
    "TickReport",
    "TickStateError",
    "VerificationDecision",
    "WATCH_EVENT_KIND",
    "WATCH_NOTE",
    "WATCH_REPORT_NAME",
    "WATCH_SCHEMA_VERSION",
    "WRITE_GATE_NOTE",
    "WatchEvent",
    "WatchEventType",
    "WorkDirError",
    "WriteGateDecision",
    "assess_row",
    "atomic_write_text",
    "attestable_material_keys",
    "attestation_schema",
    "attribute_gateway_author_evidence",
    "audit_evidence_chain",
    "build_approved_intake_list",
    "build_attestation",
    "build_decision_packet",
    "build_decision_record",
    "build_evidence_checklist",
    "build_excluded_evidence",
    "build_handoff_report",
    "build_intake_handoff",
    "build_intake_plan",
    "build_intake_receipt",
    "build_manifest_preview",
    "build_quarantine_summary",
    "build_rehearsal_chain",
    "build_snapshot",
    "build_snapshot_from_session",
    "build_submission_pack",
    "chain_audit_exit_code_for",
    "compute_attestation_id",
    "compute_chain_facts_digest",
    "compute_chain_id",
    "compute_decision_id",
    "compute_packet_id",
    "compute_plan_id",
    "compute_receipt_id",
    "compute_record_id",
    "decision_packet_exit_code_for",
    "decision_record_exit_code_for",
    "detect_changes",
    "ensure_outside_inbox",
    "ensure_rehearsal_work_dir",
    "evaluate_write_gate",
    "example_rows",
    "exit_code_for",
    "gateway_author_evidence",
    "handoff_content_sha256",
    "inbox_exit_code_for",
    "intake_evidence",
    "intake_handoff_schema",
    "intake_plan_exit_code_for",
    "intake_plan_handoff",
    "intake_receipt_exit_code_for",
    "ledger_from_raw_json",
    "load_approved_intake_list",
    "load_attestation_document",
    "load_decision_packet",
    "load_event_journal",
    "load_evidence_ledger",
    "load_gateway_author_evidence",
    "load_handoff_document",
    "load_input_rows",
    "load_intake_handoff",
    "load_intake_plan_document",
    "load_intake_receipt_document",
    "load_operator_result",
    "load_pending_register",
    "load_qualification_recheck",
    "load_readiness_state_document",
    "load_review_ledger",
    "load_snapshot_state",
    "load_submission_pack",
    "load_verification_input",
    "main_audit_exit_code",
    "main_verification_exit_code",
    "manifest_bytes",
    "manifest_digest",
    "normalize_input_row",
    "package_builder_exit_code_for",
    "package_content_sha256",
    "preflight_rows",
    "quarantine_payload",
    "read_input_file",
    "record_review_decision",
    "render_attestation_summary",
    "render_chain_audit_summary",
    "render_decision_packet_summary",
    "render_decision_record_summary",
    "render_handoff_markdown",
    "render_inbox_summary",
    "render_intake_handoff_markdown",
    "render_intake_plan_summary",
    "render_intake_receipt_summary",
    "render_intake_report",
    "render_package_summary",
    "render_quarantine_summary",
    "render_review_summary",
    "render_submission_pack_markdown",
    "render_template",
    "render_tick_summary",
    "render_watch_summary",
    "render_write_gate",
    "required_field_names",
    "resolve_format",
    "review_exit_code_for",
    "run_attestation",
    "run_chain_audit",
    "run_decision_packet",
    "run_decision_record",
    "run_inbox_scan",
    "run_intake_plan",
    "run_intake_receipt",
    "run_package_builder",
    "run_review",
    "run_submission_pack",
    "run_tick",
    "scan_inbox",
    "synthetic_marker_fields",
    "template_columns",
    "template_output_name",
    "unknown_contract_fields",
    "verify_attestation",
    "verify_decision_packet",
    "verify_decision_record",
    "verify_intake_plan_inputs",
    "verify_intake_receipt",
    "write_approved_intake_list",
    "write_event_journal",
    "write_events",
    "write_pending_register",
    "write_review_ledger",
    "write_snapshot_state",
    "write_status",
    "write_template",
]
