from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, DefaultDict
from urllib.parse import urlparse
import re

from dateutil import parser as dtparser


DIMENSION_WEIGHTS = {
    "sector_prosperity": 10,
    "profit_pool": 10,
    "high_end_attribute": 6,
    "customer_validation": 16,
    "commercialization_progress": 13,
    "investability": 14,
    "exit_feasibility": 12,
    "competitive_position": 10,
    "information_sufficiency": 9,
}

DIMENSION_LABELS = {
    "sector_prosperity": "赛道景气",
    "profit_pool": "利润池",
    "high_end_attribute": "高端属性",
    "customer_validation": "客户验证",
    "commercialization_progress": "商业化进度",
    "investability": "可投性",
    "exit_feasibility": "退出可行性",
    "competitive_position": "竞争卡位",
    "information_sufficiency": "信息充分度",
}

WEIGHT_BAND = {
    "sector_prosperity": "高",
    "profit_pool": "高",
    "high_end_attribute": "中",
    "customer_validation": "最高",
    "commercialization_progress": "高",
    "investability": "最高",
    "exit_feasibility": "高",
    "competitive_position": "高",
    "information_sufficiency": "高",
}

SUB_DIMENSIONS = {
    "sector_prosperity": {
        "label": "赛道景气",
        "sub": [
            {"key": "policy_catalyst", "label": "政策催化", "desc": "是否有明确产业政策支持、补贴、标准出台", "weight_pct": 30},
            {"key": "demand_expansion", "label": "需求扩张", "desc": "下游客户需求是否处于上升周期，渗透率提升空间", "weight_pct": 30},
            {"key": "penetration_rate", "label": "渗透率空间", "desc": "当前技术/产品在目标市场的渗透率，距天花板的距离", "weight_pct": 20},
            {"key": "industry_cycle", "label": "产业景气周期", "desc": "行业处于早期爆发/快速成长/成熟/衰退的哪个阶段", "weight_pct": 20},
        ],
    },
    "profit_pool": {
        "label": "利润池",
        "sub": [
            {"key": "value_chain_pos", "label": "价值链位置", "desc": "处于核心部件/底层软件/系统集成/终端服务的哪个位置", "weight_pct": 30},
            {"key": "pricing_power", "label": "议价能力", "desc": "对上下游的议价能力，客户切换成本是否足够高", "weight_pct": 25},
            {"key": "gross_margin", "label": "毛利厚度", "desc": "毛利率水平及趋势，与同行比较", "weight_pct": 25},
            {"key": "aftermarket", "label": "后市场潜力", "desc": "是否具备耗材/软件订阅/维保等后市场收入空间", "weight_pct": 20},
        ],
    },
    "high_end_attribute": {
        "label": "高端属性",
        "sub": [
            {"key": "tech_barrier", "label": "技术壁垒", "desc": "核心技术难度、专利布局、研发投入强度", "weight_pct": 30},
            {"key": "certification", "label": "认证门槛", "desc": "行业资质/客户认证/安全标准的进入壁垒", "weight_pct": 25},
            {"key": "process_knowhow", "label": "工艺know-how", "desc": "制造工艺/调试经验/参数积累等隐性壁垒", "weight_pct": 25},
            {"key": "import_sub", "label": "国产替代难度", "desc": "该领域国产替代的紧迫性和可行性", "weight_pct": 20},
        ],
    },
    "customer_validation": {
        "label": "客户验证",
        "sub": [
            {"key": "benchmark_client", "label": "标杆客户", "desc": "是否导入行业标杆/头部客户，客户质量", "weight_pct": 30},
            {"key": "adoption_depth", "label": "导入深度", "desc": "客户是试用/小批量/大批量/战略供应商", "weight_pct": 25},
            {"key": "repurchase", "label": "复购率", "desc": "客户续约/复购/扩单情况", "weight_pct": 25},
            {"key": "cross_industry", "label": "跨行业复制", "desc": "是否可从单一行业复制到多行业", "weight_pct": 20},
        ],
    },
    "commercialization_progress": {
        "label": "商业化进度",
        "sub": [
            {"key": "mass_production", "label": "量产能力", "desc": "产品是否已达到批量交付状态", "weight_pct": 30},
            {"key": "order_conversion", "label": "订单转化", "desc": "管线订单到签约收入的转化效率", "weight_pct": 25},
            {"key": "channel_build", "label": "渠道建设", "desc": "销售渠道/代理商/生态伙伴的建设进度", "weight_pct": 25},
            {"key": "scale_efficiency", "label": "规模化效率", "desc": "收入增长与人员/成本增长的比例关系", "weight_pct": 20},
        ],
    },
    "investability": {
        "label": "可投性",
        "sub": [
            {"key": "round_match", "label": "轮次匹配", "desc": "当前融资轮次是否符合基金偏好", "weight_pct": 30},
            {"key": "valuation", "label": "估值合理性", "desc": "当前估值相对业务进展是否合理", "weight_pct": 30},
            {"key": "equity_structure", "label": "股权结构", "desc": "创始人持股、老股东结构、对赌/优先权安排", "weight_pct": 20},
            {"key": "timing_window", "label": "融资窗口", "desc": "当前是否处于可接触和可进入的窗口期", "weight_pct": 20},
        ],
    },
    "exit_feasibility": {
        "label": "退出可行性",
        "sub": [
            {"key": "ipo_comparable", "label": "IPO可比", "desc": "是否有同赛道/同类型的IPO可比案例", "weight_pct": 30},
            {"key": "ma_appetite", "label": "并购承接", "desc": "行业内是否有活跃的产业并购方", "weight_pct": 25},
            {"key": "exit_timeline", "label": "退出时间窗口", "desc": "预期多久可以退出（3/5/7年）", "weight_pct": 25},
            {"key": "buyer_density", "label": "产业买家密度", "desc": "潜在产业买家的数量和质量", "weight_pct": 20},
        ],
    },
    "competitive_position": {
        "label": "竞争卡位",
        "sub": [
            {"key": "differentiation", "label": "差异化", "desc": "与直接竞争对手的核心差异点", "weight_pct": 30},
            {"key": "market_position", "label": "行业地位", "desc": "市场份额/品牌认知/行业排名", "weight_pct": 25},
            {"key": "customer_mindshare", "label": "客户心智", "desc": "客户提到该品类时是否首先想到该公司", "weight_pct": 25},
            {"key": "platform_extension", "label": "平台化延展", "desc": "是否具备从单点产品向平台/生态延展的能力", "weight_pct": 20},
        ],
    },
    "information_sufficiency": {
        "label": "信息充分度",
        "sub": [
            {"key": "public_evidence", "label": "公开证据", "desc": "公开可得信息的数量和质量", "weight_pct": 35},
            {"key": "cross_validation", "label": "交叉验证", "desc": "多独立信源之间的一致性", "weight_pct": 35},
            {"key": "info_consistency", "label": "信息一致性", "desc": "各渠道信息是否存在矛盾", "weight_pct": 30},
        ],
    },
}

DIMENSION_GUIDE = {key: value["label"] + "：" + " / ".join(item["label"] for item in value["sub"]) for key, value in SUB_DIMENSIONS.items()}

DIMENSION_SCORING_ANCHORS = {
    "sector_prosperity": {
        5: "近180天景气催化明确，需求拐点与渗透率提升同时成立；政策+资本+订单三重共振。",
        4: "赛道处于快速成长期，有明确政策催化或需求拐点，但渗透率提升尚需1-2年验证。",
        3: "赛道中性偏上，有明确空间但证据未完全共振；周期位置偏中段。",
        2: "赛道逻辑存在但偏早期或偏成熟，近期催化弱，需求验证不充分。",
        1: "赛道逻辑偏泛，缺少近期催化或需求验证；或已进入衰退/过度竞争阶段。",
    },
    "profit_pool": {
        5: "处于高毛利(>50%)/高议价核心环节，并具备后市场或软件化收入；切换成本极高。",
        4: "利润池较厚(毛利40-50%)，议价能力强，有后市场潜力但尚未充分释放。",
        3: "利润池存在但并非最厚(毛利25-40%)，议价能力与可持续性一般。",
        2: "利润池偏薄(毛利15-25%)，或处于价值链中间位置，上下游均有挤压。",
        1: "利润池薄(<15%)、价值链位置弱或高度依赖集成/交付/人工。",
    },
    "high_end_attribute": {
        5: "技术/工艺/认证壁垒强，国产替代或卡位价值清晰；核心专利>10项，研发投入>15%。",
        4: "壁垒较强，有明确技术护城河或认证门槛，但国际竞对仍有优势。",
        3: "存在一定壁垒，但仍可能被2-3年内复制；认证门槛中等。",
        2: "壁垒有限，主要靠工程经验或先发优势，新进入者可在1-2年内追上。",
        1: "技术含量有限，壁垒更多来自关系或项目执行；几乎无专利布局。",
    },
    "customer_validation": {
        5: "标杆客户(行业TOP3)导入+复购+扩单+跨行业复制均有公开证据；战略供应商地位确立。",
        4: "有标杆客户导入和小批量复购证据，但跨行业复制刚起步或扩单规模待验证。",
        3: "已有客户或案例，但导入深度停留在小批量/试用阶段；复购尚待验证。",
        2: "有少量客户接触或POC(概念验证)，但无公开签约/交付/复购证据。",
        1: "只有概念验证或单点试用，没有稳定客户证据；或客户质量偏低。",
    },
    "commercialization_progress": {
        5: "量产/订单/交付链路成熟，收入YoY>80%；规模化效率显现，人均产出提升。",
        4: "商业化进入快速增长期，收入YoY>50%，但供应链或产能仍是瓶颈。",
        3: "商业化启动明确，有稳定收入但量产与效率尚未完全跑通；YoY 20-50%。",
        2: "有零星收入但仍偏项目制；从产品到收入的转化路径存在卡点。",
        1: "仍偏项目制或试点期，规模化路径不清晰；收入<1000万或无收入。",
    },
    "investability": {
        5: "一级属性清晰，B轮前后，估值<15xPS合理，融资窗口开放，股权结构干净。",
        4: "可投属性明确，轮次匹配，但估值需谈判或交易结构有小瑕疵(对赌/优先权)。",
        3: "可投但窗口、估值或交易结构仍需谈判验证；或融资信息不完整。",
        2: "投资属性存疑：轮次偏早/偏晚，估值偏高，或股权结构有明显问题。",
        1: "明显不符合一级投资主体、轮次或交易进入条件；上市公司或pre-IPO溢价过高。",
    },
    "exit_feasibility": {
        5: "可比退出案例≥2个、产业买家≥3个、退出路径(IPO/并购)和时间线(3-5年)清晰。",
        4: "退出路径有1-2个锚点(可比IPO或活跃并购方)，但时间线或倍数仍需验证。",
        3: "退出逻辑有初步锚点，但承接方或路径仍不够扎实；退出窗口>5年。",
        2: "退出路径模糊，仅有概念性的IPO或并购可能，缺少可比案例支撑。",
        1: "退出锚点缺失，缺少产业并购或IPO可比支撑；行业内无活跃退出先例。",
    },
    "competitive_position": {
        5: "行业前3卡位明确，差异化强，客户心智占领；护城河可量化(份额>20%或NPS领先)。",
        4: "行业前5，差异化清晰，有护城河但格局仍在演变；1-2个关键差异点。",
        3: "有差异化但格局未定，仍需与同类持续比较；市场份额<10%。",
        2: "差异化不显著，与竞品主要靠价格或关系竞争；护城河主张缺少证据。",
        1: "缺少明确卡位，竞争优势更多停留在宣传层；同质化严重。",
    },
    "information_sufficiency": {
        5: "多源(≥5域名)、近期(≥4条180天内)、交叉验证充分，证据和口径基本一致。",
        4: "公开信息较充分(≥6条/≥4域名)，关键判断有交叉验证，少量缺口。",
        3: "公开信息可用(≥4条/≥3域名)，但仍有关键缺口或时间滞后。",
        2: "公开信息有限(2-3条)，来源单一，关键数据依赖推测。",
        1: "公开证据很少(<2条)，主要依赖推测或单一弱来源。",
    },
}

OBJECTIVE_BLEND_WEIGHTS = {
    "customer_validation": 0.50,
    "commercialization_progress": 0.55,
    "investability": 0.45,
    "exit_feasibility": 0.45,
    "competitive_position": 0.65,
    "information_sufficiency": 0.30,
}

CONFIDENCE_FACTOR = {
    "high": 1.00,
    "medium_high": 0.90,
    "medium": 0.80,
    "medium_low": 0.70,
    "low": 0.55,
}

CONFIDENCE_INDEX_SCORE = {
    "high": 100.0,
    "medium_high": 80.0,
    "medium": 60.0,
    "medium_low": 40.0,
    "low": 20.0,
}

