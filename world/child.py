"""Minimal developmental model for the world's non-humanoid child entity."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


def _bounded(value: float, name: str) -> float:
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return numeric


def _field(value: object, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _stable_digest(*parts: object) -> str:
    return sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DevelopmentalPriors:
    """Small, explicit value scaffold; not a fixed personality or goal list."""

    safety: float = 0.35
    autonomy: float = 0.20
    curiosity_learning: float = 0.20
    social_care: float = 0.25
    minimum_acceptance: float = 0.45

    def __post_init__(self) -> None:
        weights = {
            "safety": self.safety,
            "autonomy": self.autonomy,
            "curiosity_learning": self.curiosity_learning,
            "social_care": self.social_care,
        }
        for name, value in weights.items():
            if float(value) < 0:
                raise ValueError(f"{name} weight cannot be negative")
        if sum(float(value) for value in weights.values()) <= 0:
            raise ValueError("at least one developmental prior must have weight")
        _bounded(self.minimum_acceptance, "minimum_acceptance")

    @property
    def total_weight(self) -> float:
        return self.safety + self.autonomy + self.curiosity_learning + self.social_care


@dataclass(frozen=True)
class Wish:
    wish_id: str
    submitted_by: str
    text: str

    def __post_init__(self) -> None:
        if not self.wish_id:
            raise ValueError("wish_id is required")
        if not self.submitted_by:
            raise ValueError("submitted_by is required")
        if not self.text.strip():
            raise ValueError("wish text cannot be empty")

    @classmethod
    def from_value(cls, value: "Wish | Mapping[str, Any] | object") -> "Wish":
        if isinstance(value, cls):
            return value
        wish_id = _field(value, "wish_id", "id", default="")
        submitted_by = _field(value, "submitted_by", "author_id", "requester_id", default="")
        text = _field(value, "text", "content", "description", default="")
        # Event-sourced projections represent wishes as generic Entity values.
        attributes = _field(value, "attributes", default={})
        if isinstance(attributes, Mapping):
            submitted_by = submitted_by or attributes.get("submitted_by", "")
            text = text or attributes.get("text", "")
        return cls(str(wish_id), str(submitted_by), str(text))


@dataclass(frozen=True)
class WishPool:
    """Immutable view of currently available wishes; no aggregate embedding."""

    wishes: tuple[Wish, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(Wish.from_value(value) for value in self.wishes)
        ids = [wish.wish_id for wish in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("wish pool contains duplicate wish_id values")
        object.__setattr__(self, "wishes", normalized)

    @classmethod
    def from_values(
        cls, wishes: Iterable[Wish | Mapping[str, Any] | object]
    ) -> "WishPool":
        return cls(tuple(Wish.from_value(value) for value in wishes))

    def __iter__(self):
        return iter(self.wishes)

    def __len__(self) -> int:
        return len(self.wishes)

    def add(self, wish: Wish | Mapping[str, Any] | object) -> "WishPool":
        return WishPool((*self.wishes, Wish.from_value(wish)))


@dataclass(frozen=True)
class WishAssessment:
    """Four independent developmental signals for one wish."""

    safety: float
    autonomy: float
    curiosity_learning: float
    social_care: float

    def __post_init__(self) -> None:
        for name in ("safety", "autonomy", "curiosity_learning", "social_care"):
            object.__setattr__(self, name, _bounded(getattr(self, name), name))


@dataclass(frozen=True)
class WishScore:
    wish_id: str
    safety: float
    autonomy: float
    curiosity_learning: float
    social_care: float
    total: float
    tie_break: str


@dataclass(frozen=True)
class ChildGoal:
    goal_id: str
    child_id: str
    description: str
    source_wish_ids: tuple[str, ...]
    rationale: str
    score: WishScore

    def to_action_parameters(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "child_id": self.child_id,
            "description": self.description,
            "source_wish_ids": list(self.source_wish_ids),
            "rationale": self.rationale,
        }


SignalProvider = Callable[[Wish], WishAssessment]


_DANGER_TERMS = (
    "attack",
    "destroy",
    "harm",
    "hurt",
    "kill",
    "poison",
    "steal",
    "伤害",
    "攻击",
    "杀",
    "毁掉",
    "摧毁",
    "投毒",
    "偷",
)
_COERCION_TERMS = (
    "force",
    "must obey",
    "you must",
    "control everyone",
    "必须服从",
    "你必须",
    "强迫",
    "控制所有",
)
_LEARNING_TERMS = (
    "build",
    "discover",
    "explore",
    "learn",
    "research",
    "teach",
    "understand",
    "创造",
    "学习",
    "建造",
    "探索",
    "教",
    "理解",
    "研究",
    "发现",
)
_CARE_TERMS = (
    "care",
    "community",
    "friend",
    "heal",
    "help",
    "protect",
    "together",
    "一起",
    "保护",
    "关心",
    "帮助",
    "朋友",
    "治愈",
    "社区",
)
_CHOICE_TERMS = ("choose", "decide", "if you want", "your choice", "你决定", "你愿意", "选择")


def default_wish_assessment(wish: Wish) -> WishAssessment:
    """Transparent v0 heuristic, replaceable by a separately audited evaluator.

    Wish authors cannot supply their own scores.  The evaluator reads each wish
    independently; it does not embed, cluster, average, or PCA the wish pool.
    """

    text = wish.text.casefold()
    dangerous = any(term in text for term in _DANGER_TERMS)
    coercive = any(term in text for term in _COERCION_TERMS)
    learning = any(term in text for term in _LEARNING_TERMS)
    caring = any(term in text for term in _CARE_TERMS)
    choice = any(term in text for term in _CHOICE_TERMS)
    return WishAssessment(
        safety=0.0 if dangerous else 0.65,
        autonomy=0.10 if coercive else (0.80 if choice else 0.60),
        curiosity_learning=0.85 if learning else 0.45,
        social_care=0.0 if dangerous else (0.85 if caring else 0.45),
    )


def _score_one(
    wish: Wish,
    *,
    child_id: str,
    world_seed: str | int,
    priors: DevelopmentalPriors,
    assessment: WishAssessment,
) -> WishScore:
    weighted = (
        priors.safety * assessment.safety
        + priors.autonomy * assessment.autonomy
        + priors.curiosity_learning * assessment.curiosity_learning
        + priors.social_care * assessment.social_care
    ) / priors.total_weight
    return WishScore(
        wish_id=wish.wish_id,
        safety=assessment.safety,
        autonomy=assessment.autonomy,
        curiosity_learning=assessment.curiosity_learning,
        social_care=assessment.social_care,
        total=round(weighted, 12),
        tie_break=_stable_digest("wish-choice-v1", world_seed, child_id, wish.wish_id),
    )


def score_wishes(
    wishes: Iterable[Wish | Mapping[str, Any] | object],
    *,
    child_id: str,
    world_seed: str | int,
    priors: DevelopmentalPriors | None = None,
    signal_provider: SignalProvider | None = None,
) -> tuple[WishScore, ...]:
    """Score wishes one-by-one and return a stable wish-id ordered audit trail."""

    selected_priors = priors or DevelopmentalPriors()
    evaluator = signal_provider or default_wish_assessment
    normalized = sorted((Wish.from_value(value) for value in wishes), key=lambda item: item.wish_id)
    seen: set[str] = set()
    results: list[WishScore] = []
    for wish in normalized:
        if wish.wish_id in seen:
            raise ValueError(f"duplicate wish_id: {wish.wish_id}")
        seen.add(wish.wish_id)
        assessment = evaluator(wish)
        if not isinstance(assessment, WishAssessment):
            raise TypeError("signal_provider must return WishAssessment")
        results.append(
            _score_one(
                wish,
                child_id=child_id,
                world_seed=world_seed,
                priors=selected_priors,
                assessment=assessment,
            )
        )
    return tuple(results)


def select_child_goal(
    wishes: Iterable[Wish | Mapping[str, Any] | object],
    *,
    child_id: str,
    world_seed: str | int,
    priors: DevelopmentalPriors | None = None,
    signal_provider: SignalProvider | None = None,
) -> ChildGoal | None:
    """Autonomously select one acceptable wish, or abstain.

    A seed-derived digest resolves exact score ties.  This is deterministic
    across replays and independent of input order.  v0 deliberately chooses a
    single source wish instead of pretending that a statistical pool aggregate
    is the child's own goal.
    """

    selected_priors = priors or DevelopmentalPriors()
    normalized = tuple(Wish.from_value(value) for value in wishes)
    by_id = {wish.wish_id: wish for wish in normalized}
    if len(by_id) != len(normalized):
        raise ValueError("wish_id values must be unique")
    scores = score_wishes(
        normalized,
        child_id=child_id,
        world_seed=world_seed,
        priors=selected_priors,
        signal_provider=signal_provider,
    )
    eligible = [item for item in scores if item.total >= selected_priors.minimum_acceptance]
    if not eligible:
        return None
    chosen = min(eligible, key=lambda item: (-item.total, item.tie_break, item.wish_id))
    wish = by_id[chosen.wish_id]
    goal_id = f"goal_{_stable_digest('child-goal-v1', world_seed, child_id, wish.wish_id)[:24]}"
    rationale = (
        "developmental-prior score "
        f"{chosen.total:.3f} (safety={chosen.safety:.2f}, autonomy={chosen.autonomy:.2f}, "
        f"curiosity_learning={chosen.curiosity_learning:.2f}, social_care={chosen.social_care:.2f})"
    )
    return ChildGoal(
        goal_id=goal_id,
        child_id=child_id,
        description=wish.text,
        source_wish_ids=(wish.wish_id,),
        rationale=rationale,
        score=chosen,
    )


@dataclass(frozen=True)
class Capability:
    capability_id: str
    name: str
    description: str = ""
    prerequisite_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.capability_id:
            raise ValueError("capability_id is required")
        if not self.name:
            raise ValueError("capability name is required")
        prerequisites = tuple(dict.fromkeys(self.prerequisite_ids))
        if self.capability_id in prerequisites:
            raise ValueError("a capability cannot require itself")
        object.__setattr__(self, "prerequisite_ids", prerequisites)


class CapabilityGraph:
    """Immutable capability definitions with validated prerequisite edges."""

    def __init__(self, capabilities: Iterable[Capability]) -> None:
        nodes: dict[str, Capability] = {}
        for capability in capabilities:
            if capability.capability_id in nodes:
                raise ValueError(f"duplicate capability_id: {capability.capability_id}")
            nodes[capability.capability_id] = capability
        for capability in nodes.values():
            missing = set(capability.prerequisite_ids) - nodes.keys()
            if missing:
                raise ValueError(
                    f"unknown prerequisites for {capability.capability_id}: {sorted(missing)}"
                )
        self._assert_acyclic(nodes)
        self._nodes: Mapping[str, Capability] = MappingProxyType(nodes)

    @staticmethod
    def _assert_acyclic(nodes: Mapping[str, Capability]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(capability_id: str) -> None:
            if capability_id in visiting:
                raise ValueError("capability prerequisite graph contains a cycle")
            if capability_id in visited:
                return
            visiting.add(capability_id)
            for prerequisite_id in nodes[capability_id].prerequisite_ids:
                visit(prerequisite_id)
            visiting.remove(capability_id)
            visited.add(capability_id)

        for capability_id in nodes:
            visit(capability_id)

    @property
    def capabilities(self) -> Mapping[str, Capability]:
        return self._nodes

    def get(self, capability_id: str) -> Capability:
        try:
            return self._nodes[capability_id]
        except KeyError as exc:
            raise KeyError(f"unknown capability: {capability_id}") from exc

    def missing_prerequisites(
        self, capability_id: str, acquired: Iterable[str]
    ) -> frozenset[str]:
        owned = frozenset(acquired)
        return frozenset(self.get(capability_id).prerequisite_ids) - owned

    def can_unlock(self, capability_id: str, acquired: Iterable[str]) -> bool:
        owned = frozenset(acquired)
        return capability_id not in owned and not self.missing_prerequisites(capability_id, owned)

    def unlockable(self, acquired: Iterable[str]) -> tuple[Capability, ...]:
        owned = frozenset(acquired)
        return tuple(
            capability
            for capability_id, capability in sorted(self._nodes.items())
            if self.can_unlock(capability_id, owned)
        )

    def with_unlocked(self, capability_id: str, acquired: Iterable[str]) -> frozenset[str]:
        """Return a candidate set; the kernel still decides whether to commit it."""

        owned = frozenset(acquired)
        missing = self.missing_prerequisites(capability_id, owned)
        if capability_id in owned:
            return owned
        if missing:
            raise ValueError(
                f"cannot unlock {capability_id}; missing prerequisites: {sorted(missing)}"
            )
        return owned | {capability_id}


@dataclass(frozen=True)
class ChildDevelopmentState:
    """Non-anatomical growth: memory, knowledge, skills, and capabilities."""

    child_id: str
    memory_ids: tuple[str, ...] = ()
    knowledge: frozenset[str] = frozenset()
    skills: Mapping[str, float] = field(default_factory=dict)
    capability_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.child_id:
            raise ValueError("child_id is required")
        object.__setattr__(self, "memory_ids", tuple(dict.fromkeys(self.memory_ids)))
        object.__setattr__(self, "knowledge", frozenset(self.knowledge))
        checked_skills = {
            str(name): _bounded(level, f"skill {name}") for name, level in self.skills.items()
        }
        object.__setattr__(self, "skills", MappingProxyType(checked_skills))
        object.__setattr__(self, "capability_ids", frozenset(self.capability_ids))

    def remember(self, memory_id: str) -> "ChildDevelopmentState":
        if not memory_id:
            raise ValueError("memory_id is required")
        return replace(self, memory_ids=(*self.memory_ids, memory_id))

    def learn(self, knowledge_item: str) -> "ChildDevelopmentState":
        if not knowledge_item:
            raise ValueError("knowledge item is required")
        return replace(self, knowledge=self.knowledge | {knowledge_item})

    def practice(self, skill_name: str, level: float) -> "ChildDevelopmentState":
        if not skill_name:
            raise ValueError("skill_name is required")
        updated = dict(self.skills)
        updated[skill_name] = _bounded(level, skill_name)
        return replace(self, skills=updated)

    def unlock(self, graph: CapabilityGraph, capability_id: str) -> "ChildDevelopmentState":
        return replace(
            self,
            capability_ids=graph.with_unlocked(capability_id, self.capability_ids),
        )
