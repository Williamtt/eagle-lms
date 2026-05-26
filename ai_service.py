import json
import os
from anthropic import Anthropic

# Task context for AI grading
TASK_CONTEXT = {
    1: {
        "name": "專案建立",
        "learning_goals": [
            "理解工程專案基本資料的組成要素",
            "理解 PCCES 契約工項（詳細價目表）的結構與匯入方式",
            "了解預定進度計畫在系統中的角色（S 曲線基線）",
            "建立開工前準備工作完整性的責任意識"
        ],
        "accountability_focus": "專案建立的準確性與完整性"
    },
    2: {
        "name": "施工日誌填寫",
        "learning_goals": [
            "理解監造日報表的格式與填寫要求",
            "從歷史資料擷取關鍵訊息",
            "透過結構化填寫累積實際進度",
            "體驗監造工程師的觀察視角",
            "建立施工日誌真實性與正確性的責任意識",
            "培養按時填報與自我檢核的習慣"
        ],
        "accountability_focus": "日報表的真實性、準確性與按時填報"
    },
    3: {
        "name": "工程排程判讀、WBS 建模與進度分析",
        "learning_goals": [
            "能判讀綱要進度甘特圖，理解其與傳統 CPM 網狀圖的差異",
            "能從 BOQ 建立 3–4 層 WBS，並說明 WBS 與 BOQ 的組織邏輯差異",
            "能從 WBS 工作包定義可排程作業，建立作業定義表",
            "能依四步驟排程計畫建立簡化排程模型，完成 CPM 前推/後推計算",
            "能正確區分 EVM 三種資料來源：PV 來自進度曲線表、"
            "EV 來自 EAGLE 2.0 進度表正式確認累計實際 %、"
            "AC 以估驗計價累計金額作為代理值",
            "能說明監造報表在 EVM 中的角色（參考依據，非直接等於 EV）",
            "能計算 PV、EV、AC、SV、CV、SPI、CPI，並提出進度管理建議",
            "能分析變更設計對 WBS、CPM 要徑與 BAC 的影響",
        ],
        "accountability_focus": "排程假設的透明度、資料來源的正確性、管理建議的專業度"
    },
    4: {
        "name": "自主查核表填寫",
        "learning_goals": [
            "理解三級品管制度",
            "認識施工抽查程序",
            "學習自主查核表的填寫方法",
            "在 EAGLE 2.0 中匯入檢查樣板並操作品質查驗",
            "建立施工品質零容忍的監造態度"
        ],
        "accountability_focus": "品質查驗的專業判斷與責任意識"
    }
}


def get_client():
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def _extract_usage(message, default_model: str = '') -> dict:
    """從 Anthropic message response 取出 usage 資訊。
    回傳 dict 給 ai_grading.record_call 用。失敗回 {}。"""
    if not message:
        return {}
    try:
        u = getattr(message, 'usage', None)
        return {
            'input_tokens':  int(getattr(u, 'input_tokens', 0)) if u else 0,
            'output_tokens': int(getattr(u, 'output_tokens', 0)) if u else 0,
            'model':         getattr(message, 'model', '') or default_model,
        }
    except Exception:
        return {'input_tokens': 0, 'output_tokens': 0, 'model': default_model}