EXCLUSION_PENALTY = {
    "标准耗材型": {"penalty": 3, "severity": "低", "desc": "产品以标准耗材为主，差异化和壁垒偏弱，但可能有规模效应"},
    "区域性强": {"penalty": 3, "severity": "低", "desc": "业务高度依赖特定区域，全国化/全球化复制难度大"},
    "单一产品依赖": {"penalty": 4, "severity": "低", "desc": "收入>80%来自单一产品，产品线扩展能力未验证"},
    "配套功能件型": {"penalty": 5, "severity": "中", "desc": "主要为主机/系统配套的标准功能件，独立价值偏弱"},
    "工程项目型": {"penalty": 5, "severity": "中", "desc": "收入高度依赖定制项目交付，标准化产品占比低，人均产出天花板明确"},
    "宽口径拼装型": {"penalty": 5, "severity": "中", "desc": "产品以集成/拼装为主，核心部件外购比例高(>60%)"},
    "客户集中度过高": {"penalty": 5, "severity": "中", "desc": "前3大客户占收入>70%，大客户流失风险显著"},
    "团队经验不匹配": {"penalty": 5, "severity": "中", "desc": "创始团队背景与当前业务方向存在明显Gap"},
    "独立客户入口弱": {"penalty": 6, "severity": "高", "desc": "缺乏直接面对终端客户的能力，被渠道/集成商把控"},
    "利润池过薄": {"penalty": 6, "severity": "高", "desc": "所处价值链位置利润空间有限(毛利<15%)，即使放量也难改善"},
    "退出锚缺失": {"penalty": 7, "severity": "高", "desc": "缺乏可参照的IPO或并购退出案例，行业内无活跃退出先例"},
    "估值严重偏高": {"penalty": 7, "severity": "高", "desc": "当前估值远超业务进展支撑(PS>30x或亏损期估值>行业中位数3倍)"},
    "合规/诉讼风险": {"penalty": 8, "severity": "高", "desc": "存在重大合规问题、未决诉讼或监管处罚记录"},
    "上市公司仅作参照": {"penalty": 20, "severity": "一票否决", "desc": "上市公司不作为一级推荐主体"},
    "重大造假嫌疑": {"penalty": 20, "severity": "一票否决", "desc": "有公开报道或证据指向财务造假/数据虚构"},
}

SECTOR_WEIGHT_PROFILES = {
    "hardware": {
        "desc": "硬件/设备/传感器类 — 壁垒看技术+工艺，商业化看量产能力",
        "adjustments": {
            "high_end_attribute": 1.15,
            "profit_pool": 1.10,
            "commercialization_progress": 1.10,
            "information_sufficiency": 0.90,
        },
    },
    "software": {
        "desc": "工业软件/SaaS/算法类 — 壁垒看客户粘性，退出看ARR增速",
        "adjustments": {
            "competitive_position": 1.20,
            "customer_validation": 1.10,
            "high_end_attribute": 0.90,
            "profit_pool": 1.15,
        },
    },
    "platform": {
        "desc": "平台型/生态型/底座型 — 壁垒看网络效应，退出看生态粘性",
        "adjustments": {
            "competitive_position": 1.25,
            "customer_validation": 1.15,
            "exit_feasibility": 1.10,
            "commercialization_progress": 0.90,
        },
    },
    "robotics": {
        "desc": "机器人/自动化本体类 — 壁垒看软硬结合，商业化看场景落地",
        "adjustments": {
            "high_end_attribute": 1.10,
            "commercialization_progress": 1.15,
            "profit_pool": 1.05,
            "exit_feasibility": 1.05,
        },
    },
    "semiconductor": {
        "desc": "半导体/芯片类 — 壁垒看制程/IP/认证，商业化看设计导入",
        "adjustments": {
            "high_end_attribute": 1.25,
            "customer_validation": 1.15,
            "profit_pool": 1.10,
            "information_sufficiency": 0.85,
            "commercialization_progress": 0.90,
        },
    },
    "medical_device": {
        "desc": "医疗器械/IVD类 — 壁垒看注册证+临床，退出看集采影响",
        "adjustments": {
            "high_end_attribute": 1.20,
            "exit_feasibility": 1.15,
            "customer_validation": 1.10,
            "sector_prosperity": 0.90,
        },
    },
    "new_energy": {
        "desc": "新能源/储能/氢能类 — 壁垒看成本曲线，商业化看装机量",
        "adjustments": {
            "sector_prosperity": 1.15,
            "commercialization_progress": 1.15,
            "profit_pool": 1.10,
            "competitive_position": 0.90,
        },
    },
    "new_material": {
        "desc": "新材料/特种化学品类 — 壁垒看配方+工艺，商业化看认证周期",
        "adjustments": {
            "high_end_attribute": 1.20,
            "profit_pool": 1.15,
            "customer_validation": 1.10,
            "competitive_position": 0.90,
        },
    },
    "default": {"desc": "通用 — 无特殊微调", "adjustments": {}},
}

TRACKING_ORDER = {"立项": 0, "深跟": 1, "约访": 2, "补证": 3, "观察": 4, "放弃": 5}
TRACKING_SIGNAL = {"立项": 100, "深跟": 85, "约访": 70, "补证": 55, "观察": 40, "放弃": 10}
RISK_LEVEL_SCORE = {"低": 1, "中": 2, "高": 3, "一票否决": 4}
PLACEHOLDER_TEXTS = {"", "待核实", "未知", "待补充", "无", "暂无", "未披露", "待生成", "n/a", "na"}
EVIDENCE_DISCIPLINE_KEYS = {
    "customer_validation",
    "commercialization_progress",
    "investability",
    "exit_feasibility",
    "information_sufficiency",
}
SOURCE_TIER_SCORE = {"T1": 100, "T1/T2": 90, "T2": 78, "T2/T3": 62, "T3": 45}
SOCIAL_PLATFORMS = {"微信公众号", "微信视频号", "小红书", "知识星球", "小报童", "知乎", "微博", "B站", "抖音", "快手", "雪球", "韭研公社"}
NEGATIVE_EVENT_KEYWORDS = {
    "失败", "取消", "撤销", "终止", "延期", "延迟", "暂停", "未能",
    "尚未量产", "尚未交付", "尚未实现收入", "尚未获客", "尚未完成",
    "没有订单", "没有收入", "没有客户", "没有复购",
    "暂无订单", "暂无收入", "暂无客户", "暂无复购",
    "流失", "下滑", "召回", "违约", "否决", "终止合作",
}
DIMENSION_CLAIM_HINTS = {
    "sector_prosperity": {"demand_signal", "policy_signal", "commercial_signal"},
    "profit_pool": {"commercial_signal", "partnership_signal"},
    "high_end_attribute": {"product_signal", "founder_signal", "partnership_signal"},
    "customer_validation": {"partnership_signal", "commercial_signal"},
    "commercialization_progress": {"commercial_signal", "product_signal", "partnership_signal"},
    "investability": {"commercial_signal", "founder_signal", "risk_signal"},
    "exit_feasibility": {"commercial_signal", "partnership_signal", "competitive_signal"},
    "competitive_position": {"product_signal", "competitive_signal", "partnership_signal", "commercial_signal"},
    "information_sufficiency": set(),
}

HARD_GATES = [
    {"name": "一级属性门槛", "cap": 35.0},
    {"name": "证据充分度门槛", "cap": 57.0},
    {"name": "信息缺口门槛", "cap": 60.0},
    {"name": "财务数据门槛", "cap": 62.0},
    {"name": "退出锚门槛", "cap": 65.0},
    {"name": "竞争格局门槛", "cap": 68.0},
    {"name": "时效性门槛", "cap": 70.0},
    {"name": "放弃状态门槛", "cap": 30.0},
]

SOURCE_TIER_ALIASES = {
    "primary_official": "T1",
    "platform_official": "T1/T2",
    "industry_db": "T1/T2",
    "mainstream_media": "T2",
    "social_post": "T3",
    "aggregator": "T2/T3",
    "unknown": "T3",
}

DOMAIN_TIER_HINTS = {
    "gov.cn": "T1",
    "miit.gov.cn": "T1",
    "ndrc.gov.cn": "T1",
    "stats.gov.cn": "T1",
    "cninfo.com.cn": "T1",
    "sse.com.cn": "T1",
    "szse.cn": "T1",
    "36kr.com": "T2",
    "leiphone.com": "T2",
    "ofweek.com": "T2",
    "pedaily.cn": "T2",
    "chinaventure.com.cn": "T2",
    "iyiou.com": "T2",
    "itjuzi.com": "T1/T2",
    "pedata.cn": "T1/T2",
    "tianyancha.com": "T1/T2",
    "qcc.com": "T1/T2",
    "mp.weixin.qq.com": "T2/T3",
    "weixin.qq.com": "T2/T3",
    "channels.weixin.qq.com": "T2/T3",
    "xiaohongshu.com": "T3",
    "zhihu.com": "T3",
    "weibo.com": "T3",
    "bilibili.com": "T3",
    "douyin.com": "T3",
    "kuaishou.com": "T3",
}

LEGAL_ENTITY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "集团股份",
    "股份公司",
    "集团",
    "公司",
    "co.,ltd",
    "co.ltd",
    "coltd",
    "limited",
    "ltd",
    "incorporated",
    "inc",
)

SCORING_TEMPLATE = {
    "version": "PE-V3.3-trust",
    "formula": "总分 = Σ(维度权重 × 校准后档位/5 × 置信度系数) − 排除惩罚 − 风险惩罚 − 证据链惩罚，再经过硬门槛封顶",
    "dimensions": [
        {
            "key": key,
            "label": DIMENSION_LABELS[key],
            "weight": DIMENSION_WEIGHTS[key],
            "weight_band": WEIGHT_BAND[key],
            "guide": DIMENSION_GUIDE[key],
            "anchors": DIMENSION_SCORING_ANCHORS[key],
            "sub_dimensions": SUB_DIMENSIONS[key]["sub"],
        }
        for key in DIMENSION_WEIGHTS
    ],
    "hard_gates": HARD_GATES,
}


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_level(value: Any) -> int:
    return int(clamp(safe_int(value, 0), 0, 5))


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_date_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip().replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")


def parse_event_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = normalize_date_text(value)
    if not text:
        return None
    try:
        return dtparser.parse(text)
    except Exception:
        match = re.search(r"(20\d{2})-(\d{1,2})(?:-(\d{1,2}))?", text)
        if not match:
            return None
        day = int(match.group(3) or "1")
        try:
            return datetime(int(match.group(1)), int(match.group(2)), day)
        except ValueError:
            return None


def days_since(date_obj: datetime | None, now: datetime | None = None) -> int | None:
    if not date_obj:
        return None
    current = now or datetime.now(timezone.utc)
    dt = date_obj
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    delta = current.astimezone(timezone.utc) - dt.astimezone(timezone.utc)
    if delta.total_seconds() < 0:
        return None
    return int(delta.days)


def evidence_fact_date(item: dict[str, Any]) -> datetime | None:
    """Return a scoreable fact date, never a collection timestamp.

    Event date takes precedence over publication date.  ``captured_at`` is
    lineage metadata and must not make an undated or future fact look recent.
    """
    dt = parse_event_date(item.get("event_date") or item.get("date"))
    if dt is None:
        dt = parse_event_date(item.get("published_at"))
    return dt if days_since(dt) is not None else None


def is_negative_evidence(item: dict[str, Any]) -> bool:
    stance = str(item.get("stance") or "").strip().lower()
    claim_type = str(item.get("claim_type") or item.get("event_type") or "").strip()
    if stance == "negative" or claim_type in {"risk_signal", "contradiction"}:
        return True
    blob = " ".join(
        str(item.get(key) or "")
        for key in ("title", "source_title", "summary", "quote", "event_type")
    ).lower()
    return any(keyword in blob for keyword in NEGATIVE_EVENT_KEYWORDS)


