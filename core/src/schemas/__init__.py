"""Pydantic schemas for recruiter-assistant.

Ported from hris ``packages/schemas`` with the review workflow and
JD-Harmonizer/Taleo removed, and aligned to the Phase 0 DDL. Four modules:

* ``jobs`` — job create/update/out + the LLM ``JDExtracted`` extraction schema
* ``resumes`` — resume/cover-letter parse schemas (imports ``Skill`` from jobs)
* ``matching`` — the ranking contract (``MatchWeights``), score/evidence shapes
* ``auth`` — CAS identity (FU-5, ADR-019 §10): ``User`` / ``Session``
* ``audit`` — FU-5 slice 10 (ADR-019 §6/§9.4): ``RevealAuditItem``

The full KEEP public surface is re-exported here so callers can
``from src.schemas import JobCreate, ResumeParsed, MatchWeights``.
"""

from __future__ import annotations

from src.schemas.audit import RevealAuditItem
from src.schemas.auth import Session, User
from src.schemas.jobs import (
    BulkJobResult,
    Education,
    EmploymentType,
    JDExtracted,
    JDExtractText,
    JobCreate,
    JobDeleteOut,
    JobListItem,
    JobOut,
    JobStatus,
    JobTransition,
    JobUpdate,
    RemotePolicy,
    Seniority,
    Skill,
)
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    CoverLetterTheme,
    EvidenceObject,
    EvidenceStatus,
    JobMatchEntry,
    JobMatchResultOut,
    MatchWeights,
    PipelineMeta,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
    SkillCategory,
    SkillContribution,
)
from src.schemas.resumes import (
    Bullet,
    CandidateInfo,
    CoverLetterParsed,
    CoverLetterSentiment,
    EducationItem,
    Experience,
    ResumeChunk,
    ResumeCore,
    ResumeDeleteOut,
    ResumeListItem,
    ResumeOut,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
    ResumeSkillNames,
    ResumeStatus,
    ResumeStatusBreakdown,
    ResumeUploadResult,
    WithdrawRequest,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    # audit
    "RevealAuditItem",
    # auth
    "Session",
    "User",
    # jobs
    "BulkJobResult",
    "Education",
    "EmploymentType",
    "JDExtractText",
    "JDExtracted",
    "JobCreate",
    "JobDeleteOut",
    "JobListItem",
    "JobOut",
    "JobStatus",
    "JobTransition",
    "JobUpdate",
    "RemotePolicy",
    "Seniority",
    "Skill",
    # resumes
    "Bullet",
    "CandidateInfo",
    "CoverLetterParsed",
    "CoverLetterSentiment",
    "EducationItem",
    "Experience",
    "ResumeChunk",
    "ResumeCore",
    "ResumeDeleteOut",
    "ResumeListItem",
    "ResumeOut",
    "ResumeParsed",
    "ResumeSkill",
    "ResumeSkillDetail",
    "ResumeSkillDetails",
    "ResumeSkillNames",
    "ResumeStatus",
    "ResumeStatusBreakdown",
    "ResumeUploadResult",
    "WithdrawRequest",
    # matching
    "CoverLetterEvidence",
    "CoverLetterTheme",
    "EvidenceObject",
    "EvidenceStatus",
    "JobMatchEntry",
    "JobMatchResultOut",
    "MatchWeights",
    "PipelineMeta",
    "RequirementEvidence",
    "ScoreBreakdown",
    "ShortlistEntry",
    "SkillCategory",
    "SkillContribution",
]
