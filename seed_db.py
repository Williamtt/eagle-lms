"""
seed_db.py
將 questionnaire_definitions.py 中的問卷定義同步寫入資料庫。

使用方式：
    python seed_db.py

應在以下時機執行：
    - 首次部署時
    - questionnaire_definitions.py 版本更新後（版本號變動）
    - 新增問卷後

注意：此腳本採用「更新或新增」策略（upsert by code），
不會刪除已有填答資料的問卷。
"""

import json
from app import app
from models import db, Questionnaire, QuestionnaireItem
from questionnaire_definitions import QUESTIONNAIRES


def seed_questionnaires():
    seeded = 0
    updated = 0

    for code, defn in QUESTIONNAIRES.items():
        existing = Questionnaire.query.filter_by(code=code).first()

        if existing:
            # 若版本相同，跳過
            if existing.version == defn["version"]:
                print(f"  [SKIP]    {code}  (v{defn['version']} 已存在)")
                continue
            # 版本不同：更新 metadata，但不動已有答案（只更新 items）
            existing.name        = defn["name"]
            existing.description = defn.get("description", "")
            existing.version     = defn["version"]
            existing.timing      = defn.get("timing", "")
            # 清除舊 items（未有填答紀錄時才安全）
            QuestionnaireItem.query.filter_by(questionnaire_id=existing.id).delete()
            _add_items(existing.id, defn["items"])
            updated += 1
            print(f"  [UPDATE]  {code}  → v{defn['version']}")
        else:
            q = Questionnaire(
                code        = code,
                name        = defn["name"],
                description = defn.get("description", ""),
                version     = defn["version"],
                semester    = defn["semester"],
                timing      = defn.get("timing", ""),
                is_active   = False,
            )
            db.session.add(q)
            db.session.flush()   # 取得 q.id
            _add_items(q.id, defn["items"])
            seeded += 1
            print(f"  [CREATE]  {code}  (v{defn['version']})")

    db.session.commit()
    print(f"\n完成：新增 {seeded} 份問卷，更新 {updated} 份問卷。")


def _add_items(questionnaire_id, items):
    for item in items:
        qi = QuestionnaireItem(
            questionnaire_id = questionnaire_id,
            item_code        = item["item_code"],
            dimension        = item["dimension"],
            dimension_label  = item.get("dimension_label", ""),
            activity         = item.get("activity", ""),
            text             = item["text"],
            scale_type       = item.get("scale_type", "likert5"),
            choices          = json.dumps(item.get("choices", []), ensure_ascii=False),
            order            = item.get("order", 0),
            required         = item.get("required", True),
        )
        db.session.add(qi)


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("=== Seeding questionnaires ===")
        seed_questionnaires()