def generate_instant_feedback(task_number, submission_type, content, student_name=""):
    """Generate immediate AI feedback for student submission."""
    client = get_client()
    if not client:
        return {"feedback": "（AI 回饋功能尚未啟用，請等待教師設定 API 金鑰。）", "scores": {}}

    task = TASK_CONTEXT.get(task_number, {})
    task_name = task.get("name", f"任務{task_number}")
    goals = "\n".join(f"- {g}" for g in task.get("learning_goals", []))
    acc_focus = task.get("accountability_focus", "")

    system_prompt = f"""你是一位營建管理課程的 AI 教學助教，負責協助評閱學生在「磺港溪再造步道整建工程」EAGLE 2.0 工程專案管理系統自主學習任務中的作業。

## 你的角色
- 提供建設性的初步回饋，幫助學生改進。
- 你的回饋是「輔助性質」，最終評分由教師決定。
- 語氣親切但專業，像一位資深學長姐在指導學弟妹。
- 使用繁體中文回答。

## 當前任務資訊
- 任務名稱：{task_name}
- 提交類型：{submission_type}
- 學習目標：
{goals}
- 當責重點：{acc_focus}

## 回饋要求
1. **內容完整性**：學生是否涵蓋了任務要求的所有面向？
2. **專業準確性**：專業用語、概念描述是否正確？
3. **反思深度**：學生的反思是否超越表面，展現真正的理解？
4. **當責態度**：從回答中能否看出學生對專業責任的認識？

請用以下 JSON 格式回覆：
{{
  "feedback": "你的詳細回饋文字（300-500字，使用段落而非條列）",
  "scores": {{
    "completeness": 1-5,
    "accuracy": 1-5,
    "reflection_depth": 1-5,
    "accountability": 1-5
  }},
  "highlights": "學生回答中最好的部分（50字以內）",
  "suggestions": "最需要改進的一點建議（50字以內）"
}}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"以下是學生 {student_name} 提交的{submission_type}內容：\n\n{content}"
            }]
        )
        raw_text = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith('```'):
            lines = raw_text.split('\n')
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith('```')]
            raw_text = '\n'.join(lines).strip()
        result = json.loads(raw_text)
        result['_usage'] = _extract_usage(message, "claude-sonnet-4-5")
        return result
    except json.JSONDecodeError:
        # If response isn't valid JSON, return the raw text as feedback
        raw = message.content[0].text if message else "AI 回饋生成失敗"
        # Try to extract feedback from partial JSON
        return {
            "feedback": raw,
            "scores": {},
            "_usage": _extract_usage(message, "claude-sonnet-4-5") if message else {},
        }
    except Exception as e:
        return {"feedback": f"AI 回饋生成時發生錯誤：{str(e)}", "scores": {},
                "_error": str(e), "_usage": {}}


def generate_teacher_analysis(submissions_data):
    """Generate class-level analytics report for teacher."""
    client = get_client()
    if not client:
        return "AI 分析功能尚未啟用。"

    system_prompt = """你是一位營建管理課程的教學分析助理。請根據全班學生的提交資料，產生一份教學分析報告。

## 分析要點
1. **整體概況**：全班的提交狀況、完成度統計。
2. **共同優點**：多數學生表現良好的面向。
3. **共同問題**：多數學生容易犯的錯誤或遺漏。
4. **當責態度分析**：從反思內容中觀察學生對專業責任的認識程度。
5. **教學建議**：根據分析結果，建議教師在下次課堂中可以加強的內容。
6. **值得關注的學生**：表現特別優異或可能需要額外協助的學生（不需點名，以學號代稱）。

請用繁體中文、以段落方式撰寫（不要用條列式），約 500-800 字。"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"以下是全班學生的提交資料彙整：\n\n{json.dumps(submissions_data, ensure_ascii=False, indent=2)}"
            }]
        )
        return message.content[0].text
    except Exception as e:
        return f"分析報告生成失敗：{str(e)}"


def generate_review_suggestion(submission_content, task_number, submission_type):
    """Generate grading suggestion for teacher review."""
    client = get_client()
    if not client:
        return {"suggestion": "AI 建議功能尚未啟用。", "suggested_score": None}

    task = TASK_CONTEXT.get(task_number, {})
    task_name = task.get("name", f"任務{task_number}")

    system_prompt = f"""你是協助教師批改的 AI 助手。請針對學生提交的「{task_name}」{submission_type}，提供批改建議。

請以 JSON 格式回覆：
{{
  "suggestion": "給教師的批改建議（200字以內，指出值得肯定的地方和需要改進之處）",
  "suggested_score": 一個 0-100 的建議分數,
  "rubric_notes": "依據評分標準的簡要說明"
}}"""

    message = None
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"學生提交內容：\n\n{submission_content}"
            }]
        )
        raw = message.content[0].text.strip()
        # 有時 Claude 會在 JSON 前後加 markdown code fence，先移除
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        result['_usage'] = _extract_usage(message, "claude-sonnet-4-5")
        return result
    except Exception as e:
        print(f"[ai_service] generate_review_suggestion error: {e}")
        return {"suggestion": f"AI 建議生成失敗：{e}", "suggested_score": None,
                "_error": str(e),
                "_usage": _extract_usage(message, "claude-sonnet-4-5") if message else {}}


