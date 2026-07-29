"""story_weaver.recap.markdown_export — 完整故事 markdown 導出（spec §5.4 / §8.2 後盾）。

compress.py generate_report() 嘅敘事化升級：結構化 JSON → 可閱讀故事文檔。
對話沿用 compress.py 嘅 `> 引用` 排版風格。
"""

from __future__ import annotations

from .models import StoryRecap

_TYPE_LABEL = {
    "action": "",
    "gm_note": "【GM】",
    "player_intervention": "✦",
}


def export_markdown(recap: StoryRecap) -> str:
    out = [f"# {recap.sim_name}：故事全記錄", ""]
    out.append("## 故事開端")
    out.append("")
    out.append(recap.opening)
    out.append("")

    if recap.agents:
        out.append("## 角色")
        out.append("")
        for a in recap.agents:
            bits = [f"### {a.name}"]
            if a.occupation:
                bits.append(f"職業：{a.occupation}")
            if a.personality:
                bits.append(f"性格：{a.personality}")
            out.append("  \n".join(bits))
            out.append("")

    if recap.cumulative_recap.text:
        out.append("## 敘事回顧")
        out.append("")
        if recap.cumulative_recap.status == "fallback":
            out.append("> 故事摘要暫時不可用，以下為原始記錄")
            out.append("")
        out.append(recap.cumulative_recap.text)
        out.append("")

    out.append("## 完整時間線")
    out.append("")
    for r in recap.rounds:
        out.append(f"### 第 {r.round} 回合（{r.sim_time_start} – {r.sim_time_end}）")
        out.append("")
        if r.player_decision:
            out.append(f"✦ 你嘅決定：{r.player_decision.text}")
            out.append("")
        if r.round_recap:
            out.append(r.round_recap)
            out.append("")
        if r.warnings:
            for w in r.warnings:
                out.append(f"> ⚠ {w}")
            out.append("")
        if r.events:
            out.append("**事件**")
            out.append("")
            for e in r.events:
                label = _TYPE_LABEL.get(e.type, "")
                prefix = f"{label} " if label else ""
                out.append(f"- {e.sim_time} {prefix}{e.agent} @ {e.location}：{e.describe}")
            out.append("")
        if r.dialogues:
            out.append("**對話**")
            out.append("")
            for block in r.dialogues:
                participants = " -> ".join(block.participants)
                out.append(f"#### {participants} @ {block.location}")
                out.append("")
                if block.degraded:
                    out.append("> 對話原文已散佚，以下為角色回憶")
                    out.append("")
                for line in block.lines:
                    out.append(f"`{line.speaker}`")
                    out.append(f"> {line.text}")
                    out.append("")
    return "\n".join(out)
