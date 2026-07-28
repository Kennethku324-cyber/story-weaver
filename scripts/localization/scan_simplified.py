"""全 repo 簡體掃描（CI gate，spec §3.3）。

usage: python scripts/localization/scan_simplified.py
exit 0 = 零命中；非 0 = 有簡體殘留（逐檔逐字列出）。

掃描範圍：
- generative_agents/data/prompts/*.txt
- generative_agents/modules/**/*.py（豁免 keywords.py、timer.py；.py 只掃非註釋部分）
- generative_agents/frontend/templates/*.html
- generative_agents/frontend/static/assets/village/**/*.json

黑名單：OpenCC s2hk 會改動嘅 CJK 字（同轉換器同源；OpenCC 缺裝時用內置高頻表）。
注意：HK 標準字形（如「卧」「台」）喺 s2hk 映像內，唔會誤傷。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "generative_agents"

EXEMPT_PY = {"keywords.py", "timer.py"}

_FALLBACK = set("话觉闲时厅卫个们这学习体现贝见责账质买卖车站让认诉语读说听长层关门开间纪纫纯纲纳纵纷纸纹纺纽线练组绅细织终绍绎经绑绒结绕绘给络绝绞统绢绣继绩绪绫续绮绯绰绳维绵绷绸综绽绿缀缆缦缨缩缴缺罢羁翘聋职联聪肃肠肤肾肿胀胁胆胜脉脏脐脑脚脱脸腊腻腾舆舰舱艺节芜芦苇苋苍苏苹范茎茏茔茧荆荐荚荛荜荞荟荠荡荣荤荥荦荧荨荩荪荫药莅莱莲莳莴获莹莺萝萤营萦萧萨葱蓝蓟蓠蓦蔷蔼蕴薮虏虑虚虫虽虾蚀蚁蚂蚕蛮蛰蛳蜕蜗蝇蝉蝼螨衅衔补衬袄袜袭装裤见观规觅视览觉触誉计订认讥讨让训议讯记讲讳讶许论讼讽设访诀证评识诈诉诊词译试诗诚话诞诡询该详语误诱诲说诵请诸诺读课谁调谅谈谊谋谎谏谐谓谕谗谜谢谣谦谨谬谭谱谷贝贞负贡财责贤败账货质贩贪贫贬购贮贯贰贱贴贵贷贸费贺贼贾贿赂赃资赈赊赋赌赏赐赔赖赘赚赛赞赠赡赢赵赶趋跃践踪躯车轧轨轩转轮软轰轴轻载轿较辅辆辉辑输辖辗辞辟辩辫边辽达迁过迈运还这进远违连迟迹适选逊递逻遗遥邓邮邹邻郑郧酝酱释里鉴针钉钓钙钝钞钟钠钢钥钦钧钨钩钮钱钳钻铁铃铅铆铜铝铭银铸铺链销锁锄锅锈锋锌锐错锚锡锣锤锥锦键锯锰锹锻镀镁镇镜镰长门闪闭问闯闲间闷闸闹闻阀阁阅队阳阴阵阶际陆陈陕陨险随隐隶难雏雾霁静韦韧韩韵页顶顷项顺须顾顿颁颂预颅领颇颈颊频颖颗题颜额颠风飘飞饥饨饪饭饮饯饰饱饲饶饺饼饿馁馅馆馈馋馒马驭驮驰驱驳驴驶驸驹驻驼驾驿骂骄骆骇验骑骗骚骡骤髓髅鱼鲁鲜鲤鲫鲸鳃鳖鳞鸟鸠鸡鸣鸥鸦鸭鸳鸵鸽鸾鸿鹂鹃鹄鹅鹇鹈鹉鹊鹌鹏鹑鹕鹤鹩鹫鹭鹰麦黄齿龄龙龟")


def _simplified_chars_in(text: str) -> set[str]:
    try:
        from opencc import OpenCC

        cc = OpenCC("s2hk")
        return {ch for ch in text if "一" <= ch <= "鿿" and cc.convert(ch) != ch}
    except Exception:
        return {ch for ch in text if ch in _FALLBACK}


def _strip_py_comments(text: str) -> str:
    """.naive 註釋剝離：逐行切 # 之後嘅嘢（掃描工具，容許 f-string 內 # 誤切）。"""
    out = []
    for line in text.splitlines():
        pos = line.find("#")
        out.append(line if pos < 0 else line[:pos])
    return "\n".join(out)


def scan() -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}

    def _check(path: Path, text: str):
        bad = _simplified_chars_in(text)
        if bad:
            hits[str(path.relative_to(REPO_ROOT))] = bad

    for p in sorted((GEN / "data" / "prompts").glob("*.txt")):
        _check(p, p.read_text(encoding="utf-8"))
    for p in sorted((GEN / "modules").rglob("*.py")):
        if p.name in EXEMPT_PY:
            continue
        _check(p, _strip_py_comments(p.read_text(encoding="utf-8")))
    for p in sorted((GEN / "frontend" / "templates").glob("*.html")):
        _check(p, p.read_text(encoding="utf-8"))
    for p in sorted((GEN / "frontend" / "static" / "assets" / "village").rglob("*.json")):
        _check(p, p.read_text(encoding="utf-8"))
    return hits


def main() -> int:
    hits = scan()
    if hits:
        print(f"FAIL: {len(hits)} 個檔案有簡體殘留")
        for path, chars in hits.items():
            print(f"  {path}: {''.join(sorted(chars))}")
        return 1
    print("OK: 掃描範圍零簡體命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
