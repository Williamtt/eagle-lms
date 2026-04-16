# =============================================================================
# questionnaire_definitions.py
# EAGLE-LMS 問卷定義模組
#
# 版本管理規則：
#   - 修改任何題目措辭 → 更新問卷 version，在 VERSION_LOG 新增紀錄
#   - 學期中已有學生作答後，不可修改 item_code 或題目文字
#   - 前後測（pre/post）的題目必須完全一致，才能進行配對分析
# =============================================================================

QUESTIONNAIRE_VERSION_LOG = [
    {
        "version": "1.1.0",
        "date": "2026-04-16",
        "semester": "114-1",
        "changes": (
            "滿意度問卷新增「小組合作學習」活動項目（sat_imp_group_learning / sat_sat_group_learning），"
            "以及開放式題目 sat_open_group（回顧最有價值的小組討論並提出改進建議），"
            "使問卷能評估小組合作學習機制的感知重要性與學習效果。"
        ),
    },
    {
        "version": "1.0.0",
        "date": "2026-04-02",
        "semester": "114-1",
        "changes": (
            "初版：ARCSA 學習動機問卷（前後測各 25 題，5 構面）、"
            "課程活動滿意度與重要性問卷（6 活動 x 2 維度）。"
            "ARCSA 基於 Keller(1983) ARCS 模式，加入 Accountability 構面，"
            "形成 ARCSA 模式（計畫書表 10）。"
        ),
    },
]

# =============================================================================
# Likert 量表選項（共用）
# =============================================================================

LIKERT5_CHOICES = [
    {"value": "1", "label": "非常不同意"},
    {"value": "2", "label": "不同意"},
    {"value": "3", "label": "普通"},
    {"value": "4", "label": "同意"},
    {"value": "5", "label": "非常同意"},
]

IMPORTANCE_CHOICES = [
    {"value": "1", "label": "非常不重要"},
    {"value": "2", "label": "不重要"},
    {"value": "3", "label": "普通"},
    {"value": "4", "label": "重要"},
    {"value": "5", "label": "非常重要"},
]

SATISFACTION_CHOICES = [
    {"value": "1", "label": "非常不滿意"},
    {"value": "2", "label": "不滿意"},
    {"value": "3", "label": "普通"},
    {"value": "4", "label": "滿意"},
    {"value": "5", "label": "非常滿意"},
]

# =============================================================================
# ARCSA 題目定義（前後測共用相同題目，item_code 相同）
# 構面：attention / relevance / confidence / satisfaction / accountability
# =============================================================================

