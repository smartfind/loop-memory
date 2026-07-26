"""Cheap, explainable classification for distilled wiki knowledge.

The classifier deliberately runs without an LLM.  It is used on write paths,
where a network call would make the dashboard fragile and would turn a local
privacy decision into an external data disclosure.  The result is a
recommendation, never an instruction to override an explicit user scope.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Iterable


@dataclass(frozen=True)
class Classification:
    """Explainable classification and scope recommendation for one page."""

    kind: str
    is_security: bool
    is_universal: bool
    auto_global: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    scope_hint: str = "per-source"
    security_hits: int = 0
    universal_hits: int = 0
    personalization_hits: int = 0
    incident_hits: int = 0
    mode: str = "pattern"

    def to_dict(self) -> dict:
        """Return a JSON-serialisable audit representation."""
        return asdict(self)


_SECURITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("security-domain", r"安全|\bsecurity\b|secure coding"),
    ("credentials", r"凭证|\bcredentials?\b|用户名和密码|账号密码"),
    ("secrets", r"密钥|机密|秘密|\bsecrets?\b|敏感信息|敏感数据"),
    ("passwords", r"密码|口令|\bpasswords?\b|\bpasswd\b|\bpassphrase\b"),
    ("tokens", r"令牌|访问令牌|\btokens?\b|\bbearer\b"),
    ("api-keys", r"api[ _-]?key|access[ _-]?key|api密钥"),
    ("private-keys", r"private[ _-]?key|私钥|ssh key|ssh密钥"),
    ("injection", r"sql[ _-]?injection|sql注入|命令注入|注入攻击|参数化\s*(?:查询|sql)|(?:parameter|parametr)i[sz]ed? (?:sql|quer(?:y|ies))"),
    ("web-vulnerabilities",
     r"\bxss\b|跨站脚本|\bcsrf\b|跨站请求伪造|\bssrf\b|\brce\b|远程代码执行|remote code execution"),
    ("vulnerabilities", r"漏洞|vulnerability|security flaw|安全缺陷|安全漏洞"),
    ("cve", r"\bcve[- ]?\d{4}[- ]?\d{3,}\b"),
    ("authentication", r"鉴权|认证|身份验证|\bauthentication\b|\bauthorization\b|授权|访问控制|access control"),
    ("encryption", r"加密|解密|\bencryption\b|decrypt(?:ion)?|\btls\b|\bhttps\b"),
    ("integrity", r"哈希|散列|\b(?:sha[-_ ]?1|sha[-_ ]?256|sha[-_ ]?512|md5|hmac|bcrypt|argon2|pbkdf2|scrypt)\b|签名校验|完整性校验|\bintegrity\b"),
    ("privacy", r"隐私|个人信息|\bprivacy\b|personal data|\bpii\b|\bgdpr\b"),
    ("compliance", r"合规|\bcompliance\b|监管要求|数据保护"),
    ("incidents", r"泄露|外泄|泄密|leak(?:ed)?|breach|被盗|入侵|phishing|钓鱼|恶意软件|malware"),
    ("least-privilege", r"最小权限|least privilege|principle of least privilege"),
)

_UNIVERSAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("must", r"必须|务必|务须|\bmust\b|\bshall\b"),
    ("never", r"切勿|请勿|绝不要|永远不要|禁止|不要|\bnever\b|do not|don't"),
    ("always", r"始终|总是|\balways\b"),
    ("recommendation", r"推荐|建议|推荐做法|\brecommended\b|\brecommend\b|\bshould\b"),
    ("best-practice", r"最佳实践|安全实践|best[ _-]practice|secure practice"),
    ("universal", r"通用|普适|一般情况下|无论|不论|所有客户端|各客户端|全局|\bglobal\b|universal|generic|regardless|all clients|every client|any user"),
    ("rotation-practice", r"定期轮换|定期更换|rotate(?:d|s)?\s+(?:api[ _-]?keys?|credentials?|secrets?)|quarterly rotation|key rotation"),
    ("parameterized-query", r"参数化\s*(?:查询|sql)|(?:parameter|parametr)i[sz]ed(?:\s+sql)?(?:\s+quer(?:y|ies))?|prepared statements?"),
    ("safe-handling", r"不要粘贴|切勿粘贴|不要提交.*(?:密钥|secret|token)|never paste|do not commit|never commit"),
)

_PERSONAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("first-person", r"\b(?:i|me|my|mine|we|our|私の|我|我的|本人)\b"),
    ("family-detail", r"妻子|老婆|丈夫|老公|家人|孩子|生日|wife|husband|birthday|family"),
    ("specific-date", r"\b(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b"),
    ("issue-or-instance", r"(?:bug|issue|ticket|工单|事故|incident)\s*#?\d{2,}|仅本项目|项目内部|内部项目|this repo|this project|internal only"),
    ("local-path", r"(?:/Users/|/home/|~/|c:\\|d:\\)[^\s]+"),
    ("secret-value", r"(?:api[ _-]?key|access[ _-]?key|token|password|secret|密钥|令牌|密码|口令)\s*(?:is|=|:|：)\s*[A-Za-z0-9_\-/.]{8,}"),
    ("known-secret-format", r"\b(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,})\b"),
)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value if item is not None)
    return str(value)


def _hits(text: str, patterns: Iterable[tuple[str, str]]) -> list[str]:
    return [label for label, pattern in patterns if re.search(pattern, text, re.IGNORECASE)]


def _confidence(
    security_hits: int,
    universal_hits: int,
    personalization_hits: int,
    incident_hits: int,
    auto_global: bool,
) -> float:
    value = 0.38
    value += min(security_hits, 5) * 0.09
    value += min(universal_hits, 4) * 0.08
    value += min(incident_hits, 2) * 0.03
    value -= min(personalization_hits, 3) * 0.16
    if auto_global:
        value += 0.08
    return round(max(0.05, min(0.99, value)), 3)


def pattern_classify(
    title: str = "",
    body: str = "",
    summary: str = "",
    tags: Iterable[object] | None = None,
    evidence_sources: Iterable[object] | None = None,
    *,
    mode: str = "pattern",
) -> Classification:
    """Classify a page using deterministic multilingual patterns.

    ``evidence_sources`` is used only as an explanatory signal.  A page is
    never promoted solely because it appeared in several clients; repeated
    personal information must remain client-scoped.
    """
    selected_mode = str(mode or "pattern").strip().lower()
    if selected_mode not in {"pattern", "llm", "off"}:
        selected_mode = "pattern"
    text = " ".join(
        part for part in (
            _as_text(title),
            _as_text(summary),
            _as_text(body),
            _as_text(tags),
        ) if part
    )
    security_labels = _hits(text, _SECURITY_PATTERNS)
    universal_labels = _hits(text, _UNIVERSAL_PATTERNS)
    personal_labels = _hits(text, _PERSONAL_PATTERNS)
    incident_labels = [label for label in security_labels if label in {"incidents", "cve", "vulnerabilities"}]

    security_hits = len(security_labels)
    universal_hits = len(universal_labels)
    personalization_hits = len(personal_labels)
    incident_hits = len(incident_labels)
    is_security = security_hits > 0
    personal_block = personalization_hits >= 2 or "secret-value" in personal_labels or "known-secret-format" in personal_labels or (
        is_security and personalization_hits >= 1 and "first-person" in personal_labels
    )
    is_universal = universal_hits > 0 and not personal_block

    if selected_mode == "off":
        auto_global = False
    else:
        strong_practice = any(
            label in universal_labels
            for label in ("best-practice", "rotation-practice", "parameterized-query", "safe-handling")
        )
        auto_global = bool(
            is_security
            and not personal_block
            and (
                (security_hits >= 2 and universal_hits >= 1)
                or (security_hits >= 1 and universal_hits >= 2)
                or (security_hits >= 1 and strong_practice)
            )
        )

    if personal_block:
        kind = "personal"
    elif is_security and incident_hits and not auto_global:
        kind = "security-incident"
    elif is_security:
        kind = "security-rule"
    elif is_universal:
        kind = "best-practice"
    else:
        kind = "general"

    reasons: list[str] = []
    reasons.extend(f"security:{label}" for label in security_labels)
    reasons.extend(f"universal:{label}" for label in universal_labels)
    reasons.extend(f"personalization:{label}" for label in personal_labels)
    if evidence_sources:
        known_sources = sorted({str(source).strip().lower() for source in evidence_sources if str(source).strip()})
        if len(known_sources) > 1:
            reasons.append(f"evidence-sources:{','.join(known_sources[:6])}")
    if personal_block:
        reasons.append("personalization-blocks-global")
    if auto_global:
        reasons.append("security-best-practice-is-cross-client")
    elif is_security:
        reasons.append("security-content-needs-source-scope-unless-universal")
    if selected_mode == "llm":
        reasons.append("llm-mode-not-enabled-in-pattern-classifier")

    return Classification(
        kind=kind,
        is_security=is_security,
        is_universal=is_universal,
        auto_global=auto_global,
        confidence=_confidence(
            security_hits,
            universal_hits,
            personalization_hits,
            incident_hits,
            auto_global,
        ),
        reasons=reasons,
        scope_hint="global" if auto_global else "per-source",
        security_hits=security_hits,
        universal_hits=universal_hits,
        personalization_hits=personalization_hits,
        incident_hits=incident_hits,
        mode=selected_mode,
    )


def classify_page(
    title: str = "",
    body: str = "",
    summary: str = "",
    tags: Iterable[object] | None = None,
    evidence_sources: Iterable[object] | None = None,
    mode: str = "pattern",
) -> Classification:
    """Public classifier entry point used by API and consolidation jobs."""
    return pattern_classify(
        title=title,
        body=body,
        summary=summary,
        tags=tags,
        evidence_sources=evidence_sources,
        mode=mode,
    )


__all__ = ["Classification", "classify_page", "pattern_classify"]
