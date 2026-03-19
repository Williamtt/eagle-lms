import json
import os
from anthropic import Anthropic

# Task context for AI grading
TASK_CONTEXT = {
    1: {
        "name": "專案建立",
        "learning_goals": [
            "理解工程專案基本資料的組成要素",
            "理解契約工項（詳細價目表）的結構",
            "了解成員角色與權限配置",
            "認識自主檢查類型與品質管理前置作業",
            "了解工程進度表在系統中的角色",
            "建立開工前準備工作完整性的責任意識"
        ],
        "accountability_focus": "專案建立的準確性與完整性"
    },
    2: {
        "name": "施工日誌填寫（監造日報表第一聯）",
        "learning_goals": [
            "理解監造日報表的格式與填寫要求",
            "從歷史資料擷取關鍵訊息",
            "計算預定與實際進度百分比",
            "體驗監造工程師的觀察視角",
            "建立施工日誌真實性與正確性的責任意識",
            "培養按時填報與自我檢核的習慣"
        ],
        "accountability_focus": "日報表的真實性、準確性與按時填報"
    },
    3: {
        "name": "工程進度計算與填寫",
        "learning_goals": [
            "運用CPM計算要徑與浮時",
            "運用EVM計算PV、EV、AC、SV、CV、SPI、CPI",
            "在EAGLE系統中撰寫進度報告",
            "理解進度管控的關鍵影響",
            "培養以數據為基礎的管理決策能力"
        ],
        "accountability_focus": "數據分析的準確性與管理建議的專業度"
    },
    4: {
        "name": "自主查核表填寫",
        "learning_goals": [
            "理解三級品管制度",
            "認識施工抽查程序",
            "學習自主查核表的填寫方法",
            "在EAGLE系統中操作品質查驗",
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


def generate_instant_feedback(task_number, submission_type, content, student_name=""):
    """Generate immediate AI feedback for student submission."""
    client = get_client()
    if not client:
        return {"feedback": "（AI 回饋功能尚未啟用，請等待教師設定 API 金鑰。）", "scores": {}}

    task = TASK_CONTEXT.get(task_number, {})
    task_name = task.get("name", f"任務{task_number}")
    goals = "\n".join(f"- {g}" for g in task.get("learning_goals", []))
    acc_focus = task.get("accountability_focus", "")

    system_prompt = f"""你是一位營建管理課程的 AI 教學助教，負責協助評閱學生在「磺港溪再造步道整建工程」EAGLE 系統自主學習任務中的作業。

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
            model="claude-sonnet-4-20250514",
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
        return result
    except json.JSONDecodeError:
        # If response isn't valid JSON, return the raw text as feedback
        raw = message.content[0].text if message else "AI 回饋生成失敗"
        # Try to extract feedback from partial JSON
        return {
            "feedback": raw,
            "scores": {}
        }
    except Exception as e:
        return {"feedback": f"AI 回饋生成時發生錯誤：{str(e)}", "scores": {}}


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
            model="claude-sonnet-4-20250514",
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

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"學生提交內容：\n\n{submission_content}"
            }]
        )
        return json.loads(message.content[0].text)
    except Exception:
        return {"suggestion": "AI 建議生成失敗", "suggested_score": None}
