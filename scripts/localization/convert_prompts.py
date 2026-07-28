"""29 個 prompt 模板繁體化：機轉底稿生成 + --check CI 驗證。

usage:
  python scripts/localization/convert_prompts.py           # 轉換（in-place，idempotent）
  python scripts/localization/convert_prompts.py --dry-run # 只報告邊啲檔會變
  python scripts/localization/convert_prompts.py --check   # CI 驗證，唔寫檔

轉換規則（spec §5.11）：
- 中文內容 OpenCC s2hk + glossary 詞彙（vocabulary 措辭覆蓋優先）
- ${} 佔位符名零改動；protected_tokens 唔郁
- 內嵌 JSON 示例 key 唔郁，只轉 value／自然語言
- 尾部統一追加 TRADITIONAL_CHINESE_DIRECTIVE

--check 驗證每個模板：
(a) 無簡體黑名單字 (b) ${} 佔位符良好（名稱 ASCII、配對）
(c) 尾部含 TRADITIONAL_CHINESE_DIRECTIVE (d) 內嵌 JSON 示例段 json.loads 可解析
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _convert import TextConverter, load_glossary  # noqa: E402
from _report import ConvertReport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "generative_agents" / "data" / "prompts"

try:
    sys.path.insert(0, str(REPO_ROOT / "generative_agents"))
    from modules.prompt.keywords import TRADITIONAL_CHINESE_DIRECTIVE  # type: ignore
except Exception:  # keywords.py 未就位時用字面值（同 spec §3.1 一致）
    TRADITIONAL_CHINESE_DIRECTIVE = "一律使用繁體中文（香港書面語）回答。"

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")


def _simplified_chars_in(text: str) -> set[str]:
    """動態黑名單：OpenCC s2hk 會改動嘅字＝簡體（同轉換器同源，唔會自相矛盾）。"""
    try:
        from opencc import OpenCC

        cc = OpenCC("s2hk")
        return {ch for ch in text if "一" <= ch <= "鿿" and cc.convert(ch) != ch}
    except Exception:  # opencc 缺裝用內置高頻表
        fallback = "话觉闲时厅卫个们这学习体现贝见责账质买卖车站让认诉语读说听长层关门开间前觉刚则剧制到办动务块声处备复头奍妇妈妙妹始威子存学守宇完审客害密尔层屿峡差席带帮干平幸幻广庄庆应废开异弄式弟张强归当录形往很後必志忘快态怀怎怒思急恋恐恩悄悔悟恶惠惑惩想愈意慢惯憨懂懒戏户房扇才执扫扬扰折抛抢护报担拟拣拥拦拧拨择挂挚挠挡挣挤挥捞损捡换据掳扫排掘掠探接控推掩措掰掮揩摊搅搔摇搜搞摊携摄摆摇撑撒撙撸捞操担挡挤捡换据传伤价伦伪伫体余侠侣侥侦侧侨侩侪侬俣俣俦俨俩俪俭债倾偬偻偾偿傥傧储傩儿兑兖党兰关兴养兽冁内冈册写军农冯冲决况冻净凄准凉减凑凛几凤凫凭凯击凿刍划刘则刚创删别刬刭刹剂剐剑剥剧劝办务劢动励劲劳势勋匀匦匮区医华协单卖卢卤卫却厂厅历厉压厌厍厕厘厢厦厨厩县叁参双发变叙叠叶号叹叽吁后吓吕吗听启吴呐呒呓呕员呙呛呜咏咙咛咝咤响哑哒哓哔哕哗哙哝哟唛唝唠唡唢唤唿啧啬啭啮啴啸喷喽喾嗫嗳嘘嘤嘱噜嚣嚯团园囱围囵国图圆圣圹场坂坏块坚坛坜坝坞坟坠垄垅垆垒垦垩垫垭垱垲埘埙埚堑堕墙壮声壳壶壸处备复够头夹夺奁奂奋奖奥妆妇妈妩妪妫姗姜娄娅娆娇娈娱娲娴婳婴婵婶媪嫒嫔嫱嬷孙学孪宁宝实宠审宪宫宽宾寝对寻导寿将尔尘尧尴尸尽层屃屉届属屡屦屿岁岂岖岗岘岙岚岛岭岳岽岿峃峄峡峣峤峥峦崂崃崄崭嵘嵚嵝巅巩巯币帅师帏帐帘帜带帧帮帱帻帼幂并广庄庆庐庑库应庙庞废庼廪开异弃弑弛张弥弪弯弹强归当录彦彻征径徕御忆忏忧忾怀态怂怃怄怅怆怜总怼怿恋恳恶恸恹恺恻恼恽悦悫悬悭悮悯惊惧惨惩惫惬惭惮惯愠愤愦愿慑慭憷懑懒懔戆戋戏戗战戬户扑执扩扪扫扬扰抚抛抟抠抡抢护报担拟拢拣拥拦拧拨择挂挚挛挜挝挞挟挠挡挢挣挤挥挦捞损捡换捣据掳掴掷掸掺掼揽揿搀搁搂搅携摄摅摆摇摈摊撄撑撵撷撸撺擞攒敌敛数斋斓斗斩断无旧时旷旸昙昼昽显晋晒晓晔晕晖暂暧朴机杀杂权条来杨杩杰极构枞枢枣枥枧枨枪枫枭柜柠柽栀栅标栈栋栏树栖样栾桊桠桡桢档桤桥桦桧桨桩梦梼梾检棂椁椟椠椤椭楼榄榅榇榈榉槚槛槟槠横樯樱橥橱橹橼檩欢欤欧歼殁殇残殒殓殚殡殴毁毂毕毙毡毵氇气氢氩氲汇汉汤汹沟没沣沤沥沦沧沨沩沪沵泞泪泶泷泸泺泻泼泽泾洁洒洼浃浅浆浇浈浊测济浏浑浒浓浔浕涂涛涝涞涟涠涡涣涤润涧涨涩渊渌渍渎渐渑渔渖渗温游湾湿溃溅溆溇滗滚滞滟滠满滢滤滥滦滨滩滪漤潆潇潋潍潜潴澜濑濒灏灭灯灵灾灿炀炉炖炜炝点炼炽烁烂烃烛烟烦烧烨烩烫烬热焕焖焘煴爱爷牍牦牵牺犊状犷犸犹狈狝狞独狭狮狯狰狱狲猃猎猕猡猪猫猬献獭玑玙玛玮环现玱玺珉珏珐珑珰珲琏琐琼瑶瑷璎瓒瓮瓯电画畅畴疖疗疟疠疡疬疭疮疯疱疴痈痉痒痖痨痪痫瘅瘗瘘瘪瘫瘾瘿癞癣癫皑皱皲盏盐监盖盗盘眍眦眬着睁睐睑瞒瞩矫矶矾矿砀码砖砗砚砜砺砻砾础硁硕硖硗硙硚确硷碍碛碜礼祃祎祢祯祷祸禀禄禅离秃秆种积称秽秾稆税稣稳穑穷窃窍窑窜窝窥窦窭竖竞笃笋笔笕笺笼笾筑筚筛筜筝筹签简箓箦箧箨箩箪箫篑篓篮篱簖籁籴类籼粜粝粤粪粮糁糇紧絷纟纠纡红纣纤纥约级纨纩纪纫纬纭纯纰纱纲纳纵纶纷纸纹纺纽纾线绀练组绅细织终绉绊绋绍绎经绑绒结绕绘给绚绛络绝绞统绠绡绢绣绥绦继绩绪绫续绮绯绰绱绲绳维绵绶绷绸绹绺绻综绽绾绿缀缁缂缃缇缈缉缋缌缍缎缏缑缒缓缔缕编缗缘缙缚缛缜缝缟缠缡缢缣缤缥缦缧缨缩缪缫缬缭缮缯缰缱缲缳缴缵罂网罗罚罢罴羁羟羡翘耧耸耻聂聋职聍联聩聪肃肠肤肮肴肾肿胀胁胆胜胧胨胪胫胶脉脍脏脐脑脓脔脚脱脶脸腊腌腘腭腻腼腽腾膑臜舆舰舱舣艰艳艺节芈芗芜芦苁苇苈苋苌苍苎苏苧苹范茎茏茑茔茕茧荆荐荙荚荛荜荞荟荠荡荣荤荥荦荧荨荩荪荫荬荭荮药莅莱莲莳莴莶获莸莹莺莼萚萝萤营萦萧萨葱蒇蒉蒋蒌蓝蓟蓠蓣蓥蓦蔷蔹蔺蔼蕰蕲蕴薮藓蘖虏虑虚虫虬虮虽虾虿蚀蚁蚂蚕蚝蚬蛊蛎蛏蛮蛰蛱蛲蛳蛴蜕蜗蜡蝇蝈蝉蝼蝾螀螨蟏衅衔补衬衮袄袅袆袜袭袯装裆裈裢裣裤裥褛褴襁襕见观觃规觅视觇览觉觊觋觌觍觎觏觐觑觞触觯詟誉誊讠计订讣认讥讦讧讨让讪讫训议讯记讲讳讴讵讶讷许讹论讼讽设访诀证诂诃评诅识诈诉诊诋诌词诎诏译诒诓诔试诗诘诙诚诛诜话诞诟诠诡询诣诤该详诧诨诩诪诫诬语诮误诰诱诲诳说诵诶请诸诹诺读诼诽课诿谀谁调谄谅谆谈谊谋谌谍谎谏谐谑谒谓谔谕谖谗谘谙谚谛谜谟谠谡谢谣谤谥谦谧谨谩谪谫谬谭谮谯谰谱谲谳谴谵谶谷豮贝贞负贡财责贤败账货质贩贪贫贬购贮贯贰贱贲贳贴贵贶贷贸费贺贻贼贽贾贿赀赁赂赃资赅赆赇赈赉赊赋赌赍赎赏赐赑赒赓赔赕赖赗赘赙赚赛赞赠赡赢赣赵赶趋趱趸跃跄跞践跶跷跸跹跻踊踌踪踬踯蹑蹒蹰蹿躏躜躯车轧轨轩轫轭轮软轰轱轲轳轴轵轶轷轸轹轺轻轼载轾轿辀辁辂较辄辅辆辇辈辉辊辋辌辍辎辏辐辑输辕辖辗辘辙辚辞辟辩辫边辽达迁过迈运还这进远违连迟迩迳迹适选逊递逦逻遗遥邓邝邬邮邹邺邻郏郑郓郦郧郸酝酦酱酽酾酿释里鉴銮錾钅钆钇针钉钊钋钌钍钎钏钐钒钓钔钕钖钗钘钙钚钛钜钝钞钟钠钡钢钥钦钧钨钩钪钫钬钭钮钯钰钱钲钳钴钵钶钷钹钺钻钼钽钾钿铁铃铄铆铈铉铊铋铌铍铎铏铐铑铒铓铔铕铗铘铙铚铛铜铝铟铠铡铢铣铤铥铦铧铨铩铪铫铭铮铰铱铲铳铴铵银铷铸铹铺铻铼铽链铿销锁锄锅锈锉锎锏锐错锚锜锞锟锡锢锣锤锥锦锨锩锫锬锭键锯锰锱锲锴锶锷锸锹锺锻锼锽锾锿镀镁镂镃镄镅镆镇镌镏镰镓镔镕镝镞镟镡镢镣镤镥镦镧镨镩镪镫镬镭镮镯镱镲镳镴镶长门闩闪闫闭问闯闰闲间闵闷闸闹闺闻闼闽闾阀阁阂阃阄阅阆阈阉阊阋阌阍阎阏阐阑阒阔阕阖阗阘阙阚队阳阴阵阶际陆陇陈陉陕陧陨险随隐隶隽难雏雠雳雾霁霉静靥鞑鞒鞯韦韧韩韪韫韬韵页顶顷项顺须顼顾顿颀颁颂预颅领颇颈颉颊颋颌颍颏颐频颓颖颗题颙颛颜额颞颟颠颡颢颤颥颦风飏飐飑飒飓飔飕飗飘飙飚飞饣饥饧饨饩饪饫饬饭饮饯饰饱饲饳饴饵饶饷饸饹饺饻饼饽饿馀馁馂馃馄馅馆馇馈馉馊馋馌馍馎馏馐馑馒馓馔馕马驭驮驰驱驳驴驵驶驷驸驹驻驼驽驾驿骀骁骂骄骅骆骇骈骊骋验骎骏骐骑骒骓骖骗骘骚骛骜骝骞骟骠骡骢骣骤骥骧髅髋髌鬓魇魉鱼鱿鲁鲂鲍鲎鲐鲑鲔鲕鲚鲛鲜鲞鲟鲠鲡鲢鲣鲤鲥鲦鲧鲨鲩鲫鲭鲮鲰鲱鲲鲳鲴鲵鲷鲸鳃鳄鳅鳌鳍鳏鳐鳓鳔鳕鳖鳗鳘鳙鳜鳝鳞鳟鸟鸠鸡鸢鸣鸥鸦鸨鸩鸪鸫鸬鸭鸯鸱鸲鸳鸵鸶鸷鸸鸹鸺鸻鸼鸽鸾鸿鹁鹂鹃鹄鹅鹆鹇鹈鹉鹊鹋鹌鹍鹏鹐鹑鹒鹓鹔鹕鹖鹗鹘鹚鹛鹜鹞鹣鹤鹥鹦鹧鹨鹩鹪鹫鹬鹭鹰鹱鹳鹾麦黄黉黩黪黾鼋鼍鼹齐齑齿龀龃龄龅龆龇龈龉龊龋龌龙龚龛龟龠"
        return {ch for ch in text if ch in fallback}


def extract_json_blocks(text: str) -> list[str]:
    """抽出獨立成行嘅 JSON 示例段（{...} 或 [...]），用行級配對。"""
    blocks = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped in ("{", "["):
            opener = stripped
            closer = "}" if opener == "{" else "]"
            depth = 0
            buf = []
            j = i
            while j < len(lines):
                buf.append(lines[j])
                depth += lines[j].count(opener) - lines[j].count(closer)
                if depth == 0:
                    break
                j += 1
            if depth == 0:
                blocks.append("\n".join(buf))
                i = j + 1
                continue
        i += 1
    return blocks


def convert_template(text: str, conv: TextConverter) -> str:
    out = conv.convert_text(text)
    if TRADITIONAL_CHINESE_DIRECTIVE not in out:
        out = out.rstrip("\n") + "\n\n" + TRADITIONAL_CHINESE_DIRECTIVE + "\n"
    return out


def check_template(path: Path) -> list[str]:
    errors = []
    text = path.read_text(encoding="utf-8")
    bad = _simplified_chars_in(text)
    if bad:
        errors.append(f"含簡體字：{''.join(sorted(bad))}")
    # 佔位符良好性：名稱只准 ASCII identifier
    for m in _PLACEHOLDER_RE.finditer(text):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            errors.append(f"佔位符異常：${{{m.group(1)}}}")
    if text.count("${") != len(_PLACEHOLDER_RE.findall(text)):
        errors.append("存在未配對嘅 ${ 佔位符")
    if TRADITIONAL_CHINESE_DIRECTIVE not in text:
        errors.append("尾部缺 TRADITIONAL_CHINESE_DIRECTIVE")
    for block in extract_json_blocks(text):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            errors.append(f"JSON 示例段解析失敗：{exc}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glossary", default=str(REPO_ROOT / "generative_agents" / "data" / "glossary_s2hk.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    templates = sorted(PROMPTS_DIR.glob("*.txt"))
    if len(templates) != 29:
        print(f"ERROR: 模板數量唔係 29：{len(templates)}", file=sys.stderr)
        return 1

    if args.check:
        failed = 0
        for p in templates:
            errors = check_template(p)
            if errors:
                failed += 1
                print(f"FAIL {p.name}")
                for e in errors:
                    print(f"  - {e}")
        if failed:
            print(f"{failed}/{len(templates)} 個模板未過 check")
            return 1
        print(f"OK: {len(templates)} 個模板全部通過 check")
        return 0

    glossary = load_glossary(args.glossary)
    conv = TextConverter(glossary)
    report = ConvertReport(dry_run=args.dry_run)
    for p in templates:
        report.files_scanned += 1
        src = p.read_text(encoding="utf-8")
        out = convert_template(src, conv)
        if out != src:
            report.files_changed += 1
            report.replacements += sum(1 for a, b in zip(src, out) if a != b) + abs(len(out) - len(src))
            if not args.dry_run:
                p.write_text(out, encoding="utf-8")
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
