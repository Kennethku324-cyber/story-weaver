"""共用轉換邏輯：glossary 詞彙優先 + OpenCC s2hk 兜底。

所有 scripts/localization/ 腳本共用呢個 converter，保證 build-time 轉換
同 runtime normalize 層（modules/model/text_normalize.py）行為一致：
- glossary 嘅 place_names / agent_names / keywords 值同 OpenCC s2hk 輸出一致
- vocabulary 係書面語措辭覆蓋，優先於 OpenCC
- protected_tokens（${...} 佔位符、living_area、sleeping、the Ville）唔准郁
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GLOSSARY = REPO_ROOT / "generative_agents" / "data" / "glossary_s2hk.json"

_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")
_SENTINEL = "\x00{}\x01"

# OpenCC s2hk 嘅習字表／舊字形 → 香港日常常用字形（只適用於敘述文字，
# 地名 token 行 place_names 查表，唔經呢度）。
# 牀→床 同時係 keywords.py KW_BED="床" 嘅硬性要求（睡覺地址推導）。
VARIANT_FIX = {
    "牀": "床",
    "衞": "衛",
    "啓": "啟",
    "説": "說",
    "閲": "閱",
    "悦": "悅",
    "脱": "脫",
    "税": "稅",
    "兑": "兌",
    "鋭": "銳",
    "藴": "蘊",
}

_opencc_instance = None


def _get_opencc():
    global _opencc_instance
    if _opencc_instance is None:
        try:
            from opencc import OpenCC

            _opencc_instance = OpenCC("s2hk")
        except Exception:  # pragma: no cover - opencc 缺裝降級
            _opencc_instance = False
    return _opencc_instance or None


def load_glossary(path: str | Path = DEFAULT_GLOSSARY) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TextConverter:
    """簡→繁（香港書面語）文字轉換器。"""

    def __init__(self, glossary: dict):
        self.glossary = glossary
        # 替換表：vocabulary（措辭覆蓋）+ keywords + agent_names + place_names
        # 全部按 key 長度降序，避免短詞搶先命中（「咖啡馆」唔會畀「咖啡」截糊）
        self._replacements: list[tuple[str, str]] = []
        for section in ("vocabulary", "keywords", "agent_names", "place_names"):
            for src, dst in glossary.get(section, {}).items():
                self._replacements.append((src, dst))
        self._replacements.sort(key=lambda kv: len(kv[0]), reverse=True)
        self.protected = list(glossary.get("protected_tokens", []))

    def _protect(self, text: str) -> tuple[str, list[str]]:
        """將 protected token 同 ${...} 佔位符換做 sentinel，轉換後還原。"""
        stash: list[str] = []

        def _sub(m: re.Match) -> str:
            stash.append(m.group(0))
            return _SENTINEL.format(len(stash) - 1)

        text = _PLACEHOLDER_RE.sub(_sub, text)
        for tok in self.protected:
            if tok == "${":  # 佔位符已由 regex 處理
                continue
            if tok in text:
                stash.append(tok)
                text = text.replace(tok, _SENTINEL.format(len(stash) - 1))
        return text, stash

    @staticmethod
    def _restore(text: str, stash: list[str]) -> str:
        for i, tok in enumerate(stash):
            text = text.replace(_SENTINEL.format(i), tok)
        return text

    def convert_text(self, text: str) -> str:
        if not text:
            return text
        protected, stash = self._protect(text)
        for src, dst in self._replacements:
            if src in protected:
                protected = protected.replace(src, dst)
        cc = _get_opencc()
        if cc is not None:
            protected = cc.convert(protected)
        for variant, common in VARIANT_FIX.items():
            if variant in protected:
                protected = protected.replace(variant, common)
        return self._restore(protected, stash)

    def convert_json_values(self, obj):
        """遞歸轉換 JSON 結構入面嘅字串 value；dict key 唔郁。"""
        if isinstance(obj, str):
            return self.convert_text(obj)
        if isinstance(obj, list):
            return [self.convert_json_values(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.convert_json_values(v) for k, v in obj.items()}
        return obj


def has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


# 常見簡體高頻表（OpenCC 缺裝時嘅 scanner 兜底；唔包含床/衛/啟呢啲香港常用字）
_FALLBACK_SIMPLIFIED = set("话觉闲时厅卫个们这学习体现贝见责账质买卖车站让认诉语读说听长层关门开间纪纫纯纲纳纵纷纸纹纺纽线练组绅细织终绍绎经绑绒结绕绘给络绝绞统绢绣继绩绪绫续绮绯绰绳维绵绷绸综绽绿缀缺罢羁翘聋职联聪肃肠肤肾肿胀胁胆胜脉脏脐脑脚脱脸腊腻腾舆舰舱艺节芜芦苇苋苍苏苹范茎茏茔茧荆荐荚荛荜荞荟荠荡荣荤荥荦荧荨荩荪荫药莅莱莲莳莴获莹莺萝萤营萦萧萨葱蓝蓟蓠蓦蔷蔼蕴薮虏虑虚虫虽虾蚀蚁蚂蚕蛮蛰蛳蜕蜗蝇蝉蝼螨衅衔补衬袄袜袭装裤见观规觅视览觉触誉计订认讥讨让训议讯记讲讳讶许论讼讽设访诀证评识诈诉诊词译试诗诚话诞诡询该详语误诱诲说诵请诸诺读课谁调谅谈谊谋谎谏谐谓谕谗谜谢谣谦谨谬谭谱谷贝贞负贡财责贤败账货质贩贪贫贬购贮贯贰贱贴贵贷贸费贺贼贾贿赂赃资赈赊赋赌赏赐赔赖赘赚赛赞赠赡赢赵赶趋跃践踪躯车轧轨轩转轮软轰轴轻载轿较辅辆辉辑输辖辗辞辟辩辫边辽达迁过迈运还这进远违连迟迹适选逊递逻遗遥邓邮邹邻郑郧酝酱释鉴针钉钓钙钝钞钟钠钢钥钦钧钨钩钮钱钳钻铁铃铅铆铜铝铭银铸铺链销锁锄锅锈锋锌锐错锚锡锣锤锥锦键锯锰锹锻镀镁镇镜镰长闪闭问闯闲间闷闸闹闻阀阁阅队阳阴阵阶际陆陈陕陨险随隐隶难雏雾霁静韦韧韩韵页顶顷项顺须顾顿颁颂预颅领颇颈颊频颖颗题颜额颠风飘飞饥饨饪饭饮饯饰饱饲饶饺饼饿馁馅馆馈馋馒马驭驮驰驱驳驴驶驸驹驻驼驾驿骂骄骆骇验骑骗骚骡骤髅鱼鲁鲜鲤鲫鲸鳃鳖鳞鸟鸠鸡鸣鸥鸦鸭鸳鸵鸽鸾鸿鹂鹃鹄鹅鹇鹈鹉鹊鹌鹏鹑鹕鹤鹩鹫鹭鹰麦黄齿龄龙龟")


def simplified_chars_in(text: str) -> set[str]:
    """簡體黑名單掃描：OpenCC s2hk 會改動嘅 CJK 字（動態衍生，同轉換器同源）。

    豁免 VARIANT_FIX 嘅常用字形（床/衛/啟）——佢哋係香港日常寫法，
    build-time 轉換會刻意用呢啲字，唔算簡體殘留。
    """
    cc = _get_opencc()
    if cc is not None:
        exempt = set(VARIANT_FIX.values())
        return {
            ch
            for ch in text
            if "一" <= ch <= "鿿" and ch not in exempt and cc.convert(ch) != ch
        }
    return {ch for ch in text if ch in _FALLBACK_SIMPLIFIED}


def convert_address_token(token: str, glossary: dict) -> str:
    """地名 token 精確映射：protected 原樣、place_names 查表、非 CJK 原樣。

    查唔到表嘅 CJK token 拋 KeyError（caller 收集做 error，唔好靜默放過）。
    """
    if token in glossary.get("protected_tokens", []):
        return token
    mapping = glossary.get("place_names", {})
    if token in mapping:
        return mapping[token]
    if token in mapping.values():  # 已轉換嘅 token 原樣放行（rerun 安全）
        return token
    if not has_cjk(token):
        return token
    raise KeyError(f"地名 token 未喺 glossary place_names 登記：{token!r}")