_ARCSA_ITEMS = [
    # ── Attention（引起注意）5 題 ──
    {
        "item_code": "arcsa_a1",
        "dimension": "attention",
        "dimension_label": "引起注意",
        "text": "在課堂活動中，講師安排的專案模擬或實務案例能有效吸引我的注意力。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 1,
    },
    {
        "item_code": "arcsa_a2",
        "dimension": "attention",
        "dimension_label": "引起注意",
        "text": "在完成學習任務（如施工日誌填寫或進度計算）時，我對解決問題感到好奇且感興趣。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 2,
    },
    {
        "item_code": "arcsa_a3",
        "dimension": "attention",
        "dimension_label": "引起注意",
        "text": "教師提出的問題與挑戰促使我集中注意力並深入思考。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 3,
    },
    {
        "item_code": "arcsa_a4",
        "dimension": "attention",
        "dimension_label": "引起注意",
        "text": "課堂上結合案例討論與實務練習的方式令我感到新奇且具有吸引力。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 4,
    },
    {
        "item_code": "arcsa_a5",
        "dimension": "attention",
        "dimension_label": "引起注意",
        "text": "教學內容的呈現方式（如多媒體教材、範例分析）能持續引發我的學習興趣。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 5,
    },
    # ── Relevance（切身相關）5 題 ──
    {
        "item_code": "arcsa_r1",
        "dimension": "relevance",
        "dimension_label": "切身相關",
        "text": "我認為課程設計的內容與未來工程職場的實務需求高度相關。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 6,
    },
    {
        "item_code": "arcsa_r2",
        "dimension": "relevance",
        "dimension_label": "切身相關",
        "text": "課堂中進行的專案模擬與案例練習，幫助我理解營建管理的核心概念。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 7,
    },
    {
        "item_code": "arcsa_r3",
        "dimension": "relevance",
        "dimension_label": "切身相關",
        "text": "課堂任務與我之前學過的專業知識有清楚的連結，幫助我整合知識。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 8,
    },
    {
        "item_code": "arcsa_r4",
        "dimension": "relevance",
        "dimension_label": "切身相關",
        "text": "在課堂學習中，我能感受到這些技能和知識對未來工作的應用價值。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 9,
    },
    {
        "item_code": "arcsa_r5",
        "dimension": "relevance",
        "dimension_label": "切身相關",
        "text": "教師的引導與學習資源（如教材、範例）幫助我理解學習內容與實務的切身性。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 10,
    },
    # ── Confidence（建立信心）5 題 ──
    {
        "item_code": "arcsa_c1",
        "dimension": "confidence",
        "dimension_label": "建立信心",
        "text": "在完成課堂學習任務的過程中，我覺得自己能成功解決課堂中提出的問題。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 11,
    },
    {
        "item_code": "arcsa_c2",
        "dimension": "confidence",
        "dimension_label": "建立信心",
        "text": "通過進度計算或專案模擬練習，我對自己的能力更具信心。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 12,
    },
    {
        "item_code": "arcsa_c3",
        "dimension": "confidence",
        "dimension_label": "建立信心",
        "text": "小組合作與課堂討論讓我相信自己能與他人協作完成專案任務。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 13,
    },
    {
        "item_code": "arcsa_c4",
        "dimension": "confidence",
        "dimension_label": "建立信心",
        "text": "教師或業師的指導與課堂活動幫助我更有信心完成學習挑戰。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 14,
    },
    {
        "item_code": "arcsa_c5",
        "dimension": "confidence",
        "dimension_label": "建立信心",
        "text": "在學習過程中，我能逐漸相信自己可以達成學習目標並取得良好成果。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 15,
    },
    # ── Satisfaction（獲得滿足）5 題 ──
    {
        "item_code": "arcsa_s1",
        "dimension": "satisfaction",
        "dimension_label": "獲得滿足",
        "text": "完成課堂中的學習任務（如日誌填寫或問題解決）後，我感到滿足且有成就感。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 16,
    },
    {
        "item_code": "arcsa_s2",
        "dimension": "satisfaction",
        "dimension_label": "獲得滿足",
        "text": "學習過程中，我能感受到自己的努力被教師或同學所認可。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 17,
    },
    {
        "item_code": "arcsa_s3",
        "dimension": "satisfaction",
        "dimension_label": "獲得滿足",
        "text": "我覺得課程所設計的學習任務能有效應用所學知識，這讓我感到滿意。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 18,
    },
    {
        "item_code": "arcsa_s4",
        "dimension": "satisfaction",
        "dimension_label": "獲得滿足",
        "text": "教師的回饋幫助我確認學習進步，並對學習成果感到滿足。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 19,
    },
    {
        "item_code": "arcsa_s5",
        "dimension": "satisfaction",
        "dimension_label": "獲得滿足",
        "text": "在完成課堂活動後，我感到學習內容對未來實務工作有實質幫助，這提升了我的學習動力。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 20,
    },
    # ── Accountability（必須當責）5 題 ──
    {
        "item_code": "arcsa_ac1",
        "dimension": "accountability",
        "dimension_label": "必須當責",
        "text": "我會主動為自己完成的工作進行品質自我檢核，確保符合專業要求。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 21,
    },
    {
        "item_code": "arcsa_ac2",
        "dimension": "accountability",
        "dimension_label": "必須當責",
        "text": "當遇到問題或困難時，我願意立即尋求解決方案，而不是等待他人指示。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 22,
    },
    {
        "item_code": "arcsa_ac3",
        "dimension": "accountability",
        "dimension_label": "必須當責",
        "text": "在團隊合作中，我能夠明確承擔自己的責任，並確保如期完成分內工作。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 23,
    },
    {
        "item_code": "arcsa_ac4",
        "dimension": "accountability",
        "dimension_label": "必須當責",
        "text": "我會認真思考每個決策可能帶來的後果，並為自己的決定負責。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 24,
    },
    {
        "item_code": "arcsa_ac5",
        "dimension": "accountability",
        "dimension_label": "必須當責",
        "text": "當發現錯誤或疏失時，我願意立即承認並提出改善方案。",
        "scale_type": "likert5",
        "choices": LIKERT5_CHOICES,
        "order": 25,
    },
]

