"""AI Token 額度服務（v2.8.0）

- 月份計算固定 Asia/Taipei（避免 UTC 跨日跨月混亂）
- 預設月 cap：5,000,000 tokens（可由 AI_DRAFT_MONTHLY_TOKEN_CAP 覆寫）
- Soft ceiling：80% 即拒絕新自動預生（保留 20% 給教師單筆 fallback）
- Race condition：接受 soft cap 小誤差；20% buffer 吸收
- AIUsageLog 永久保留（Codex 確認：1500–5000 筆/學期可接受）
"""

from __future__ import annotations
import os
from datetime import datetime
from typing import Tuple, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from sqlalchemy import func

from models import db, AIUsageLog, AIQuotaOverride

TZ = ZoneInfo('Asia/Taipei')
DEFAULT_CAP = int(os.environ.get('AI_DRAFT_MONTHLY_TOKEN_CAP', '5000000'))
SOFT_CEILING_RATIO = 0.80   # 達 80% 拒絕新自動預生


def current_period() -> str:
    """回傳 'YYYY-MM' 格式的本期（Asia/Taipei）。"""
    return datetime.now(TZ).strftime('%Y-%m')


def used_tokens(period: Optional[str] = None) -> int:
    """彙整某期 input_tokens + output_tokens。失敗 call 也算（避免 retry 風暴）。"""
    p = period or current_period()
    row = db.session.query(
        func.coalesce(func.sum(AIUsageLog.input_tokens + AIUsageLog.output_tokens), 0)
    ).filter(AIUsageLog.period == p).scalar()
    return int(row or 0)


def cap_for_period(period: Optional[str] = None) -> int:
    """DEFAULT_CAP + Σ AIQuotaOverride.extra_tokens（該期）。"""
    p = period or current_period()
    extra = db.session.query(
        func.coalesce(func.sum(AIQuotaOverride.extra_tokens), 0)
    ).filter(AIQuotaOverride.period == p).scalar()
    return DEFAULT_CAP + int(extra or 0)


def can_call(period: Optional[str] = None) -> Tuple[bool, str]:
    """檢查是否允許新自動預生呼叫。
    回傳 (allowed, reason)。allowed=False 時 reason 給前端顯示。

    Soft ceiling：用量達 cap × 80% 即拒絕；保留 20% buffer 給教師單筆 fallback。
    """
    p = period or current_period()
    used = used_tokens(p)
    cap = cap_for_period(p)
    ceiling = int(cap * SOFT_CEILING_RATIO)
    if used >= ceiling:
        return (False,
                f'本月 AI 用量 {used:,} 已達 soft ceiling {ceiling:,}'
                f'（cap {cap:,} 的 {int(SOFT_CEILING_RATIO * 100)}%），'
                f'自動預生暫停。請至 /teacher/settings/ai-quota 核准追加。')
    return (True, '')


def record_call(*,
                purpose: str,
                model_used: str,
                input_tokens: int,
                output_tokens: int,
                task_submission_id: Optional[int] = None,
                user_id: Optional[int] = None,
                success: bool = True,
                error_message: str = '',
                commit: bool = True) -> AIUsageLog:
    """寫入 AIUsageLog。成功與失敗都要寫（追蹤失敗率）。

    commit=True 時立即寫；False 時讓呼叫端統一 commit（用於 batch worker）。
    """
    log = AIUsageLog(
        period             = current_period(),
        purpose            = purpose,
        model_used         = model_used or '',
        input_tokens       = int(input_tokens or 0),
        output_tokens      = int(output_tokens or 0),
        task_submission_id = task_submission_id,
        user_id            = user_id,
        success            = bool(success),
        error_message      = (error_message or '')[:500],
    )
    db.session.add(log)
    if commit:
        db.session.commit()
    return log


def status_summary(period: Optional[str] = None) -> dict:
    """給 dashboard chip 與 quota 頁用。"""
    p = period or current_period()
    used = used_tokens(p)
    cap = cap_for_period(p)
    ratio = (used / cap) if cap else 0.0
    return {
        'period':       p,
        'used':         used,
        'cap':          cap,
        'ratio':        round(ratio, 4),
        'soft_ceiling': int(cap * SOFT_CEILING_RATIO),
        'over_ceiling': used >= int(cap * SOFT_CEILING_RATIO),
    }