def generate_self_study_rubric_suggestion(proposal_text: str, axes: list, axes_desc: dict) -> dict:
    """為對照組自主學習成果產生 Rubric 建議分數（每軸 1–5）。"""
    client = get_client()
    if not client:
        return {"error": "ai_disabled", "rubric_scores": {}, "comment": ""}

    axes_block = '\n'.join(
        f'- {ax}：{axes_desc.get(ax, ax)}' for ax in axes
    )
    system_prompt = f"""你是協助教師評閱學生自主學習成果的 AI 助手。
請依據以下評分向度，對學生的成果報告給出建議分數（各 1–5 分）。

評分向度：
{axes_block}

評分標準：1=明顯不足 / 2=有待加強 / 3=基本達標 / 4=良好 / 5=優秀

請以 JSON 格式回覆，僅回傳 JSON，不要其他文字：
{{
  "rubric_scores": {{{', '.join(f'"{ax}": 整數1到5' for ax in axes)}}},
  "comment": "給教師參考的整體評語（100字以內）"
}}"""

    message = None
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": f"學生成果內容：\n\n{proposal_text}"}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        # 確保分數在 1–5 範圍內
        for ax in axes:
            v = result.get('rubric_scores', {}).get(ax)
            if v is not None:
                result['rubric_scores'][ax] = max(1, min(5, int(v)))
        result['_usage'] = _extract_usage(message, "claude-sonnet-4-6")
        return result
    except Exception as e:
        print(f"[ai_service] generate_self_study_rubric_suggestion error: {e}")
        return {"error": str(e), "rubric_scores": {}, "comment": f"AI 建議生成失敗：{e}",
                "_error": str(e),
                "_usage": _extract_usage(message, "claude-sonnet-4-6") if message else {}}


def generate_self_study_grading(plan_text: str, result_text: str,
                                axes: list, axes_desc: dict) -> dict:
    """為對照組自主學習成果產生 AI 預批草稿（建議性質，教師為最終權威）。

    評分依據明確分兩面：
      1. 計畫 vs 成果相符度：學生最後成果是否落實了當初提出的自學計畫。
      2. 內容品質：成果本身的專業正確性、完整性與深度。

    參數：
      plan_text   學生最初的自學計畫（主題/動機目標/預期成果/時程）
      result_text 學生提交的成果（成果說明＋反思）
    回傳 {'rubric_scores': {軸:1-5}, 'comment': str, '_usage': {...}}；
    失敗回 {'error': str, 'rubric_scores': {}, 'comment': str, ...}。
    """
    client = get_client()
    if not client:
        return {"error": "ai_disabled", "rubric_scores": {}, "comment": ""}

    axes_block = '\n'.join(f'- {ax}：{axes_desc.get(ax, ax)}' for ax in axes)
    system_prompt = f"""你是協助教師評閱學生自主學習成果的 AI 助手。你的評分為「建議草稿」，最終評分由教師覆核決定。

請依兩個面向評分：
（一）計畫 vs 成果相符度：學生最後提交的成果，是否確實落實了他最初提出的自學計畫（主題、動機目標、預期成果）。若成果偏離計畫或明顯縮水，相符度低。
（二）內容品質：成果本身的專業正確性、完整性與反思深度。

評分向度：
{axes_block}

評分標準：1=明顯不足 / 2=有待加強 / 3=基本達標 / 4=良好 / 5=優秀

請以 JSON 格式回覆，僅回傳 JSON，不要其他文字：
{{
  "rubric_scores": {{{', '.join(f'"{ax}": 整數1到5' for ax in axes)}}},
  "comment": "給教師參考的整體評語（100字以內，需同時點出『計畫達成度』與『內容品質』）"
}}"""

    user_content = (
        f"【原始自學計畫】\n{plan_text or '（學生未填寫計畫）'}\n\n"
        f"【提交成果】\n{result_text or '（學生未填寫成果）'}"
    )

    message = None
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        # 夾限分數在 1–5
        scores = result.get('rubric_scores', {}) or {}
        for ax in axes:
            v = scores.get(ax)
            if v is not None:
                try:
                    scores[ax] = max(1, min(5, int(v)))
                except (ValueError, TypeError):
                    scores.pop(ax, None)
        result['rubric_scores'] = {ax: scores[ax] for ax in axes if ax in scores}
        result['_usage'] = _extract_usage(message, "claude-sonnet-4-6")
        return result
    except Exception as e:
        print(f"[ai_service] generate_self_study_grading error: {e}")
        return {"error": str(e), "rubric_scores": {}, "comment": f"AI 建議生成失敗：{e}",
                "_error": str(e),
                "_usage": _extract_usage(message, "claude-sonnet-4-6") if message else {}}