# =============================================================================
# 課程活動滿意度與重要性問卷題目
# 6 項活動 × 2 維度（重要性 + 滿意度）= 12 題
# =============================================================================

_SATISFACTION_ACTIVITIES = [
    {"code": "expert_lecture",   "label": "專家業師演講與授課"},
    {"code": "site_visit",       "label": "案例工地參觀"},
    {"code": "eagle_tasks",      "label": "EAGLE 自主學習任務（任務 1–4）"},
    {"code": "ai_assistant",     "label": "AI 助教（任務即時回饋）"},
    {"code": "task_handbook",    "label": "任務手冊（自主學習指引）"},
    {"code": "learning_journal", "label": "學習日誌撰寫"},
    {"code": "group_learning",   "label": "小組合作學習（組員互動與討論）"},
]

_SATISFACTION_ITEMS = []
_order = 1
for act in _SATISFACTION_ACTIVITIES:
    _SATISFACTION_ITEMS.append({
        "item_code": f"sat_imp_{act['code']}",
        "dimension": "importance",
        "dimension_label": "重要性",
        "activity": act["label"],
        "text": f"【{act['label']}】這項課程活動對我的學習而言是重要的。",
        "scale_type": "likert5",
        "choices": IMPORTANCE_CHOICES,
        "order": _order,
    })
    _order += 1
    _SATISFACTION_ITEMS.append({
        "item_code": f"sat_sat_{act['code']}",
        "dimension": "satisfaction",
        "dimension_label": "滿意度",
        "activity": act["label"],
        "text": f"【{act['label']}】我對這項課程活動的學習體驗感到滿意。",
        "scale_type": "likert5",
        "choices": SATISFACTION_CHOICES,
        "order": _order,
    })
    _order += 1

# 加一題小組學習開放式
_SATISFACTION_ITEMS.append({
    "item_code": "sat_open_group",
    "dimension": "open",
    "dimension_label": "小組學習回饋",
    "activity": "小組合作學習",
    "text": (
        "回顧整個學期的小組互動：哪一次討論對你的學習最有幫助？"
        "如果你是課程設計者，你會如何調整分組學習的方式，讓它對學習更有幫助？"
    ),
    "scale_type": "text",
    "choices": [],
    "order": _order,
})
_order += 1

# 加一題整體開放式
_SATISFACTION_ITEMS.append({
    "item_code": "sat_open1",
    "dimension": "open",
    "dimension_label": "開放回饋",
    "activity": "",
    "text": "整體而言，你認為哪項課程活動對你的學習幫助最大？原因是什麼？",
    "scale_type": "text",
    "choices": [],
    "order": _order,
})


# =============================================================================
# 問卷定義字典（供 db seed 使用）
# =============================================================================

QUESTIONNAIRES = {
    "arcsa_pre": {
        "name": "ARCSA 學習動機前測問卷",
        "description": (
            "本問卷評估你在課程開始時對學習的動機狀態，包含引起注意、切身相關、"
            "建立信心、獲得滿足及必須當責五個構面，共 25 題。"
            "請根據你目前的真實感受作答，沒有對錯之分。"
        ),
        "version": "1.0.0",
        "semester": "114-1",
        "timing": "學期初（第 1–2 週）",
        "items": _ARCSA_ITEMS,
    },
    "arcsa_post": {
        "name": "ARCSA 學習動機後測問卷",
        "description": (
            "本問卷評估你在完成課程任務後的學習動機狀態，題目與前測相同。"
            "請根據你現在的真實感受作答，與前測時的答案可以相同也可以不同。"
        ),
        "version": "1.0.0",
        "semester": "114-1",
        "timing": "學期末（第 16–17 週）",
        "items": _ARCSA_ITEMS,
    },
    "satisfaction": {
        "name": "課程活動滿意度與重要性問卷",
        "description": (
            "本問卷調查你對各項課程活動的重要性感知與滿意程度，"
            "用於評估教學設計的成效，作為未來課程改進的依據。共 16 題。"
        ),
        "version": "1.1.0",
        "semester": "114-1",
        "timing": "學期末（第 16–17 週）",
        "items": _SATISFACTION_ITEMS,
    },
}