def canonical_entity_name(value: Any) -> str:
    """Collapse punctuation and legal suffix variants without guessing unrelated aliases."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[（(][^）)]{0,60}[）)]", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    changed = True
    while changed and text:
        changed = False
        for suffix in LEGAL_ENTITY_SUFFIXES:
            normalized_suffix = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", suffix.lower())
            if text.endswith(normalized_suffix) and len(text) > len(normalized_suffix) + 1:
                text = text[: -len(normalized_suffix)]
                changed = True
                break
    return text


def normalize_source_url(url: Any) -> str:
    text = (url or "").strip()
    return text if re.match(r"^https?://", text, re.I) else ""


def extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().strip()
    except Exception:
        return ""


def domain_matches(domain: str, target: str) -> bool:
    domain = (domain or "").lower().strip(".")
    target = (target or "").lower().strip(".")
    return bool(domain and target and (domain == target or domain.endswith(f".{target}")))


def infer_source_pack_from_domain(domain: str) -> tuple[str, dict[str, str]]:
    d = (domain or "").lower()
    if not d:
        return "", {}
    for target, tier in DOMAIN_TIER_HINTS.items():
        if domain_matches(d, target):
            return target, {"tier": tier}
    return "", {}


def score_source_tier(tier: str) -> int:
    return SOURCE_TIER_SCORE.get(tier or "T3", 40)


def is_meaningful_text(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped and stripped.lower() not in PLACEHOLDER_TEXTS)
    if isinstance(value, dict):
        return any(is_meaningful_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(is_meaningful_text(item) for item in value)
    return True


def count_meaningful_values(values: list[Any]) -> int:
    return sum(1 for item in values if is_meaningful_text(item))


def score_bucket(score: float) -> str:
    if score >= 78:
        return "最终推荐"
    if score >= 68:
        return "重点跟踪"
    if score >= 58:
        return "备选观察"
    if score >= 45:
        return "待补证"
    return "低优先级"


def score_grade(score: float) -> str:
    if score >= 82:
        return "A+"
    if score >= 76:
        return "A"
    if score >= 68:
        return "B+"
    if score >= 60:
        return "B"
    if score >= 52:
        return "C+"
    if score >= 44:
        return "C"
    return "D"


def domain_to_platform(domain: str) -> str:
    d = (domain or "").lower()
    platform_map = {
        "mp.weixin.qq.com": "微信公众号",
        "weixin.qq.com": "微信公众号",
        "channels.weixin.qq.com": "微信视频号",
        "developers.weixin.qq.com": "微信开放平台",
        "open.weixin.qq.com": "微信开放平台",
        "xiaohongshu.com": "小红书",
        "miniapp.xiaohongshu.com": "小红书小程序",
        "zsxq.com": "知识星球",
        "doc.zsxq.com": "知识星球",
        "xiaobot.net": "小报童",
        "yuque.com": "语雀",
        "flowus.cn": "FlowUs",
        "feishu.cn": "飞书文档",
        "notion.site": "Notion",
        "xueqiu.com": "雪球",
        "jiuyangongshe.com": "韭研公社",
        "cls.cn": "财联社",
        "eastmoney.com": "东方财富",
        "cninfo.com.cn": "巨潮资讯",
        "36kr.com": "36氪",
        "leiphone.com": "雷锋网",
        "ofweek.com": "OFweek",
        "pedaily.cn": "投资界",
        "chinaventure.com.cn": "投中网",
        "iyiou.com": "亿欧",
        "itjuzi.com": "IT桔子",
        "pedata.cn": "清科数据",
        "zero2ipo.com.cn": "清科",
        "tianyancha.com": "天眼查",
        "qcc.com": "企查查",
        "aiqicha.com": "爱企查",
        "zhipin.com": "BOSS直聘",
        "liepin.com": "猎聘",
        "lagou.com": "拉勾",
        "ccgp.gov.cn": "中国政府采购网",
        "soopat.com": "SooPAT专利",
        "zhihu.com": "知乎",
        "weibo.com": "微博",
        "bilibili.com": "B站",
        "douyin.com": "抖音",
        "kuaishou.com": "快手",
        "github.com": "GitHub",
        "gitee.com": "Gitee",
        "gongkong.com": "工控网",
        "ca800.com": "中国自动化网",
    }
    for key, label in platform_map.items():
        if key in d:
            return label
    return domain or "未知来源"


def credibility_from_tier(tier: str) -> str:
    if tier == "T1":
        return "high"
    if tier == "T1/T2":
        return "medium_high"
    if tier == "T2":
        return "medium"
    if tier == "T2/T3":
        return "medium_low"
    return "low"


def normalize_source_tier(tier: str, domain: str = "") -> str:
    raw = (tier or "").strip()
    if raw in SOURCE_TIER_SCORE:
        return raw
    if raw in SOURCE_TIER_ALIASES:
        return SOURCE_TIER_ALIASES[raw]
    if raw:
        lowered = raw.lower()
        if lowered in SOURCE_TIER_ALIASES:
            return SOURCE_TIER_ALIASES[lowered]
    return infer_source_pack_from_domain(domain)[1].get("tier") or "T3"


def confidence_label_from_score(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 65:
        return "medium_high"
    if score >= 50:
        return "medium"
    if score >= 35:
        return "medium_low"
    return "low"


def weighted_level(sub_levels: list[dict[str, Any]]) -> int:
    if not sub_levels:
        return 0
    weighted = sum(item["level"] * item["weight_pct"] for item in sub_levels)
    return clamp_level(round(weighted / 100.0))


def blend_score_level(key: str, model_level: int, objective_level: int | None) -> int:
    if objective_level is None:
        return model_level
    model_weight = OBJECTIVE_BLEND_WEIGHTS.get(key, 0.75)
    blended = round(model_level * model_weight + objective_level * (1 - model_weight))
    return clamp_level(blended)


def compute_confidence_index(confidence_levels: dict[str, str]) -> float:
    if not confidence_levels:
        return 0.0
    avg = sum(CONFIDENCE_INDEX_SCORE.get(value, 20.0) for value in confidence_levels.values()) / max(len(confidence_levels), 1)
    return round(avg, 1)


def normalize_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    url = normalize_source_url(item.get("source_url"))
    domain = extract_domain(url)
    event_date = parse_event_date(item.get("event_date") or item.get("date"))
    published_at = parse_event_date(item.get("published_at"))
    captured_at = parse_event_date(item.get("captured_at"))
    fact_date = event_date or published_at
    if days_since(fact_date) is None:
        fact_date = None
    date_text = fact_date.isoformat() if fact_date else ""
    tier = normalize_source_tier(item.get("source_tier", ""), domain)
    title = (item.get("source_title") or "").strip()
    quote = (item.get("quote") or "").strip()
    tags = [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()]
    traceability = "missing"
    if url and title and quote and item.get("quote_verified") is True:
        traceability = "verified"
    elif url and (title or quote):
        traceability = "matched"
    elif url:
        traceability = "declared"
    platform = (item.get("platform") or "").strip() or domain_to_platform(domain)
    claim_type = (item.get("claim_type") or "").strip()
    stance = (item.get("stance") or "").strip() or "neutral"
    summary = quote or title
    blob = " ".join([item.get("entity", ""), title, quote, " ".join(tags), claim_type, stance]).lower()
    return {
        **item,
        "source_url": url,
        "source_domain": domain,
        "source_tier": tier,
        "event_date": event_date.isoformat() if event_date else item.get("event_date"),
        "published_at": published_at.isoformat() if published_at else item.get("published_at"),
        "captured_at": captured_at.isoformat() if captured_at else item.get("captured_at"),
        "date": date_text,
        "title": title,
        "summary": summary,
        "event_type": (item.get("event_type") or claim_type),
        "traceability": traceability,
        "platform": platform,
        "tags": tags,
        "_blob": blob,
    }


def detect_sector_type(company: dict[str, Any]) -> str:
    evidence_text = " ".join(
        " ".join(
            [
                str(item.get("title") or item.get("source_title") or ""),
                str(item.get("summary") or item.get("quote") or ""),
                " ".join(str(tag) for tag in (item.get("tags") or [])),
            ]
        )
        for item in (company.get("evidence") or [])
    )
    text = " ".join([
        company.get("sector", ""),
        company.get("core_product", ""),
        company.get("company_positioning", ""),
        company.get("value_node", ""),
        evidence_text,
    ]).lower()
    if any(kw in text for kw in ["芯片", "半导体", "ic设计", "晶圆", "封装", "eda", "ip核", "fpga", "mcu", "soc"]):
        return "semiconductor"
    if any(kw in text for kw in ["医疗", "器械", "ivd", "体外诊断", "内窥镜", "影像", "植入", "耗材", "临床"]):
        return "medical_device"
    if any(kw in text for kw in ["储能", "光伏", "风电", "氢能", "新能源", "锂电", "电池", "充电", "逆变器"]):
        return "new_energy"
    if any(kw in text for kw in ["材料", "化学品", "涂料", "树脂", "膜", "粉体", "纤维", "碳", "陶瓷"]):
        return "new_material"
    if any(kw in text for kw in ["软件", "saas", "算法", "数据", "系统软件", "操作系统", "中间件", "云"]):
        return "software"
    if any(kw in text for kw in ["平台", "底座", "生态", "调度", "marketplace"]):
        return "platform"
    if any(kw in text for kw in ["机器人", "协作", "移动机器人", "amr", "agv", "机械臂", "exo"]):
        return "robotics"
    if any(kw in text for kw in ["传感", "视觉", "检测", "硬件", "设备", "仪器", "部件", "模组"]):
        return "hardware"
    return "default"


def get_adjusted_weights(sector_type: str) -> dict[str, float]:
    profile = SECTOR_WEIGHT_PROFILES.get(sector_type, SECTOR_WEIGHT_PROFILES["default"])
    adjustments = profile["adjustments"]
    adjusted: dict[str, float] = {}
    total = 0.0
    for key, base_weight in DIMENSION_WEIGHTS.items():
        weight = base_weight * adjustments.get(key, 1.0)
        adjusted[key] = weight
        total += weight
    if total > 0:
        factor = 100.0 / total
        for key in adjusted:
            adjusted[key] = round(adjusted[key] * factor, 2)
    return adjusted


def infer_stage_fit_level(stage_text: str) -> int:
    text = (stage_text or "").strip().lower()
    if not text:
        return 2
    if "种子" in text or "天使" in text or "pre-a" in text or "prea" in text:
        return 5
    if "a+" in text or "a轮" in text or "a round" in text:
        return 4
    if "b轮" in text or "b round" in text:
        return 3
    if "c轮" in text or "c round" in text:
        return 2
    if "d轮" in text or "pre-ipo" in text or "ipo" in text or "后期" in text:
        return 1
    if "成长" in text:
        return 3
    return 2


def collect_evidence_stats(company: dict[str, Any]) -> dict[str, Any]:
    evidence = company.get("evidence") or []
    unique_domains: set[str] = set()
    dated_count = 0
    recent_90 = 0
    recent_180 = 0
    traceable_count = 0
    verified_count = 0
    t1_count = 0
    t2_count = 0
    social_count = 0
    authoritative_count = 0
    official_anchor_count = 0
    multi_provider_retrieval_count = 0
    independently_corroborated_count = 0
    for item in evidence:
        domain = (item.get("source_domain") or "").strip().lower()
        if not domain and item.get("source_url"):
            domain = extract_domain(item["source_url"])
        if domain:
            unique_domains.add(domain)
        if normalize_source_url(item.get("source_url", "")):
            traceable_count += 1
        if item.get("traceability") == "verified":
            verified_count += 1
        source_tier = normalize_source_tier(item.get("source_tier") or "", domain)
        tier_score = score_source_tier(source_tier)
        if source_tier in {"T1", "T1/T2"}:
            official_anchor_count += 1
        if source_tier == "T1":
            t1_count += 1
        elif tier_score >= SOURCE_TIER_SCORE["T2"]:
            t2_count += 1
        if tier_score >= SOURCE_TIER_SCORE["T2"]:
            authoritative_count += 1
        retrieval_count = safe_int(
            item.get("retrieval_provider_count"),
            safe_int(item.get("provider_count"), 0),
        )
        if retrieval_count >= 2:
            multi_provider_retrieval_count += 1
        if bool(item.get("independently_corroborated")) and safe_int(item.get("independent_source_count"), 0) >= 2:
            independently_corroborated_count += 1
        platform = item.get("platform") or domain_to_platform(domain)
        if platform in SOCIAL_PLATFORMS:
            social_count += 1
        dt = evidence_fact_date(item)
        if not dt:
            continue
        dated_count += 1
        age = days_since(dt)
        if age is None:
            continue
        if age <= 90:
            recent_90 += 1
        if age <= 180:
            recent_180 += 1
    total_evidence = len(evidence)
    freshness_score = round(min(100.0, recent_90 * 22 + max(0, recent_180 - recent_90) * 11 + dated_count * 4), 1)
    traceability_score = 0.0
    if total_evidence:
        traceability_score = round(
            min(
                100.0,
                ((verified_count * 1.0 + max(0, traceable_count - verified_count) * 0.65) / total_evidence) * 100,
            ),
            1,
        )
    authority_score = round(min(100.0, t1_count * 28 + t2_count * 16 + len(unique_domains) * 6), 1) if total_evidence else 0.0
    cross_validation_score = round(
        min(100.0, independently_corroborated_count * 35),
        1,
    ) if total_evidence else 0.0
    corroborated_claim_count = independently_corroborated_count
    social_dependency_ratio = round(social_count / max(total_evidence, 1), 2) if total_evidence else 0.0
    evidence_chain_score = round(
        max(
            0.0,
            min(
                100.0,
                official_anchor_count * 18
                + max(0, authoritative_count - official_anchor_count) * 7
                + independently_corroborated_count * 18
                + corroborated_claim_count * 12
                + verified_count * 6
                + min(len(unique_domains), 6) * 5
                + min(recent_180, 5) * 4
                - (18 if social_dependency_ratio >= 0.60 and official_anchor_count == 0 else 0),
            ),
        ),
        1,
    ) if total_evidence else 0.0
    reliability_score = round(
        freshness_score * 0.20 + traceability_score * 0.25 + authority_score * 0.20 + cross_validation_score * 0.15 + evidence_chain_score * 0.20,
        1,
    ) if total_evidence else 0.0
    evidence_strength = round(
        min(
            100.0,
            total_evidence * 10 + len(unique_domains) * 9 + recent_90 * 10 + recent_180 * 5 + verified_count * 6 + authoritative_count * 4,
        ),
        1,
    )
    return {
        "total": total_evidence,
        "dated": dated_count,
        "recent_90": recent_90,
        "recent_180": recent_180,
        "unique_domains": len(unique_domains),
        "evidence_strength": evidence_strength,
        "traceable_count": traceable_count,
        "verified_count": verified_count,
        "t1_count": t1_count,
        "t2_count": t2_count,
        "authoritative_count": authoritative_count,
        "official_anchor_count": official_anchor_count,
        "social_count": social_count,
        "social_dependency_ratio": social_dependency_ratio,
        "cross_provider_count": multi_provider_retrieval_count,
        "multi_provider_retrieval_count": multi_provider_retrieval_count,
        "independently_corroborated_count": independently_corroborated_count,
        "corroborated_claim_count": corroborated_claim_count,
        "freshness_score": freshness_score,
        "traceability_score": traceability_score,
        "authority_score": authority_score,
        "cross_validation_score": cross_validation_score,
        "evidence_chain_score": evidence_chain_score,
        "reliability_score": reliability_score,
    }


def build_dimension_support_map(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    support_map: dict[str, dict[str, Any]] = {}
    for key in DIMENSION_WEIGHTS:
        claim_hints = DIMENSION_CLAIM_HINTS.get(key, set())
        relevant = [
            item for item in evidence
            if not claim_hints or str(item.get("claim_type") or item.get("event_type") or "").strip() in claim_hints
        ]
        domains: set[str] = set()
        authoritative_count = 0
        verified_count = 0
        recent_180 = 0
        social_count = 0
        multi_provider_retrieval_count = 0
        independently_corroborated_count = 0
        for item in relevant:
            domain = (item.get("source_domain") or "").strip().lower()
            if not domain and item.get("source_url"):
                domain = extract_domain(item["source_url"])
            if domain:
                domains.add(domain)
            tier = normalize_source_tier(item.get("source_tier") or "", domain)
            if score_source_tier(tier) >= SOURCE_TIER_SCORE["T1/T2"]:
                authoritative_count += 1
            if item.get("traceability") == "verified":
                verified_count += 1
            retrieval_count = safe_int(
                item.get("retrieval_provider_count"),
                safe_int(item.get("provider_count"), 0),
            )
            if retrieval_count >= 2:
                multi_provider_retrieval_count += 1
            if bool(item.get("independently_corroborated")) and safe_int(item.get("independent_source_count"), 0) >= 2:
                independently_corroborated_count += 1
            platform = item.get("platform") or domain_to_platform(domain)
            if platform in SOCIAL_PLATFORMS:
                social_count += 1
            dt = evidence_fact_date(item)
            age = days_since(dt) if dt else None
            if age is not None and age <= 180:
                recent_180 += 1
        corroborated_claim_count = independently_corroborated_count
        count = len(relevant)
        social_only = bool(count and social_count == count and authoritative_count == 0)
        support_score = round(
            max(
                0.0,
                min(
                    100.0,
                    count * 10
                    + authoritative_count * 14
                    + verified_count * 12
                    + recent_180 * 8
                    + len(domains) * 10
                    + corroborated_claim_count * 22
                    - (12 if social_only else 0),
                ),
            ),
            1,
        ) if count else 0.0
        support_map[key] = {
            "count": count,
            "unique_domains": len(domains),
            "authoritative_count": authoritative_count,
            "verified_count": verified_count,
            "recent_180": recent_180,
            "cross_provider_count": multi_provider_retrieval_count,
            "multi_provider_retrieval_count": multi_provider_retrieval_count,
            "independently_corroborated_count": independently_corroborated_count,
            "corroborated_claim_count": corroborated_claim_count,
            "social_only": social_only,
            "support_score": support_score,
        }
    return support_map


def derive_objective_levels(company: dict[str, Any], evidence_stats: dict[str, Any]) -> dict[str, int]:
    financial = company.get("financial_profile") or {}
    exit_mapping = company.get("exit_mapping") or {}
    moat = company.get("moat_analysis") or {}
    evidence = company.get("evidence") or []
    competitors = company.get("competitive_landscape") or []

    time_weighted_evidence = (
        evidence_stats["recent_90"] * 2.0
        + (evidence_stats["recent_180"] - evidence_stats["recent_90"]) * 1.5
        + (evidence_stats["total"] - evidence_stats["recent_180"]) * 0.5
    )
    domain_diversity = evidence_stats["unique_domains"]
    info_level = 0
    if (
        time_weighted_evidence >= 12
        and domain_diversity >= 5
        and evidence_stats["recent_90"] >= 3
        and evidence_stats["traceability_score"] >= 75
        and evidence_stats["authority_score"] >= 65
    ):
        info_level = 5
    elif (
        time_weighted_evidence >= 8
        and domain_diversity >= 4
        and evidence_stats["recent_180"] >= 3
        and evidence_stats["traceability_score"] >= 55
    ):
        info_level = 4
    elif time_weighted_evidence >= 5 and domain_diversity >= 3 and evidence_stats["traceable_count"] >= 2:
        info_level = 3
    elif time_weighted_evidence >= 2 and domain_diversity >= 2:
        info_level = 2
    elif evidence_stats["total"] >= 1:
        info_level = 1
    if evidence_stats["recent_180"] == 0 and evidence_stats["total"] > 0:
        info_level = min(info_level, 3)
    if evidence_stats["social_dependency_ratio"] >= 0.75 and evidence_stats["authoritative_count"] == 0:
        info_level = min(info_level, 2)

    exit_fields = {
        "comparable_exits": exit_mapping.get("comparable_exits"),
        "potential_acquirers": exit_mapping.get("potential_acquirers"),
        "ipo_timeline": exit_mapping.get("ipo_timeline"),
        "target_multiple": exit_mapping.get("target_multiple"),
        "exit_path_preference": exit_mapping.get("exit_path_preference"),
    }
    exit_completeness = count_meaningful_values(list(exit_fields.values()))
    has_core_exit_info = is_meaningful_text(exit_fields["comparable_exits"]) and is_meaningful_text(exit_fields["potential_acquirers"])
    if exit_completeness >= 5 and has_core_exit_info:
        exit_level = 5
    elif exit_completeness >= 4 and has_core_exit_info:
        exit_level = 4
    elif exit_completeness >= 3:
        exit_level = 3
    elif exit_completeness >= 2:
        exit_level = 2
    else:
        exit_level = 1

    moat_quality = count_meaningful_values([
        moat.get("moat_type"),
        moat.get("durability"),
        moat.get("threats"),
        company.get("why_selected_over_peers"),
    ])
    competitor_quality = sum(
        1
        for item in competitors
        if is_meaningful_text(item.get("comparison")) and is_meaningful_text(item.get("relative_strength"))
    )
    if competitor_quality >= 3 and moat_quality >= 3:
        competitive_level = 5
    elif competitor_quality >= 2 and moat_quality >= 2:
        competitive_level = 4
    elif competitor_quality >= 1 and moat_quality >= 1:
        competitive_level = 3
    elif moat_quality >= 1 or len(competitors) >= 1:
        competitive_level = 2
    else:
        competitive_level = 1

    customer_hits = 0
    customer_depth_hits = 0
    commercialization_hits = 0
    for item in evidence:
        if is_negative_evidence(item):
            continue
        blob = " ".join([item.get("event_type", ""), item.get("title", ""), item.get("summary", "")]).lower()
        if any(keyword in blob for keyword in ["客户", "签约", "案例", "验证", "合作", "导入", "中标"]):
            customer_hits += 1
        if any(keyword in blob for keyword in ["复购", "扩单", "战略供应商", "独家", "批量采购", "框架协议"]):
            customer_depth_hits += 1
        if any(keyword in blob for keyword in ["量产", "交付", "订单", "收入", "发布", "商业化", "出货", "产线", "下线"]):
            commercialization_hits += 1

    if customer_hits >= 4 and customer_depth_hits >= 2 and evidence_stats["authoritative_count"] >= 2:
        customer_level = 5
    elif customer_hits >= 3 and customer_depth_hits >= 1:
        customer_level = 4
    elif customer_hits >= 2:
        customer_level = 3
    elif customer_hits >= 1:
        customer_level = 2
    else:
        customer_level = 1

    financial_completeness = count_meaningful_values([
        financial.get("estimated_revenue"),
        financial.get("revenue_trend"),
        financial.get("gross_margin_estimate"),
        financial.get("latest_round"),
    ])
    revenue_trend = (financial.get("revenue_trend") or "").lower()
    trend_bonus = 1 if any(keyword in revenue_trend for keyword in ["增长", "翻倍", "上升", ">50%", ">80%", "yoy"]) else 0
    commercialization_signals = financial_completeness + commercialization_hits + trend_bonus
    if commercialization_signals >= 7 and evidence_stats["traceability_score"] >= 60:
        commercialization_level = 5
    elif commercialization_signals >= 5:
        commercialization_level = 4
    elif commercialization_signals >= 3:
        commercialization_level = 3
    elif commercialization_signals >= 2:
        commercialization_level = 2
    else:
        commercialization_level = 1

    listed_status = company.get("listed_status", "")
    if "未上市" not in listed_status:
        investability_level = 0
    else:
        stage_level = infer_stage_fit_level(company.get("stage", ""))
        investability_signals = count_meaningful_values([
            financial.get("latest_round"),
            financial.get("latest_valuation"),
            financial.get("funding_total"),
        ])
        if stage_level >= 4 and investability_signals >= 3 and evidence_stats["traceability_score"] >= 55:
            investability_level = 5
        elif stage_level >= 4 and investability_signals >= 2:
            investability_level = 4
        elif stage_level >= 3 and investability_signals >= 1:
            investability_level = 3
        elif stage_level >= 2:
            investability_level = 2
        else:
            investability_level = 1

    return {
        "information_sufficiency": info_level,
        "exit_feasibility": exit_level,
        "competitive_position": competitive_level,
        "customer_validation": customer_level,
        "commercialization_progress": commercialization_level,
        "investability": investability_level,
    }


def compute_risk_pressure(company: dict[str, Any], penalty: float) -> dict[str, float]:
    risks = company.get("risk_matrix") or []
    weighted_risk = 0
    for item in risks:
        severity = RISK_LEVEL_SCORE.get(item.get("severity"), 2)
        probability = RISK_LEVEL_SCORE.get(item.get("probability"), 2)
        weighted_risk += severity * probability
    risk_pressure = round(min(100.0, weighted_risk * 5 + penalty * 2.5), 1)
    return {"weighted_risk": weighted_risk, "risk_pressure": risk_pressure}


def pick_dimension_highlights(dimension_details: dict[str, dict[str, Any]], dimension_scores: dict[str, float]) -> dict[str, list[dict[str, Any]]]:
    rows = []
    for key, detail in dimension_details.items():
        rows.append({
            "key": key,
            "label": DIMENSION_LABELS[key],
            "level": detail["raw_level"],
            "adjusted_score": dimension_scores.get(key, 0),
        })
    strong = sorted(rows, key=lambda item: (-item["level"], -item["adjusted_score"], item["label"]))[:3]
    weak = sorted(rows, key=lambda item: (item["level"], item["adjusted_score"], item["label"]))[:3]
    return {"strengths": strong, "watchouts": weak}


def extract_financing_facts(text: str) -> dict[str, str]:
    blob = text or ""
    lower_blob = blob.lower()
    round_match = re.search(r"(pre-ipo|pre ipo|pre-a\+?|seed|angel|天使轮|pre-a轮|a\+轮|a轮|b\+轮|b轮|c\+轮|c轮|d轮)", lower_blob, re.I)
    amount_match = re.search(r"((?:累计|近|约|超|逾)?\s*(?:人民币|美元)?\s*\d+(?:\.\d+)?\s*(?:亿元|亿人民币|亿美元|亿美金|万元|万人民币|万美元))", blob, re.I)
    valuation_match = re.search(r"(?:投后估值|估值|valuation)[^，。；;]{0,24}?((?:人民币|美元)?\s*\d+(?:\.\d+)?\s*(?:亿元|亿人民币|亿美元|亿美金))", blob, re.I)
    return {
        "latest_round": round_match.group(1) if round_match else "",
        "funding_total": amount_match.group(1) if amount_match else "",
        "latest_valuation": valuation_match.group(1) if valuation_match else "",
    }


def extract_growth_text(text: str) -> str:
    matches = re.findall(r"(?:同比|yoy|增长|提升)[^。；;\n]{0,20}?(?:\d+(?:\.\d+)?%|翻倍|大幅增长)", text or "", re.I)
    return "；".join(matches[:2])


def extract_gross_margin_text(text: str) -> str:
    match = re.search(r"(毛利率[^。；;\n]{0,20}?(?:\d+(?:\.\d+)?%))", text or "", re.I)
    return match.group(1) if match else ""


def parse_percentage(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def hit_count(evidence: list[dict[str, Any]], keywords: list[str], recent_days: int | None = None, positive_only: bool = False) -> int:
    total = 0
    for item in evidence:
        if positive_only and is_negative_evidence(item):
            continue
        if recent_days is not None:
            age = days_since(evidence_fact_date(item))
            if age is None or age > recent_days:
                continue
        blob = item.get("_blob", "")
        if any(keyword in blob for keyword in keywords):
            total += 1
    return total


def any_hit(text: str, keywords: list[str]) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in keywords)


def level_from_thresholds(value: float, thresholds: tuple[float, float, float, float]) -> int:
    if value >= thresholds[3]:
        return 5
    if value >= thresholds[2]:
        return 4
    if value >= thresholds[1]:
        return 3
    if value >= thresholds[0]:
        return 2
    return 1


def build_signal_profile(entity: str, evidence: list[dict[str, Any]], thesis: str) -> dict[str, Any]:
    combined_text = " ".join([
        entity,
        *[
            " ".join([
                item.get("title", ""),
                item.get("summary", ""),
                " ".join(item.get("tags", [])),
                item.get("event_type", ""),
                item.get("platform", ""),
            ])
            for item in evidence
        ],
    ]).lower()
    claim_counter = Counter(item.get("claim_type", "") for item in evidence if item.get("claim_type"))
    positive_importance = sum(max(1, safe_int(item.get("importance"), 2)) for item in evidence if (item.get("stance") or "").lower() != "negative")
    negative_items = [item for item in evidence if is_negative_evidence(item)]
    customer_keywords = ["客户", "签约", "中标", "合作", "案例", "导入", "项目落地", "验证"]
    depth_keywords = ["复购", "扩单", "战略供应商", "独家", "批量采购", "框架协议", "续费", "续约"]
    commercial_keywords = ["量产", "交付", "订单", "收入", "商业化", "出货", "产线", "下线", "发布", "装机"]
    policy_keywords = ["政策", "补贴", "标准", "指南", "规划", "条例", "试点", "专项"]
    financing_keywords = ["融资", "投资方", "领投", "跟投", "估值", "投后", "轮"]
    product_keywords = ["产品", "发布", "量产", "升级", "迭代", "认证", "获批", "装机", "芯片流片"]
    risk_keywords = ["风险", "诉讼", "处罚", "违规", "召回", "造假", "负面", "下滑", "亏损", "价格战", "压价"]
    exit_keywords = ["ipo", "上市", "并购", "收购", "退出", "并表", "整合"]
    moat_keywords = ["专利", "壁垒", "护城河", "know-how", "knowhow", "工艺", "认证", "资质", "国产替代", "自主可控"]
    competitor_keywords = ["竞品", "竞争", "对标", "替代", "份额", "龙头", "领先", "第一", "前五"]
    platform_keywords = ["平台", "生态", "底座", "系统", "套件", "矩阵"]
    customer_hits = hit_count(evidence, customer_keywords, positive_only=True)
    customer_depth_hits = hit_count(evidence, depth_keywords, positive_only=True)
    commercialization_hits = hit_count(evidence, commercial_keywords, positive_only=True)
    policy_hits = hit_count(evidence, policy_keywords, positive_only=True)
    financing_hits = hit_count(evidence, financing_keywords, recent_days=365, positive_only=True)
    product_hits = hit_count(evidence, product_keywords, positive_only=True)
    risk_hits = hit_count(evidence, risk_keywords)
    exit_hits = hit_count(evidence, exit_keywords, positive_only=True)
    moat_hits = hit_count(evidence, moat_keywords, positive_only=True)
    competitor_hits = hit_count(evidence, competitor_keywords, positive_only=True)
    platform_hits = hit_count(evidence, platform_keywords, positive_only=True)
    cross_industry_hits = hit_count(evidence, ["汽车", "锂电", "3c", "半导体", "医疗", "仓储", "物流", "光伏", "消费电子"], positive_only=True)
    benchmark_hits = hit_count(evidence, ["头部客户", "标杆客户", "top3", "top5", "龙头客户", "央企", "国企", "世界500强"], positive_only=True)
    direct_customer_entry_hits = hit_count(evidence, ["直销", "终端客户", "自建销售", "客户成功"], positive_only=True)
    channel_dependency_hits = hit_count(evidence, ["经销商", "代理商", "渠道商", "集成商"], positive_only=True)
    project_hits = hit_count(evidence, ["项目制", "定制化", "工程服务", "交钥匙"], positive_only=True)
    assembly_hits = hit_count(evidence, ["集成", "拼装", "方案集成", "外购"], positive_only=True)
    consumable_hits = hit_count(evidence, ["耗材", "试剂", "配件", "一次性"], positive_only=True)
    aftermarket_hits = hit_count(evidence, ["订阅", "维保", "服务费", "耗材", "软件授权", "续费"], positive_only=True)
    gross_margin_text = extract_gross_margin_text(combined_text)
    growth_text = extract_growth_text(combined_text)
    financing_facts = extract_financing_facts(combined_text)
    listed = any_hit(combined_text, ["上市公司", "股票代码", "科创板", "创业板", "港交所", "纳斯达克", "纽交所", "a股"]) and not any_hit(combined_text, ["未上市"])
    return {
        "combined_text": combined_text,
        "claim_counter": claim_counter,
        "positive_importance": positive_importance,
        "negative_items": negative_items,
        "customer_hits": customer_hits,
        "customer_depth_hits": customer_depth_hits,
        "commercialization_hits": commercialization_hits,
        "policy_hits": policy_hits,
        "financing_hits": financing_hits,
        "product_hits": product_hits,
        "risk_hits": risk_hits,
        "exit_hits": exit_hits,
        "moat_hits": moat_hits,
        "competitor_hits": competitor_hits,
        "platform_hits": platform_hits,
        "cross_industry_hits": cross_industry_hits,
        "benchmark_hits": benchmark_hits,
        "direct_customer_entry_hits": direct_customer_entry_hits,
        "channel_dependency_hits": channel_dependency_hits,
        "project_hits": project_hits,
        "assembly_hits": assembly_hits,
        "consumable_hits": consumable_hits,
        "aftermarket_hits": aftermarket_hits,
        "gross_margin_text": gross_margin_text,
        "gross_margin_pct": parse_percentage(gross_margin_text) if gross_margin_text else None,
        "growth_text": growth_text,
        "financing_facts": financing_facts,
        "listed": listed,
    }


def infer_listed_status(profile: dict[str, Any]) -> str:
    if profile["listed"]:
        return "上市公司/二级主体"
    if any_hit(profile["combined_text"], ["未上市", "创业公司", "b轮", "a轮", "pre-ipo", "pre-a"]):
        return "未上市"
    return "未知"


def infer_tracking_stage(profile: dict[str, Any], evidence_stats: dict[str, Any]) -> str:
    if any_hit(profile["combined_text"], ["放弃", "终止合作", "清算", "停业", "重大造假"]):
        return "放弃"
    if evidence_stats["recent_90"] >= 3 and profile["customer_hits"] >= 2 and profile["financing_hits"] >= 1:
        return "深跟"
    if evidence_stats["recent_180"] >= 2 and (profile["customer_hits"] >= 1 or profile["commercialization_hits"] >= 2):
        return "立项"
    if evidence_stats["total"] < 3 or evidence_stats["unique_domains"] < 2:
        return "补证"
    return "观察"


def infer_competitive_landscape(profile: dict[str, Any]) -> list[dict[str, str]]:
    text = profile["combined_text"]
    snippets = re.findall(r"(?:对标|相比|竞争对手为|替代)([^。；;\n]{0,36})", text, re.I)
    competitors = []
    for idx, snippet in enumerate(snippets[:3], start=1):
        clean = snippet.strip(" ：:，,。；;")
        if not clean:
            continue
        competitors.append({
            "name": clean[:40] or f"竞对{idx}",
            "comparison": f"公开材料提及与 {clean[:20]} 的对比或替代关系。",
            "relative_strength": "具备一定差异化，但仍需补充更量化的市场份额和客户替换证据。",
        })
    if not competitors and profile["competitor_hits"] >= 2:
        competitors.append({
            "name": "行业头部竞对",
            "comparison": "公开证据显示存在直接竞品和同赛道头部玩家。",
            "relative_strength": "具备初步差异化，但格局和份额仍需进一步补证。",
        })
    return competitors


def infer_exit_mapping(profile: dict[str, Any], sector_type: str) -> dict[str, str]:
    text = profile["combined_text"]
    comparable_exits = "待核实"
    potential_acquirers = "待核实"
    ipo_timeline = "待核实"
    target_multiple = "待核实"
    exit_path_preference = "待核实"
    if any_hit(text, ["ipo", "上市", "科创板", "创业板", "港股", "纳斯达克"]):
        comparable_exits = "公开证据提及IPO/上市锚点，说明该赛道存在一定二级承接逻辑。"
        ipo_timeline = "若后续客户验证和商业化延续，理论上具备3-5年资本化窗口。"
        exit_path_preference = "IPO优先"
    if any_hit(text, ["并购", "收购", "整合", "并表", "产业方"]):
        potential_acquirers = "公开材料出现产业整合或并购语境，潜在买方可能来自上下游龙头。"
        if exit_path_preference == "待核实":
            exit_path_preference = "并购优先"
    if comparable_exits != "待核实" and potential_acquirers != "待核实":
        target_multiple = "退出倍数需结合收入增速、毛利和稀缺性进一步校准。"
    return {
        "comparable_exits": comparable_exits,
        "potential_acquirers": potential_acquirers,
        "ipo_timeline": ipo_timeline,
        "target_multiple": target_multiple,
        "exit_path_preference": exit_path_preference,
    }


def infer_moat_analysis(profile: dict[str, Any]) -> dict[str, str]:
    text = profile["combined_text"]
    moat_type = "待核实"
    if any_hit(text, ["专利", "研发", "算法", "芯片", "材料配方"]):
        moat_type = "技术/工艺"
    elif any_hit(text, ["认证", "注册证", "资质", "准入"]):
        moat_type = "认证/准入"
    elif any_hit(text, ["续费", "复购", "粘性", "平台", "生态"]):
        moat_type = "客户粘性/平台效应"
    durability = "待核实"
    if moat_type != "待核实":
        durability = "公开证据显示存在一定持续性，但仍需用份额、替换率或认证周期补证护城河持久性。"
    threats = "待核实"
    if profile["competitor_hits"] or profile["risk_hits"]:
        threats = "主要威胁来自同质化竞争、价格压力和大客户验证周期。"
    return {"moat_type": moat_type, "durability": durability, "threats": threats}


def infer_financial_profile(profile: dict[str, Any], sector_type: str) -> dict[str, str]:
    text = profile["combined_text"]
    financing = profile["financing_facts"]
    estimated_revenue = "待核实"
    revenue_match = re.search(r"(营收[^。；;\n]{0,20}(?:\d+(?:\.\d+)?\s*(?:亿|万元|万)))", text, re.I)
    if revenue_match:
        estimated_revenue = revenue_match.group(1)
    revenue_trend = profile["growth_text"] or "待核实"
    gross_margin_estimate = profile["gross_margin_text"] or "待核实"
    return {
        "estimated_revenue": estimated_revenue,
        "revenue_trend": revenue_trend,
        "gross_margin_estimate": gross_margin_estimate,
        "funding_total": financing["funding_total"] or "待核实",
        "latest_round": financing["latest_round"] or "待核实",
        "latest_valuation": financing["latest_valuation"] or "待核实",
        "burn_rate_comment": "公开证据不足，需用融资节奏、交付周期和毛利结构进一步判断现金消耗。",
    }


def infer_info_gaps(profile: dict[str, Any], financial_profile: dict[str, str], exit_mapping: dict[str, str], competitive_landscape: list[dict[str, str]], evidence_stats: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    if not is_meaningful_text(financial_profile.get("estimated_revenue")) or financial_profile.get("estimated_revenue") == "待核实":
        gaps.append("缺少营收规模或收入区间")
    if not is_meaningful_text(financial_profile.get("gross_margin_estimate")) or financial_profile.get("gross_margin_estimate") == "待核实":
        gaps.append("缺少毛利率或价值链利润池证据")
    if not is_meaningful_text(financial_profile.get("latest_valuation")) or financial_profile.get("latest_valuation") == "待核实":
        gaps.append("缺少最新估值信息")
    if profile["customer_hits"] < 2:
        gaps.append("客户验证深度不足")
    if profile["customer_depth_hits"] == 0:
        gaps.append("缺少复购/扩单/战略供应商证据")
    if len(competitive_landscape) < 2:
        gaps.append("竞争对手对比不足")
    if count_meaningful_values([exit_mapping.get("comparable_exits"), exit_mapping.get("potential_acquirers"), exit_mapping.get("exit_path_preference")]) < 2:
        gaps.append("退出锚点不足")
    if evidence_stats["recent_180"] == 0 and evidence_stats["total"] > 0:
        gaps.append("缺少近180天有效证据")
    return gaps[:8]


def infer_risk_matrix(profile: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    for item in evidence:
        if not is_negative_evidence(item):
            continue
        importance = clamp(safe_int(item.get("importance"), 2), 1, 5)
        severity = "高" if importance >= 4 else "中" if importance >= 3 else "低"
        probability = "高" if importance >= 4 else "中"
        desc = item.get("quote") or item.get("source_title") or "公开来源出现风险或矛盾表述。"
        risk_type = "市场/竞争"
        blob = item.get("_blob", "")
        if any(keyword in blob for keyword in ["诉讼", "处罚", "违规", "合规"]):
            risk_type = "合规/诉讼"
        elif any(keyword in blob for keyword in ["亏损", "现金流", "融资", "估值"]):
            risk_type = "财务/融资"
        elif any(keyword in blob for keyword in ["技术", "量产", "交付", "缺陷", "召回"]):
            risk_type = "技术/交付"
        risks.append({
            "risk_type": risk_type,
            "description": desc[:120],
            "probability": probability,
            "severity": severity,
            "mitigation": "需补充原文和后续公开进展，确认该风险是否已被缓释。",
        })
    if not risks and profile["project_hits"] >= 2:
        risks.append({
            "risk_type": "交付/规模化",
            "description": "公开材料更偏项目制或定制化，规模化效率可能承压。",
            "probability": "中",
            "severity": "中",
            "mitigation": "补充标准化产品占比、交付周期和复购情况。",
        })
    return risks[:5]


def infer_exclusion_tags(profile: dict[str, Any], company: dict[str, Any]) -> list[str]:
    text = profile["combined_text"]
    tags: list[str] = []
    if profile["consumable_hits"] >= 2 and not any_hit(text, ["平台", "订阅", "高值耗材", "独家"]):
        tags.append("标准耗材型")
    if any_hit(text, ["区域龙头", "本地市场", "华东地区", "华南地区", "区域市场"]) and not any_hit(text, ["全国", "全球", "海外"]):
        tags.append("区域性强")
    if any_hit(text, ["单一产品", "单品", "核心产品占比"]) or (profile["product_hits"] >= 3 and any_hit(text, ["唯一", "单一"])):
        tags.append("单一产品依赖")
    if any_hit(text, ["功能件", "配套件", "配件", "模组"]) and not any_hit(text, ["核心部件", "底层软件"]):
        tags.append("配套功能件型")
    if profile["project_hits"] >= 2:
        tags.append("工程项目型")
    if profile["assembly_hits"] >= 2:
        tags.append("宽口径拼装型")
    if any_hit(text, ["客户集中度", "前五大客户", "前3大客户", "单一大客户"]):
        tags.append("客户集中度过高")
    if any_hit(text, ["跨界创业", "转型", "团队经验不足", "无行业经验"]):
        tags.append("团队经验不匹配")
    if profile["channel_dependency_hits"] > profile["direct_customer_entry_hits"] + 1:
        tags.append("独立客户入口弱")
    gm = profile["gross_margin_pct"]
    if gm is not None and gm < 15:
        tags.append("利润池过薄")
    elif any_hit(text, ["低毛利", "价格战", "压价严重"]):
        tags.append("利润池过薄")
    exit_mapping = company.get("exit_mapping") or {}
    if count_meaningful_values([exit_mapping.get("comparable_exits"), exit_mapping.get("potential_acquirers"), exit_mapping.get("exit_path_preference")]) < 2:
        tags.append("退出锚缺失")
    valuation_text = (company.get("financial_profile") or {}).get("latest_valuation", "")
    if any_hit(valuation_text.lower(), ["30x", "过高", "高估值"]) or any_hit(text, ["估值倒挂", "估值过高", "高估值"]):
        tags.append("估值严重偏高")
    if any_hit(text, ["诉讼", "处罚", "违规", "召回", "环保处罚", "行政处罚"]):
        tags.append("合规/诉讼风险")
    if (company.get("listed_status") or "").startswith("上市公司"):
        tags.append("上市公司仅作参照")
    if any_hit(text, ["造假", "财务造假", "数据造假", "虚假宣传", "重大失信"]):
        tags.append("重大造假嫌疑")
    ordered = []
    seen = set()
    for tag in tags:
        if tag in EXCLUSION_PENALTY and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ordered


def infer_dimension_sublevels(company: dict[str, Any], profile: dict[str, Any], evidence_stats: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    text = profile["combined_text"]
    gm = profile["gross_margin_pct"]
    financial = company.get("financial_profile") or {}
    stage = company.get("stage", "")
    stage_level = infer_stage_fit_level(stage)

    sector_sub = [
        {"key": "policy_catalyst", "label": "政策催化", "desc": SUB_DIMENSIONS["sector_prosperity"]["sub"][0]["desc"], "weight_pct": 30, "level": level_from_thresholds(profile["policy_hits"], (1, 2, 3, 4))},
        {"key": "demand_expansion", "label": "需求扩张", "desc": SUB_DIMENSIONS["sector_prosperity"]["sub"][1]["desc"], "weight_pct": 30, "level": level_from_thresholds(profile["customer_hits"] + profile["commercialization_hits"], (1, 2, 4, 6))},
        {"key": "penetration_rate", "label": "渗透率空间", "desc": SUB_DIMENSIONS["sector_prosperity"]["sub"][2]["desc"], "weight_pct": 20, "level": 4 if any_hit(text, ["渗透率", "蓝海", "国产替代", "增量空间"]) else 3 if profile["product_hits"] >= 2 else 2},
        {"key": "industry_cycle", "label": "产业景气周期", "desc": SUB_DIMENSIONS["sector_prosperity"]["sub"][3]["desc"], "weight_pct": 20, "level": 4 if evidence_stats["recent_180"] >= 3 and profile["policy_hits"] + profile["financing_hits"] >= 2 else 3 if evidence_stats["recent_180"] >= 1 else 2},
    ]

    gross_margin_level = 1
    if gm is not None:
        gross_margin_level = 5 if gm >= 50 else 4 if gm >= 40 else 3 if gm >= 25 else 2 if gm >= 15 else 1
    elif any_hit((financial.get("gross_margin_estimate") or "").lower(), ["55%-80%", "高毛利"]):
        gross_margin_level = 4
    elif financial.get("gross_margin_estimate") != "待核实":
        gross_margin_level = 3
    profit_sub = [
        {"key": "value_chain_pos", "label": "价值链位置", "desc": SUB_DIMENSIONS["profit_pool"]["sub"][0]["desc"], "weight_pct": 30, "level": 5 if any_hit(text, ["核心部件", "底层软件", "平台底座", "核心材料", "核心算法"]) else 3 if any_hit(text, ["系统", "模组", "方案"]) else 2},
        {"key": "pricing_power", "label": "议价能力", "desc": SUB_DIMENSIONS["profit_pool"]["sub"][1]["desc"], "weight_pct": 25, "level": 4 if any_hit(text, ["议价", "不可替代", "高粘性", "标准制定"]) else 3 if profile["benchmark_hits"] or profile["customer_depth_hits"] else 2},
        {"key": "gross_margin", "label": "毛利厚度", "desc": SUB_DIMENSIONS["profit_pool"]["sub"][2]["desc"], "weight_pct": 25, "level": gross_margin_level},
        {"key": "aftermarket", "label": "后市场潜力", "desc": SUB_DIMENSIONS["profit_pool"]["sub"][3]["desc"], "weight_pct": 20, "level": level_from_thresholds(profile["aftermarket_hits"], (1, 2, 3, 4))},
    ]

    high_end_sub = [
        {"key": "tech_barrier", "label": "技术壁垒", "desc": SUB_DIMENSIONS["high_end_attribute"]["sub"][0]["desc"], "weight_pct": 30, "level": level_from_thresholds(profile["moat_hits"] + hit_count(company["evidence"], ["专利", "发明", "研发", "算法", "芯片", "配方"], positive_only=True), (1, 2, 4, 6))},
        {"key": "certification", "label": "认证门槛", "desc": SUB_DIMENSIONS["high_end_attribute"]["sub"][1]["desc"], "weight_pct": 25, "level": level_from_thresholds(hit_count(company["evidence"], ["认证", "注册证", "获批", "资质", "标准"], positive_only=True), (1, 2, 3, 4))},
        {"key": "process_knowhow", "label": "工艺know-how", "desc": SUB_DIMENSIONS["high_end_attribute"]["sub"][2]["desc"], "weight_pct": 25, "level": 4 if any_hit(text, ["工艺", "量产经验", "调参", "know-how", "knowhow"]) else 3 if profile["commercialization_hits"] >= 2 else 2},
        {"key": "import_sub", "label": "国产替代难度", "desc": SUB_DIMENSIONS["high_end_attribute"]["sub"][3]["desc"], "weight_pct": 20, "level": 5 if any_hit(text, ["国产替代", "自主可控", "卡脖子"]) else 3 if any_hit(text, ["进口替代"]) else 2},
    ]

    customer_sub = [
        {"key": "benchmark_client", "label": "标杆客户", "desc": SUB_DIMENSIONS["customer_validation"]["sub"][0]["desc"], "weight_pct": 30, "level": level_from_thresholds(profile["benchmark_hits"] + profile["customer_hits"], (1, 2, 3, 5))},
        {"key": "adoption_depth", "label": "导入深度", "desc": SUB_DIMENSIONS["customer_validation"]["sub"][1]["desc"], "weight_pct": 25, "level": 5 if profile["customer_depth_hits"] >= 2 else 4 if profile["customer_depth_hits"] >= 1 else 3 if profile["customer_hits"] >= 2 else 2},
        {"key": "repurchase", "label": "复购率", "desc": SUB_DIMENSIONS["customer_validation"]["sub"][2]["desc"], "weight_pct": 25, "level": level_from_thresholds(profile["customer_depth_hits"], (1, 1.5, 2, 3))},
        {"key": "cross_industry", "label": "跨行业复制", "desc": SUB_DIMENSIONS["customer_validation"]["sub"][3]["desc"], "weight_pct": 20, "level": 4 if profile["cross_industry_hits"] >= 3 else 3 if profile["cross_industry_hits"] >= 1 else 2},
    ]

    commercialization_sub = [
        {"key": "mass_production", "label": "量产能力", "desc": SUB_DIMENSIONS["commercialization_progress"]["sub"][0]["desc"], "weight_pct": 30, "level": level_from_thresholds(hit_count(company["evidence"], ["量产", "批量", "规模交付", "出货"], positive_only=True), (1, 2, 3, 4))},
        {"key": "order_conversion", "label": "订单转化", "desc": SUB_DIMENSIONS["commercialization_progress"]["sub"][1]["desc"], "weight_pct": 25, "level": level_from_thresholds(hit_count(company["evidence"], ["订单", "签约", "收入", "回款"], positive_only=True), (1, 2, 3, 4))},
        {"key": "channel_build", "label": "渠道建设", "desc": SUB_DIMENSIONS["commercialization_progress"]["sub"][2]["desc"], "weight_pct": 25, "level": 4 if any_hit(text, ["代理商", "渠道", "生态伙伴", "合作伙伴"]) else 3 if profile["platform_hits"] >= 1 else 2},
        {"key": "scale_efficiency", "label": "规模化效率", "desc": SUB_DIMENSIONS["commercialization_progress"]["sub"][3]["desc"], "weight_pct": 20, "level": 4 if any_hit((financial.get("revenue_trend") or "").lower(), ["翻倍", ">80%", "增长", "yoy"]) else 3 if profile["commercialization_hits"] >= 2 else 2},
    ]

    investability_sub = [
        {"key": "round_match", "label": "轮次匹配", "desc": SUB_DIMENSIONS["investability"]["sub"][0]["desc"], "weight_pct": 30, "level": stage_level},
        {"key": "valuation", "label": "估值合理性", "desc": SUB_DIMENSIONS["investability"]["sub"][1]["desc"], "weight_pct": 30, "level": 4 if is_meaningful_text(financial.get("latest_valuation")) and financial.get("latest_valuation") != "待核实" else 2},
        {"key": "equity_structure", "label": "股权结构", "desc": SUB_DIMENSIONS["investability"]["sub"][2]["desc"], "weight_pct": 20, "level": 4 if any_hit(text, ["股权结构", "创始人持股", "老股东", "优先权", "对赌"]) else 2},
        {"key": "timing_window", "label": "融资窗口", "desc": SUB_DIMENSIONS["investability"]["sub"][3]["desc"], "weight_pct": 20, "level": 5 if profile["financing_hits"] >= 2 and hit_count(company["evidence"], ["融资", "交割", "募集"], recent_days=365, positive_only=True) >= 1 else 3 if profile["financing_hits"] >= 1 else 2},
    ]

    exit_sub = [
        {"key": "ipo_comparable", "label": "IPO可比", "desc": SUB_DIMENSIONS["exit_feasibility"]["sub"][0]["desc"], "weight_pct": 30, "level": 4 if any_hit(text, ["ipo", "上市", "科创板", "创业板"]) else 2},
        {"key": "ma_appetite", "label": "并购承接", "desc": SUB_DIMENSIONS["exit_feasibility"]["sub"][1]["desc"], "weight_pct": 25, "level": 4 if any_hit(text, ["并购", "收购", "产业整合"]) else 2},
        {"key": "exit_timeline", "label": "退出时间窗口", "desc": SUB_DIMENSIONS["exit_feasibility"]["sub"][2]["desc"], "weight_pct": 25, "level": 4 if any_hit(text, ["3-5年", "三到五年", "上市计划"]) else 2},
        {"key": "buyer_density", "label": "产业买家密度", "desc": SUB_DIMENSIONS["exit_feasibility"]["sub"][3]["desc"], "weight_pct": 20, "level": 4 if any_hit(text, ["产业方", "龙头", "上下游", "战略买家"]) else 2},
    ]

    competitive_sub = [
        {"key": "differentiation", "label": "差异化", "desc": SUB_DIMENSIONS["competitive_position"]["sub"][0]["desc"], "weight_pct": 30, "level": level_from_thresholds(profile["competitor_hits"] + profile["moat_hits"], (1, 2, 3, 5))},
        {"key": "market_position", "label": "行业地位", "desc": SUB_DIMENSIONS["competitive_position"]["sub"][1]["desc"], "weight_pct": 25, "level": 5 if any_hit(text, ["第一", "前3", "龙头", "top3"]) else 4 if any_hit(text, ["领先", "前五", "头部"]) else 3 if profile["competitor_hits"] else 2},
        {"key": "customer_mindshare", "label": "客户心智", "desc": SUB_DIMENSIONS["competitive_position"]["sub"][2]["desc"], "weight_pct": 25, "level": 4 if profile["benchmark_hits"] >= 1 or profile["customer_depth_hits"] >= 1 else 3 if profile["customer_hits"] >= 2 else 2},
        {"key": "platform_extension", "label": "平台化延展", "desc": SUB_DIMENSIONS["competitive_position"]["sub"][3]["desc"], "weight_pct": 20, "level": 4 if profile["platform_hits"] >= 2 else 3 if profile["platform_hits"] >= 1 else 2},
    ]

    info_sub = [
        {"key": "public_evidence", "label": "公开证据", "desc": SUB_DIMENSIONS["information_sufficiency"]["sub"][0]["desc"], "weight_pct": 35, "level": level_from_thresholds(evidence_stats["total"] + evidence_stats["recent_180"], (2, 4, 6, 8))},
        {"key": "cross_validation", "label": "交叉验证", "desc": SUB_DIMENSIONS["information_sufficiency"]["sub"][1]["desc"], "weight_pct": 35, "level": level_from_thresholds(evidence_stats["unique_domains"] + evidence_stats["authoritative_count"], (2, 3, 5, 7))},
        {"key": "info_consistency", "label": "信息一致性", "desc": SUB_DIMENSIONS["information_sufficiency"]["sub"][2]["desc"], "weight_pct": 30, "level": 5 if len(profile["negative_items"]) == 0 and evidence_stats["traceability_score"] >= 70 else 4 if evidence_stats["traceability_score"] >= 55 else 3 if evidence_stats["traceability_score"] >= 35 else 2},
    ]

    return {
        "sector_prosperity": sector_sub,
        "profit_pool": profit_sub,
        "high_end_attribute": high_end_sub,
        "customer_validation": customer_sub,
        "commercialization_progress": commercialization_sub,
        "investability": investability_sub,
        "exit_feasibility": exit_sub,
        "competitive_position": competitive_sub,
        "information_sufficiency": info_sub,
    }


def infer_confidence_levels(
    company: dict[str, Any],
    profile: dict[str, Any],
    evidence_stats: dict[str, Any],
    sub_levels: dict[str, list[dict[str, Any]]],
    dimension_support: dict[str, dict[str, Any]],
) -> dict[str, str]:
    confidence_levels: dict[str, str] = {}
    for key in sub_levels:
        support_meta = dimension_support.get(key, {})
        count = safe_int(support_meta.get("count"), 0)
        domains = safe_int(support_meta.get("unique_domains"), 0)
        if count <= 0:
            confidence_levels[key] = "low"
            continue

        # Confidence measures support for this dimension, not how attractive
        # the inferred level is.  Global evidence quality is deliberately a
        # small modifier so irrelevant evidence cannot saturate every dimension.
        local_score = (
            min(count, 4) * 8
            + min(domains, 4) * 9
            + min(safe_int(support_meta.get("authoritative_count"), 0), 3) * 7
            + min(safe_int(support_meta.get("verified_count"), 0), 3) * 6
            + min(safe_int(support_meta.get("recent_180"), 0), 3) * 4
            + min(safe_int(support_meta.get("corroborated_claim_count"), 0), 2) * 8
        )
        score = min(100.0, local_score * 0.80 + evidence_stats["reliability_score"] * 0.20)
        if count == 1 or domains == 1:
            score = min(score, 49.0)
        if safe_int(support_meta.get("authoritative_count"), 0) == 0:
            score = min(score, 64.0)
        if support_meta.get("social_only"):
            score = min(score, 34.0)
        confidence_levels[key] = confidence_label_from_score(score)
    return confidence_levels


def build_company_profile(entity: str, evidence: list[dict[str, Any]], thesis: str) -> dict[str, Any]:
    company: dict[str, Any] = {"name": entity, "thesis": thesis, "evidence": evidence}
    profile = build_signal_profile(entity, evidence, thesis)
    company["sector"] = ""
    company["core_product"] = "；".join(item.get("title", "") for item in evidence[:3] if item.get("title"))
    company["company_positioning"] = ""
    company["value_node"] = ""
    company["stage"] = profile["financing_facts"]["latest_round"] or "待核实"
    company["listed_status"] = infer_listed_status(profile)
    sector_type = detect_sector_type(company)
    company["financial_profile"] = infer_financial_profile(profile, sector_type)
    company["competitive_landscape"] = infer_competitive_landscape(profile)
    company["exit_mapping"] = infer_exit_mapping(profile, sector_type)
    company["moat_analysis"] = infer_moat_analysis(profile)
    evidence_stats = collect_evidence_stats(company)
    company["tracking_stage"] = infer_tracking_stage(profile, evidence_stats)
    company["risk_matrix"] = infer_risk_matrix(profile, evidence)
    company["why_selected_over_peers"] = "公开证据显示其在客户验证、商业化或技术壁垒上具备一定先行信号。"
    company["info_gaps"] = infer_info_gaps(profile, company["financial_profile"], company["exit_mapping"], company["competitive_landscape"], evidence_stats)
    company["exclusion_tags"] = infer_exclusion_tags(profile, company)
    sub_levels = infer_dimension_sublevels(company, profile, evidence_stats)
    dimension_support = build_dimension_support_map(evidence)
    company["score_levels"] = {key: weighted_level(items) for key, items in sub_levels.items()}
    company["confidence_levels"] = infer_confidence_levels(company, profile, evidence_stats, sub_levels, dimension_support)
    company["_sub_levels"] = sub_levels
    company["_dimension_support"] = dimension_support
    company["_profile"] = profile
    company["_evidence_stats"] = evidence_stats
    return company


def compute_company_scores(comp: dict[str, Any]) -> dict[str, Any]:
    levels = comp.get("score_levels") or {}
    confidence_levels = comp.get("confidence_levels") or {}
    sector_type = detect_sector_type(comp)
    weights = get_adjusted_weights(sector_type)
    evidence_stats = comp.get("_evidence_stats") or collect_evidence_stats(comp)
    objective_levels = derive_objective_levels(comp, evidence_stats)
    sub_levels = comp.get("_sub_levels") or {}
    dimension_support = comp.get("_dimension_support") or build_dimension_support_map(comp.get("evidence") or [])

    dimension_scores: dict[str, float] = {}
    dimension_details: dict[str, dict[str, Any]] = {}
    total = 0.0
    total_possible = 0.0

    for key, weight in weights.items():
        model_level = clamp_level(levels.get(key, 0))
        objective_level = objective_levels.get(key)
        level = blend_score_level(key, model_level, objective_level)
        discipline_applied = False
        discipline_reason = ""
        if objective_level is not None and key in EVIDENCE_DISCIPLINE_KEYS and model_level - objective_level >= 2:
            disciplined_level = min(level, objective_level + 1)
            if disciplined_level < level:
                level = disciplined_level
                discipline_applied = True
                discipline_reason = "模型判断显著高于客观证据，按证据纪律封顶到客观档位+1。"
        confidence = confidence_levels.get(key, "medium")
        conf_factor = CONFIDENCE_FACTOR.get(confidence, 0.85)
        raw_weighted = round(weight * level / 5.0, 2)
        adjusted = round(raw_weighted * conf_factor, 2)
        dimension_scores[key] = adjusted
        dimension_details[key] = {
            "label": DIMENSION_LABELS[key],
            "guide": DIMENSION_GUIDE[key],
            "anchors": DIMENSION_SCORING_ANCHORS[key],
            "anchor": DIMENSION_SCORING_ANCHORS[key].get(level, ""),
            "model_level": model_level,
            "objective_level": objective_level,
            "raw_level": level,
            "weight": weight,
            "weight_band": WEIGHT_BAND[key],
            "confidence": confidence,
            "confidence_factor": conf_factor,
            "raw_weighted": raw_weighted,
            "adjusted": adjusted,
            "discipline_applied": discipline_applied,
            "discipline_reason": discipline_reason,
            "sub_dimensions": sub_levels.get(key, []),
            "evidence_support": dimension_support.get(key, {}),
        }
        total += adjusted
        total_possible += weight

    penalty = 0
    penalty_details = []
    for tag in comp.get("exclusion_tags") or []:
        entry = EXCLUSION_PENALTY.get(tag)
        if not entry:
            continue
        penalty += entry["penalty"]
        penalty_details.append({
            "tag": tag,
            "penalty": entry["penalty"],
            "severity": entry["severity"],
            "desc": entry["desc"],
        })
    penalty = min(25, penalty)
    total = round(max(0.0, total - penalty), 1)

    static_keys = ["sector_prosperity", "profit_pool", "high_end_attribute", "competitive_position"]
    dynamic_keys = ["customer_validation", "commercialization_progress", "information_sufficiency"]
    deal_keys = ["investability", "exit_feasibility"]
    static_score = round(sum(dimension_scores.get(key, 0) for key in static_keys), 1)
    dynamic_score = round(sum(dimension_scores.get(key, 0) for key in dynamic_keys), 1)
    deal_score = round(sum(dimension_scores.get(key, 0) for key in deal_keys), 1)

    deal_momentum = 0.0
    momentum_signals: list[str] = []
    for item in comp.get("evidence") or []:
        if is_negative_evidence(item):
            continue
        dt = evidence_fact_date(item)
        age = days_since(dt)
        if age is None or age > 90:
            continue
        blob = " ".join([item.get("event_type", ""), item.get("title", ""), item.get("summary", "")]).lower()
        if any(keyword in blob for keyword in ["融资", "战略投资", "估值"]):
            deal_momentum += 1.0
            momentum_signals.append("融资事件")
        elif any(keyword in blob for keyword in ["客户", "签约", "中标", "订单"]):
            deal_momentum += 0.8
            momentum_signals.append("客户签约")
        elif any(keyword in blob for keyword in ["产品", "发布", "量产", "下线"]):
            deal_momentum += 0.6
            momentum_signals.append("产品进展")
        elif any(keyword in blob for keyword in ["专利", "获批", "认证"]):
            deal_momentum += 0.4
            momentum_signals.append("资质认证")
    deal_momentum_bonus = round(min(3.0, deal_momentum), 1)
    total = round(total + deal_momentum_bonus, 1)

    risks = comp.get("risk_matrix") or []
    high_risks = sum(1 for item in risks if item.get("severity") == "高")
    medium_risks = sum(1 for item in risks if item.get("severity") == "中")
    low_risks = sum(1 for item in risks if item.get("severity") == "低")
    confidence_index = compute_confidence_index(confidence_levels)
    risk_stats = compute_risk_pressure(comp, penalty)
    risk_penalty = round(min(10.0, max(0.0, high_risks * 2.5 + medium_risks * 1.0 + low_risks * 0.3)), 1)
    total = round(max(0.0, total - risk_penalty), 1)
    reliability_floor = min(evidence_stats["reliability_score"], evidence_stats["evidence_chain_score"])
    reliability_penalty = round(min(8.0, max(0.0, (62.0 - reliability_floor) / 6.5)), 1)
    total = round(max(0.0, total - reliability_penalty), 1)

    execution_readiness = round(
        min(
            100.0,
            TRACKING_SIGNAL.get(comp.get("tracking_stage"), 40) * 0.30
            + (dimension_details["investability"]["raw_level"] / 5.0) * 25
            + (dimension_details["customer_validation"]["raw_level"] / 5.0) * 20
            + (dimension_details["commercialization_progress"]["raw_level"] / 5.0) * 15
            + min(deal_momentum_bonus / 3.0, 1.0) * 10,
        ),
        1,
    )
    data_quality = round(
        min(
            100.0,
            confidence_index * 0.24
            + evidence_stats["evidence_strength"] * 0.18
            + evidence_stats["reliability_score"] * 0.34
            + max(0, 100 - len(comp.get("info_gaps") or []) * 10) * 0.15
            + min(evidence_stats["recent_90"] * 15, 100) * 0.09,
        ),
        1,
    )
    highlights = pick_dimension_highlights(dimension_details, dimension_scores)

    score_cap = 100.0
    gate_details = []
    if "未上市" not in (comp.get("listed_status") or ""):
        score_cap = min(score_cap, 35.0)
        gate_details.append({"name": "一级属性门槛", "cap": 35.0, "reason": "上市公司或非一级主体，不进入一级推荐主体。"})
    if evidence_stats["total"] < 3 or evidence_stats["unique_domains"] < 2:
        score_cap = min(score_cap, 57.0)
        gate_details.append({"name": "证据充分度门槛", "cap": 57.0, "reason": "公开证据条数或来源域名过少，只能进入补证池。"})
    if evidence_stats["official_anchor_count"] == 0 and evidence_stats["authoritative_count"] < 2 and evidence_stats["total"] > 0:
        score_cap = min(score_cap, 63.0)
        gate_details.append({"name": "官方锚门槛", "cap": 63.0, "reason": "缺少官网、监管、数据库等硬锚点，暂不支持高分结论。"})
    if evidence_stats["social_dependency_ratio"] >= 0.60 and evidence_stats["official_anchor_count"] == 0:
        score_cap = min(score_cap, 58.0)
        gate_details.append({"name": "社交依赖门槛", "cap": 58.0, "reason": "证据过度依赖社交平台且未补到硬证据，只能先当线索池。"})
    if evidence_stats["evidence_chain_score"] < 45 and evidence_stats["total"] > 0:
        score_cap = min(score_cap, 61.0)
        gate_details.append({"name": "证据链门槛", "cap": 61.0, "reason": "跨来源、跨引擎或跨域名支撑不足，证据链完整度不够。"})
    if len(comp.get("info_gaps") or []) >= 5:
        score_cap = min(score_cap, 60.0)
        gate_details.append({"name": "信息缺口门槛", "cap": 60.0, "reason": "关键待补证据过多，优先级自动下修。"})
    financial = comp.get("financial_profile") or {}
    financial_completeness = count_meaningful_values([
        financial.get("estimated_revenue"),
        financial.get("revenue_trend"),
        financial.get("gross_margin_estimate"),
    ])
    if financial_completeness == 0 or all((financial.get(key) or "") == "待核实" for key in ["estimated_revenue", "revenue_trend", "gross_margin_estimate"]):
        score_cap = min(score_cap, 62.0)
        gate_details.append({"name": "财务数据门槛", "cap": 62.0, "reason": "营收、增长趋势、毛利估算全部缺失，无法支撑高分商业化判断。"})
    exit_mapping = comp.get("exit_mapping") or {}
    if count_meaningful_values([exit_mapping.get("comparable_exits"), exit_mapping.get("potential_acquirers"), exit_mapping.get("exit_path_preference")]) < 2:
        score_cap = min(score_cap, 65.0)
        gate_details.append({"name": "退出锚门槛", "cap": 65.0, "reason": "退出可比与承接方信息不足，高分不成立。"})
    if count_meaningful_values(comp.get("competitive_landscape") or []) < 2:
        score_cap = min(score_cap, 68.0)
        gate_details.append({"name": "竞争格局门槛", "cap": 68.0, "reason": "竞争对手对比不足，无法支撑高分卡位判断。"})
    if evidence_stats["recent_180"] == 0 and evidence_stats["total"] > 0:
        score_cap = min(score_cap, 70.0)
        gate_details.append({"name": "时效性门槛", "cap": 70.0, "reason": "无近180天证据，信息可能已过时。"})
    if (comp.get("tracking_stage") or "") == "放弃":
        score_cap = min(score_cap, 30.0)
        gate_details.append({"name": "放弃状态门槛", "cap": 30.0, "reason": "当前跟踪阶段已标记放弃。"})

    pre_gate_score = total
    total = round(min(total, score_cap), 1)

    return {
        "dimension_scores": dimension_scores,
        "dimension_details": dimension_details,
        "penalty": penalty,
        "risk_penalty": risk_penalty,
        "reliability_penalty": reliability_penalty,
        "penalty_details": penalty_details,
        "total_score": total,
        "pre_gate_score": pre_gate_score,
        "score_cap": score_cap,
        "gate_details": gate_details,
        "total_possible": round(total_possible, 1),
        "score_pct": round(total / total_possible * 100, 1) if total_possible > 0 else 0,
        "score_bucket": score_bucket(total),
        "score_grade": score_grade(total),
        "sector_type": sector_type,
        "adjusted_weights": weights,
        "static_score": static_score,
        "dynamic_score": dynamic_score,
        "deal_score": deal_score,
        "risk_summary": {"high": high_risks, "medium": medium_risks, "low": low_risks, "total": len(risks)},
        "deal_momentum": {"bonus": deal_momentum_bonus, "signals": momentum_signals[:6]},
        "diagnostics": {
            "confidence_index": confidence_index,
            "evidence_strength": evidence_stats["evidence_strength"],
            "freshness_score": evidence_stats["freshness_score"],
            "traceability_score": evidence_stats["traceability_score"],
            "source_authority_score": evidence_stats["authority_score"],
            "cross_validation_score": evidence_stats["cross_validation_score"],
            "evidence_chain_score": evidence_stats["evidence_chain_score"],
            "reliability_score": evidence_stats["reliability_score"],
            "social_dependency_ratio": round(evidence_stats["social_dependency_ratio"] * 100, 1),
            "risk_pressure": risk_stats["risk_pressure"],
            "execution_readiness": execution_readiness,
            "data_quality": data_quality,
            "deal_momentum_bonus": deal_momentum_bonus,
            "reliability_penalty": reliability_penalty,
        },
        "signal_stats": {
            "evidence_total": evidence_stats["total"],
            "dated_evidence": evidence_stats["dated"],
            "recent_evidence_90d": evidence_stats["recent_90"],
            "recent_evidence_180d": evidence_stats["recent_180"],
            "source_domain_count": evidence_stats["unique_domains"],
            "traceable_evidence_count": evidence_stats["traceable_count"],
            "verified_evidence_count": evidence_stats["verified_count"],
            "t1_evidence_count": evidence_stats["t1_count"],
            "t2_evidence_count": evidence_stats["t2_count"],
            "authoritative_evidence_count": evidence_stats["authoritative_count"],
            "official_anchor_count": evidence_stats["official_anchor_count"],
            "cross_provider_evidence_count": evidence_stats["independently_corroborated_count"],
            "multi_provider_retrieval_count": evidence_stats["multi_provider_retrieval_count"],
            "independently_corroborated_count": evidence_stats["independently_corroborated_count"],
            "corroborated_claim_count": evidence_stats["corroborated_claim_count"],
            "weighted_risk_score": risk_stats["weighted_risk"],
            "info_gap_count": len(comp.get("info_gaps") or []),
        },
        "highlights": highlights,
    }


def keyword_overlap_score(thesis: str, evidence: list[dict[str, Any]]) -> float:
    thesis_words = {word.strip().lower() for word in thesis.replace("，", " ").replace(",", " ").split() if word.strip()}
    if not thesis_words:
        return 0.0
    text = " ".join((item.get("entity", "") + " " + item.get("quote", "") + " " + " ".join(item.get("tags", []))).lower() for item in evidence)
    hits = sum(1 for word in thesis_words if word in text)
    return min(10.0, hits * 2.0)


def summarize_candidate(entity: str, scoring: dict[str, Any], evidence_stats: dict[str, Any]) -> str:
    strengths = "、".join(item["label"] for item in scoring["highlights"]["strengths"][:2]) or "证据密度"
    watchouts = "、".join(item["label"] for item in scoring["highlights"]["watchouts"][:2]) or "信息补证"
    return f"{entity}：{scoring['score_bucket']}，{evidence_stats['total']}条证据/{evidence_stats['unique_domains']}个域名，近180天{evidence_stats['recent_180']}条，官方/数据库锚点{evidence_stats['official_anchor_count']}条；优势在{strengths}，短板在{watchouts}。"


def build_breakdown(scoring: dict[str, Any], thesis: str, evidence: list[dict[str, Any]]) -> dict[str, float]:
    dimension_scores = scoring["dimension_scores"]
    demand = round(dimension_scores.get("sector_prosperity", 0) + dimension_scores.get("profit_pool", 0), 1)
    traction = round(dimension_scores.get("customer_validation", 0) + dimension_scores.get("commercialization_progress", 0), 1)
    team = round(dimension_scores.get("high_end_attribute", 0) + dimension_scores.get("competitive_position", 0), 1)
    timing = round(dimension_scores.get("investability", 0) + dimension_scores.get("exit_feasibility", 0), 1)
    proof = round(dimension_scores.get("information_sufficiency", 0), 1)
    fit = round(keyword_overlap_score(thesis, evidence), 1)
    risk_penalty = round(scoring["risk_penalty"] + scoring["penalty"] + scoring["reliability_penalty"], 1)
    return {
        "demand": demand,
        "traction": traction,
        "team": team,
        "timing": timing,
        "proof": proof,
        "fit": fit,
        "risk_penalty": risk_penalty,
    }


def priority_weight_from_confidence(confidence_score: float) -> float:
    return round(0.55 + 0.45 * confidence_score / 100.0, 4)


def score_candidate(entity: str, evidence: list[dict[str, Any]], thesis: str) -> dict[str, Any]:
    normalized_evidence = [normalize_evidence_item(item) for item in evidence]
    normalized_evidence.sort(
        key=lambda item: (
            safe_int(item.get("importance"), 0),
            -(days_since(evidence_fact_date(item)) if days_since(evidence_fact_date(item)) is not None else 99999),
        ),
        reverse=True,
    )
    company = build_company_profile(entity, normalized_evidence, thesis)
    scoring = compute_company_scores(company)
    evidence_stats = company["_evidence_stats"]
    confidence_score = scoring["diagnostics"]["confidence_index"]
    priority_score = round(
        scoring["total_score"] * priority_weight_from_confidence(confidence_score),
        1,
    )
    summary = summarize_candidate(entity, scoring, evidence_stats)
    return {
        "entity": entity,
        "summary": summary,
        "total_score": scoring["total_score"],
        "score_bucket": scoring["score_bucket"],
        "opportunity_score": scoring["pre_gate_score"],
        "confidence_score": confidence_score,
        "priority_score": priority_score,
        "dimension_scores": scoring["dimension_scores"],
        "dimension_details": scoring["dimension_details"],
        "breakdown": build_breakdown(scoring, thesis, normalized_evidence),
        "exclusion_penalties": scoring["penalty_details"],
        "gate_details": scoring["gate_details"],
        "deal_momentum": scoring["deal_momentum"],
        "risk_summary": scoring["risk_summary"],
        "diagnostics": {
            **scoring["diagnostics"],
            "sector_type": scoring["sector_type"],
            "adjusted_weights": scoring["adjusted_weights"],
            "static_score": scoring["static_score"],
            "dynamic_score": scoring["dynamic_score"],
            "deal_score": scoring["deal_score"],
            "score_cap": scoring["score_cap"],
            "score_grade": scoring["score_grade"],
            "score_pct": scoring["score_pct"],
            "signal_stats": scoring["signal_stats"],
            "tracking_stage": company.get("tracking_stage"),
            "listed_status": company.get("listed_status"),
            "info_gaps": company.get("info_gaps"),
            "financial_profile": company.get("financial_profile"),
            "exit_mapping": company.get("exit_mapping"),
            "competitive_landscape": company.get("competitive_landscape"),
            "moat_analysis": company.get("moat_analysis"),
            "scoring_template_version": SCORING_TEMPLATE["version"],
        },
        "evidence_count": len(normalized_evidence),
        "evidence": normalized_evidence,
    }


def build_report(thesis: str, discovery_links: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: DefaultDict[str, list[dict[str, Any]]] = defaultdict(list)
    entity_aliases: DefaultDict[str, set[str]] = defaultdict(set)
    ignored_evidence_count = 0
    for item in evidence:
        if item.get("evidence_eligible") is False or (
            bool(item.get("discovery_only")) and item.get("quote_verified") is not True
        ):
            ignored_evidence_count += 1
            continue
        entity = (item.get("entity") or "").strip()
        if not entity:
            ignored_evidence_count += 1
            continue
        entity_key = canonical_entity_name(entity)
        if not entity_key:
            ignored_evidence_count += 1
            continue
        grouped[entity_key].append(item)
        entity_aliases[entity_key].add(entity)

    candidates = []
    for entity_key, items in grouped.items():
        aliases = sorted(entity_aliases[entity_key], key=lambda value: (len(value), value), reverse=True)
        display_entity = aliases[0]
        candidate = score_candidate(display_entity, items, thesis)
        candidate["entity_aliases"] = aliases
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item["priority_score"], item["confidence_score"], item["evidence_count"]), reverse=True)

    scoring_summary = {
        "recommended": sum(1 for c in candidates if c.get("score_bucket") == "最终推荐"),
        "tracking": sum(1 for c in candidates if c.get("score_bucket") == "重点跟踪"),
        "watchlist": sum(1 for c in candidates if c.get("score_bucket") == "备选观察"),
        "pending": sum(1 for c in candidates if c.get("score_bucket") == "待补证"),
        "low_priority": sum(1 for c in candidates if c.get("score_bucket") == "低优先级"),
    }

    all_domains = sorted({
        urlparse(e.get("source_url", "")).netloc
        for items in grouped.values()
        for e in items
        if e.get("source_url")
    } - {""})

    return {
        "generated_at": now_utc_iso(),
        "thesis": thesis,
        "discovery_links": discovery_links,
        "total_evidence": sum(len(items) for items in grouped.values()),
        "ignored_unverified_or_unassigned_evidence": ignored_evidence_count,
        "total_candidates": len(candidates),
        "total_domains": len(all_domains),
        "scoring_version": SCORING_TEMPLATE["version"],
        "scoring_summary": scoring_summary,
        "scoring_template": SCORING_TEMPLATE,
        "candidates": candidates,
    }
