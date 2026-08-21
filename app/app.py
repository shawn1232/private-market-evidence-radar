from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse
import json
import os
import shutil
import subprocess
import sys
import threading

from dateutil import parser as dtparser
from flask import Flask, abort, redirect, render_template_string, request, url_for

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "output"
URLS_PATH = ROOT / "data" / "input" / "urls.txt"
REPORT_PATH = OUT_DIR / "latest_report.json"
LATEST_ATTEMPT_PATH = OUT_DIR / "latest_pipeline_attempt.json"
DISCOVERY_LINKS_PATH = OUT_DIR / "discovery_links.json"
SCORING_CONFIG_PATH = ROOT / "config" / "scoring_config.json"
SOURCE_TEMPLATE_PATH = ROOT / "config" / "source_templates.json"
COLLECTOR_PLAYBOOK_PATH = ROOT / "config" / "collector_playbook.json"
SESSIONS_DIR = ROOT / "sessions"

app = Flask(__name__)
_pipeline_lock = threading.Lock()
_pipeline_state: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "message": "",
    "started_at": "",
    "finished_at": "",
    "thesis": "",
}


@app.before_request
def enforce_local_request() -> None:
    """Keep mutating actions local even if a web page tries to call localhost."""
    remote = (request.remote_addr or "").split("%", 1)[0]
    if remote not in {"127.0.0.1", "::1"}:
        abort(403)
    if request.method == "POST":
        origin = request.headers.get("Origin", "").strip()
        if origin and (urlparse(origin).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            abort(403)

GROUP_ORDER = [
    "微信/公众号",
    "小红书",
    "知识星球",
    "知识星球/知识社区",
    "企业/官方",
    "工商/融资",
    "政策/监管",
    "招聘/招投标",
    "专利/标准",
    "视频/社交",
    "开发者/供应链",
]
SESSION_PLATFORM_META = {
    "xiaohongshu": {"label": "小红书", "hint": "公开笔记、账号页、关键词搜索页"},
    "zsxq": {"label": "知识星球", "hint": "帖子、专栏、文档页"},
    "weixin": {"label": "微信公众号后台", "hint": "公众号原文、菜单、小程序入口"},
}
SKILL_PATH_HINTS = {
    "wechat_article_reader": "20_openclaw-selected/8421bit/wechat-article-reader/SKILL.md",
    "xiaohongshu_analyzer": "20_openclaw-selected/275254cl-hash/xiaohongshu-analyzer/SKILL.md",
    "a_share_site_crawl": "20_openclaw-selected/afengzi/a-share-site-crawl/SKILL.md",
    "ak_rss_24h_brief": "20_openclaw-selected/seandong/ak-rss-24h-brief/SKILL.md",
    "browser_use": "25_terminalskills-shortlist/browser-use/SKILL.md",
    "chrome_open_tabs": "20_openclaw-selected/mindsocket/open-chrome-tabs/SKILL.md",
}
CLAIM_TYPE_LABELS = {
    "demand_signal": "需求信号",
    "commercial_signal": "商业化信号",
    "product_signal": "产品信号",
    "founder_signal": "创始团队信号",
    "hiring_signal": "招聘信号",
    "policy_signal": "政策信号",
    "partnership_signal": "合作信号",
    "risk_signal": "风险信号",
    "contradiction": "矛盾项",
}
SOURCE_TIER_LABELS = {
    "primary_official": "一级官方",
    "platform_official": "平台官方",
    "industry_db": "产业数据库",
    "mainstream_media": "主流媒体",
    "social_post": "社交内容",
    "aggregator": "聚合页",
    "unknown": "未知来源",
    "T1": "T1",
    "T1/T2": "T1/T2",
    "T2": "T2",
    "T2/T3": "T2/T3",
    "T3": "T3",
}
CONFIDENCE_LABELS = {
    "high": "高",
    "medium_high": "中高",
    "medium": "中",
    "medium_low": "中低",
    "low": "低",
}
SOURCE_TIER_SCORES = {
    "primary_official": 100,
    "platform_official": 92,
    "industry_db": 85,
    "mainstream_media": 78,
    "social_post": 52,
    "aggregator": 40,
    "unknown": 35,
    "T1": 100,
    "T1/T2": 90,
    "T2": 78,
    "T2/T3": 62,
    "T3": 45,
}
INTENSITY_OPTIONS = [
    {"value": "standard", "label": "standard"},
    {"value": "deep", "label": "deep"},
    {"value": "epic", "label": "epic"},
]
MAX_MANUAL_URLS = 30

TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DealScope｜一级市场证据工作台</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%232563eb'/%3E%3Ctext x='32' y='41' text-anchor='middle' font-size='22' font-family='Arial' font-weight='700' fill='white'%3EDS%3C/text%3E%3C/svg%3E">
<style>
:root {
  --bg: #eef3f9;
  --header: #1a1f36;
  --card: #ffffff;
  --text: #172033;
  --muted: #64748b;
  --line: #d9e2ef;
  --accent: #2563eb;
  --accent-strong: #1d4ed8;
  --green: #15803d;
  --green-deep: #166534;
  --yellow: #d97706;
  --orange: #ea580c;
  --red: #dc2626;
  --gray: #64748b;
  --shadow: 0 18px 45px rgba(15, 23, 42, 0.10);
  --radius: 20px;
  --radius-sm: 14px;
  --font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: var(--font);
  color: var(--text);
  background:
    radial-gradient(circle at top right, rgba(37,99,235,0.12), transparent 28%),
    linear-gradient(180deg, #f6f9fd 0%, var(--bg) 100%);
}
a { color: inherit; text-decoration: none; }
.page {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 20px 52px;
}
.hero {
  background:
    linear-gradient(140deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02)),
    linear-gradient(135deg, #1a1f36 0%, #212c4e 55%, #1b2b58 100%);
  color: #f8fbff;
  border-radius: 28px;
  padding: 28px;
  box-shadow: var(--shadow);
  overflow: hidden;
  position: relative;
}
.hero::after {
  content: "";
  position: absolute;
  inset: auto -80px -120px auto;
  width: 260px;
  height: 260px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(37,99,235,0.28), transparent 68%);
  pointer-events: none;
}
.hero-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 22px;
}
.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: rgba(255,255,255,.72);
}
.hero h1 {
  margin: 10px 0 10px;
  font-size: clamp(30px, 4vw, 40px);
  line-height: 1.08;
}
.hero p {
  margin: 0;
  color: rgba(255,255,255,.74);
  max-width: 760px;
  line-height: 1.65;
}
.hero-meta {
  min-width: 280px;
  padding: 18px 20px;
  border-radius: 18px;
  background: rgba(255,255,255,.08);
  border: 1px solid rgba(255,255,255,.14);
  backdrop-filter: blur(10px);
}
.hero-meta .meta-label {
  font-size: 12px;
  color: rgba(255,255,255,.62);
  margin-bottom: 6px;
}
.hero-meta .meta-value {
  font-size: 16px;
  font-weight: 700;
  line-height: 1.45;
}
.hero-form {
  background: rgba(255,255,255,.08);
  border: 1px solid rgba(255,255,255,.14);
  border-radius: 22px;
  padding: 18px;
  backdrop-filter: blur(10px);
}
.form-grid {
  display: grid;
  grid-template-columns: minmax(0, 2.4fr) minmax(220px, 1fr) 180px;
  gap: 14px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.field span {
  font-size: 13px;
  color: rgba(255,255,255,.72);
}
.field input,
.field select,
.field textarea {
  width: 100%;
  border: 1px solid rgba(255,255,255,.16);
  background: rgba(255,255,255,.96);
  color: var(--text);
  border-radius: 14px;
  padding: 14px 15px;
  font-size: 15px;
  outline: none;
}
.field textarea {
  min-height: 92px;
  resize: vertical;
  line-height: 1.55;
}
.field-wide { grid-column: 1 / -1; }
.field-note { font-size: 12px; color: rgba(255,255,255,.62); line-height: 1.5; }
.field input::placeholder { color: #94a3b8; }
.field input:focus,
.field select:focus {
  border-color: rgba(37,99,235,.75);
  box-shadow: 0 0 0 3px rgba(37,99,235,.22);
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 16px;
}
.btn,
.btn-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 16px;
  border-radius: 14px;
  border: 1px solid transparent;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  transition: transform .18s ease, background .18s ease, border-color .18s ease;
}
.btn:hover,
.btn-link:hover {
  transform: translateY(-1px);
}
.btn-primary {
  background: var(--accent);
  color: #fff;
}
.btn-primary:hover { background: var(--accent-strong); }
.btn-secondary {
  background: rgba(255,255,255,.12);
  color: #fff;
  border-color: rgba(255,255,255,.18);
}
.btn-secondary:hover { background: rgba(255,255,255,.18); }
.banner {
  margin-top: 18px;
  border-radius: 16px;
  padding: 14px 18px;
  font-size: 14px;
  border: 1px solid #c7d7f7;
  background: #eff6ff;
  color: #1e3a8a;
}
.surface {
  background: var(--card);
  border-radius: 24px;
  box-shadow: var(--shadow);
  margin-top: 18px;
  overflow: hidden;
}
.stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 14px;
  padding: 18px;
}
.stat-card {
  background: linear-gradient(180deg, #ffffff 0%, #f7faff 100%);
  border: 1px solid #e3ebf6;
  border-radius: 18px;
  padding: 16px;
}
.stat-label {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 8px;
}
.stat-value {
  font-size: 24px;
  font-weight: 800;
}
.stat-note {
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
  padding: 20px 22px;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.section-title {
  font-size: 18px;
  font-weight: 800;
}
.section-subtitle {
  font-size: 13px;
  color: var(--muted);
  margin-top: 4px;
}
.toggle-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #eff5ff;
  color: var(--accent);
  font-weight: 900;
}
.collapse {
  overflow: hidden;
  transition: max-height .28s ease, opacity .28s ease;
}
.collapse-inner {
  padding: 0 22px 22px;
}
.link-groups {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
}
.link-group {
  border: 1px solid #e4ebf4;
  background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
  border-radius: 18px;
  padding: 16px;
}
.group-title {
  font-size: 16px;
  font-weight: 800;
  margin: 0 0 12px;
}
.link-list {
  display: grid;
  gap: 10px;
}
.link-item {
  display: block;
  padding: 12px 13px;
  border-radius: 14px;
  background: #f7faff;
  border: 1px solid #e5edf8;
  transition: border-color .18s ease, transform .18s ease;
}
.link-item:hover {
  border-color: rgba(37,99,235,.32);
  transform: translateY(-1px);
}
.link-item strong {
  display: block;
  font-size: 14px;
}
.link-url {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  word-break: break-all;
}
.empty-state {
  border: 1px dashed #d7dfeb;
  background: #f8fbff;
  border-radius: 18px;
  padding: 20px;
  color: var(--muted);
  text-align: center;
}
.candidate {
  padding: 22px;
}
.candidate + .candidate {
  border-top: 1px solid #edf2f7;
}
.candidate-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}
.candidate-title {
  margin: 0;
  font-size: clamp(24px, 2.5vw, 30px);
  line-height: 1.12;
}
.candidate-summary {
  margin-top: 10px;
  color: var(--muted);
  line-height: 1.7;
  max-width: 850px;
}
.score-badge {
  min-width: 170px;
  border-radius: 18px;
  padding: 16px 18px;
  color: #fff;
  text-align: right;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.14);
}
.score-badge .score-number {
  display: block;
  font-size: 34px;
  font-weight: 800;
  line-height: 1;
}
.score-badge .score-label {
  margin-top: 8px;
  font-size: 13px;
  opacity: .88;
}
.badge-green { background: linear-gradient(135deg, #15803d, #166534); }
.badge-blue { background: linear-gradient(135deg, #2563eb, #1d4ed8); }
.badge-orange { background: linear-gradient(135deg, #ea580c, #c2410c); }
.badge-gray { background: linear-gradient(135deg, #64748b, #475569); }
.triple-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-top: 18px;
}
.metric-card {
  background: linear-gradient(180deg, #ffffff 0%, #f7faff 100%);
  border: 1px solid #e3ebf6;
  border-radius: 18px;
  padding: 16px;
}
.metric-card .metric-label {
  color: var(--muted);
  font-size: 13px;
}
.metric-card .metric-value {
  margin-top: 10px;
  font-size: 34px;
  font-weight: 800;
}
.dimension-board {
  margin-top: 18px;
  display: grid;
  gap: 12px;
}
.dimension-row {
  border: 1px solid #e5edf8;
  background: #fbfdff;
  border-radius: 18px;
  padding: 14px 16px;
}
.dimension-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 10px;
}
.dimension-name {
  font-size: 15px;
  font-weight: 700;
}
.dimension-meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.mini-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 999px;
  padding: 4px 10px;
  background: #edf3ff;
  color: #1d4ed8;
  font-size: 12px;
  font-weight: 700;
}
.bar-track {
  width: 100%;
  height: 12px;
  border-radius: 999px;
  background: #e7edf5;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: inherit;
  transition: width .3s ease;
}
.level-5 { background: linear-gradient(90deg, #166534, #15803d); }
.level-4 { background: linear-gradient(90deg, #15803d, #22c55e); }
.level-3 { background: linear-gradient(90deg, #eab308, #f59e0b); }
.level-2 { background: linear-gradient(90deg, #fb923c, #ea580c); }
.level-1 { background: linear-gradient(90deg, #ef4444, #dc2626); }
.level-0 { background: linear-gradient(90deg, #cbd5e1, #94a3b8); }
.dimension-foot {
  margin-top: 10px;
  color: var(--muted);
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.warning {
  color: #b45309;
  font-weight: 700;
}
.detail-grid,
.diagnostic-grid,
.penalty-grid {
  display: grid;
  gap: 14px;
}
.detail-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.penalty-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-top: 18px;
}
.detail-card,
.diagnostic-card,
.panel-card {
  border: 1px solid #e5edf8;
  background: #fbfdff;
  border-radius: 18px;
  padding: 16px;
}
.panel-card h4,
.detail-card h4,
.diagnostic-card h4 {
  margin: 0 0 12px;
  font-size: 15px;
}
.stack {
  display: grid;
  gap: 10px;
}
.stack-item {
  border-radius: 14px;
  background: #f7faff;
  border: 1px solid #e5edf8;
  padding: 11px 12px;
}
.stack-item strong {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
}
.stack-meta,
.muted {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.65;
}
.penalty-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 6px 10px;
  border-radius: 999px;
  background: #fff7ed;
  border: 1px solid #fdba74;
  color: #9a3412;
  font-size: 12px;
  font-weight: 700;
}
.diagnostic-grid {
  grid-template-columns: repeat(7, minmax(0, 1fr));
  margin-top: 18px;
}
.diagnostic-card .diag-value {
  margin-top: 8px;
  font-size: 26px;
  font-weight: 800;
}
.table-wrap {
  overflow-x: auto;
  border: 1px solid #e5edf8;
  border-radius: 18px;
  background: #fbfdff;
}
table {
  width: 100%;
  min-width: 900px;
  border-collapse: collapse;
}
th,
td {
  padding: 13px 14px;
  border-bottom: 1px solid #e7edf5;
  text-align: left;
  vertical-align: top;
  font-size: 13px;
}
th {
  background: #f7faff;
  color: #475569;
  font-size: 12px;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.badge-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.claim-badge,
.platform-badge,
.tier-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 999px;
  padding: 4px 10px;
  font-weight: 700;
  font-size: 12px;
}
.claim-badge { background: #e0ecff; color: #1d4ed8; }
.platform-badge { background: #eff6ff; color: #1e40af; }
.tier-badge { background: #f1f5f9; color: #334155; }
.source-link {
  color: var(--accent);
  word-break: break-all;
}
.sub-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.sub-pill {
  border-radius: 999px;
  padding: 5px 10px;
  background: #eef5ff;
  color: #1d4ed8;
  font-size: 12px;
}
@media (max-width: 1080px) {
  .form-grid,
  .stats,
  .detail-grid,
  .penalty-grid,
  .diagnostic-grid,
  .triple-grid {
    grid-template-columns: 1fr;
  }
  .candidate-head,
  .hero-top {
    flex-direction: column;
  }
  .score-badge {
    width: 100%;
    text-align: left;
  }
}
@media (max-width: 720px) {
  .page { padding: 16px 14px 40px; }
  .hero { padding: 22px; border-radius: 22px; }
  .surface { border-radius: 20px; }
  .candidate { padding: 18px; }
  .section-head { padding: 18px; }
  .collapse-inner { padding: 0 18px 18px; }
}
</style>
</head>
<body data-pipeline-running="{{ 'true' if pipeline_runtime.running else 'false' }}">
<div class="page">
  <section class="hero">
    <div class="hero-top">
      <div>
        <div class="eyebrow">DEALSCOPE · 证据优先 / 可溯源 / 交叉验证</div>
        <h1>一级市场证据工作台</h1>
        <p>这里用于对周度雷达中的线索补充原文、核验证据并做结构化整理。事实、推断与待核实项分开显示，不以搜索摘要代替原文。</p>
        <p>本工具仅整理公开信息，不构成投资建议、募资推介、估值意见或交易依据。</p>
      </div>
      <div class="hero-meta">
        <div class="meta-label">当前报告</div>
        <div class="meta-value">{{ report.title_line }}</div>
        <div class="meta-label" style="margin-top:14px;">最近生成</div>
        <div class="meta-value">{{ report.generated_at or "暂无" }}</div>
      </div>
    </div>
    <form class="hero-form" method="post" action="{{ url_for('generate_links') }}">
      <div class="form-grid">
        <label class="field">
          <span>研究主题</span>
          <input type="text" name="q" value="{{ q }}" placeholder="例如：工业软件 AI质检 出海 / 智能制造 SaaS / 人形机器人 工业场景">
        </label>
        <label class="field">
          <span>公司（可选）</span>
          <input type="text" name="company" value="{{ company }}" placeholder="需要定向补证时填写">
        </label>
        <label class="field">
          <span>搜索强度</span>
          <select name="intensity">
            {% for item in intensity_options %}
            <option value="{{ item.value }}" {% if intensity == item.value %}selected{% endif %}>{{ item.label }}</option>
            {% endfor %}
          </select>
        </label>
        <label class="field field-wide">
          <span>待核验原文链接（可选，每行一个）</span>
          <textarea name="urls" placeholder="把公司官网、融资公告、招投标、监管文件或公众号原文链接直接粘贴到这里">{{ manual_urls_text }}</textarea>
          <span class="field-note">搜索结果页和摘要只能作为线索；系统会尝试读取原文，只有引文能回到正文且主体匹配时才进入评分。</span>
        </label>
      </div>
      <div class="actions">
        <a class="btn-link btn-secondary" href="http://127.0.0.1:8791/">← 返回证据雷达</a>
        <button class="btn btn-primary" type="submit" formaction="{{ url_for('generate_links') }}">生成发现链接</button>
        <button class="btn btn-secondary" type="submit" formaction="{{ url_for('save_manual_urls_route') }}">保存原文链接</button>
        <button class="btn btn-primary" id="runPipelineButton" type="submit" formaction="{{ url_for('run_pipeline_route') }}" {% if pipeline_runtime.running %}disabled{% endif %}>{{ '正在后台评估…' if pipeline_runtime.running else '运行采集与评分' }}</button>
        <button class="btn btn-secondary" type="submit" formaction="{{ url_for('open_input') }}">打开 urls.txt</button>
        <button class="btn btn-secondary" type="submit" formaction="{{ url_for('open_project') }}">打开项目目录</button>
        <button class="btn btn-secondary" type="submit" formaction="{{ url_for('login_platform', platform='xiaohongshu') }}">登录小红书</button>
        <button class="btn btn-secondary" type="submit" formaction="{{ url_for('login_platform', platform='zsxq') }}">登录知识星球</button>
        <button class="btn btn-secondary" type="submit" formaction="{{ url_for('login_platform', platform='weixin') }}">登录公众号</button>
      </div>
    </form>
  </section>

  {% if banner_message %}
  <div class="banner">{{ banner_message }}</div>
  {% endif %}

  {% if report.is_demo %}
  <div class="banner" style="background:#fff7ed;border-color:#fdba74;color:#9a3412;">
    当前载入的是历史演示样例，其中含占位网址，不能作为真实项目判断。运行一次取得原文证据的评估后才会替换；没有合格证据时系统会保留旧文件但不会假装成功。
  </div>
  {% endif %}

  {% if latest_attempt and not latest_attempt.get('ok') %}
  <div class="banner" style="background:#fff7ed;border-color:#fdba74;color:#9a3412;">
    最近一次评估未生成新报告：{{ latest_attempt.get('message', '没有取得可核验的新证据。') }}
  </div>
  {% endif %}

  {% if pipeline_runtime.running or pipeline_runtime.message %}
  <div id="pipeline-status" class="banner" style="background:{{ '#eff6ff' if pipeline_runtime.running or pipeline_runtime.status == 'success' else '#fff7ed' }};border-color:{{ '#93c5fd' if pipeline_runtime.running or pipeline_runtime.status == 'success' else '#fdba74' }};color:{{ '#1e40af' if pipeline_runtime.running or pipeline_runtime.status == 'success' else '#9a3412' }};">
    {% if pipeline_runtime.running %}正在后台读取原文并评估“{{ pipeline_runtime.thesis }}”。你可以停留在本页，完成后会自动刷新；也可以先返回证据雷达。{% else %}{{ pipeline_runtime.message }}{% endif %}
  </div>
  {% endif %}

  <section class="surface">
    <div class="stats">
      <div class="stat-card">
        <div class="stat-label">候选公司数</div>
        <div class="stat-value">{{ report.stats.total_candidates }}</div>
        <div class="stat-note">{{ report.stats.top_bucket }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">证据总量</div>
        <div class="stat-value">{{ report.stats.total_evidence }}</div>
        <div class="stat-note">含自动搜索与手工 URLs</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">覆盖域名</div>
        <div class="stat-value">{{ report.stats.source_domains }}</div>
        <div class="stat-note">{{ report.stats.domain_preview }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">搜索引擎</div>
        <div class="stat-value">{{ report.stats.search_engine_count }}</div>
        <div class="stat-note">{{ report.stats.search_engine_text }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均置信分</div>
        <div class="stat-value">{{ fmt(report.stats.avg_confidence) }}</div>
        <div class="stat-note">{{ report.scoring_version }} ｜ {{ report.compatibility_note }}</div>
      </div>
    </div>
  </section>

  <section class="surface">
    <button class="section-head" type="button" data-collapse-target="discovery-links" aria-expanded="true">
      <div>
        <div class="section-title">发现链接</div>
        <div class="section-subtitle">按渠道分组的发现入口，适合人工浏览、筛选和补录 `urls.txt`。这里已经扩展到公众号、小红书、知识社区、工商融资、招聘招投标、专利标准、视频社交和供应链信号。</div>
      </div>
      <span class="toggle-icon">+</span>
    </button>
    <div class="collapse" id="discovery-links" data-open="true">
      <div class="collapse-inner">
        {% if grouped_links %}
        <div class="link-groups">
          {% for group in grouped_links %}
          <div class="link-group">
            <h3 class="group-title">{{ group.title }}</h3>
            <div class="link-list">
              {% for item in group.links %}
              <a class="link-item" href="{{ item.url }}" target="_blank" rel="noopener noreferrer">
                <strong>{{ item.name }}</strong>
                <div class="link-url">{{ item.url }}</div>
              </a>
              {% endfor %}
            </div>
          </div>
          {% endfor %}
        </div>
        {% else %}
        <div class="empty-state">输入研究主题后点击“生成发现链接”，这里会按微信/公众号、小红书、知识社区、企业/官方、政策/监管等渠道展开。</div>
        {% endif %}
      </div>
    </div>
  </section>

  <section class="surface">
    <div class="section-head" style="cursor:default;">
      <div>
        <div class="section-title">采集打法与登录态</div>
        <div class="section-subtitle">先保存登录态，再抓原文；社交平台主要用于找线索，关键判断尽量回指到官网、数据库、招投标、专利或监管链接。</div>
      </div>
      <span class="toggle-icon" style="background:#eef2ff;color:#334155;">可执行</span>
    </div>
    <div class="penalty-grid">
      <div class="panel-card">
        <h4>登录态状态</h4>
        <div class="stack">
          {% for item in session_statuses %}
          <div class="stack-item">
            <strong>{{ item.name }}</strong>
            <div class="stack-meta">{{ item.status_text }} ｜ {{ item.hint }}</div>
            <div style="margin-top:10px;">
              <form method="post" action="{{ item.action_url }}"><button class="btn btn-primary" type="submit">{{ item.button_label }}</button></form>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% for section in collector_playbook %}
      <div class="panel-card">
        <h4>{{ section.title }}</h4>
        <div class="stack">
          {% for item in section["items"] %}
          <div class="stack-item">
            <strong>{{ item.name }}</strong>
            <div class="stack-meta">{{ item.capability }}</div>
            <div class="stack-meta" style="margin-top:6px;">{{ item.workflow }}</div>
            <div class="stack-meta" style="margin-top:6px;">{{ item.status_text }}</div>
            {% if item.path_text %}
            <div class="stack-meta" style="margin-top:6px;word-break:break-all;">{{ item.path_text }}</div>
            {% endif %}
            {% if item.action_url %}
            <div style="margin-top:10px;">
              <form method="post" action="{{ item.action_url }}"><button class="btn btn-primary" type="submit">立即处理</button></form>
            </div>
            {% endif %}
          </div>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>
  </section>

  <section class="surface">
    <div class="section-head" style="cursor:default;">
      <div>
        <div class="section-title">待核验项目卡片</div>
        <div class="section-subtitle">{{ report.subtitle_line }}</div>
      </div>
      <span class="toggle-icon" style="background:#eef2ff;color:#334155;">{{ report.candidates|length }}</span>
    </div>
    {% if report.candidates %}
      {% for candidate in report.candidates %}
      <article class="candidate">
        <div class="candidate-head">
          <div>
            <h2 class="candidate-title">{{ candidate.name }}</h2>
            <div class="candidate-summary">{{ candidate.summary }}</div>
          </div>
          <div class="score-badge {{ candidate.badge_class }}">
            <span class="score-number">{{ fmt(candidate.total_score) }}</span>
            <div class="score-label">{{ candidate.score_bucket }}</div>
          </div>
        </div>

        <div class="triple-grid">
          <div class="metric-card">
            <div class="metric-label">线索分</div>
            <div class="metric-value">{{ fmt(candidate.opportunity_score) }}</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">置信分</div>
            <div class="metric-value">{{ fmt(candidate.confidence_score) }}</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">核验优先级</div>
            <div class="metric-value">{{ fmt(candidate.priority_score) }}</div>
          </div>
        </div>
        <div class="muted" style="margin-top:10px;">以上为证据整理排序分，仅用于安排核验顺序和暴露信息缺口；不代表投资质量、收益概率、估值结论或交易建议。</div>

        <div class="dimension-board">
          {% for dim in candidate.dimension_rows %}
          <div class="dimension-row">
            <div class="dimension-top">
              <div class="dimension-name">{{ dim.label }}</div>
              <div class="dimension-meta">
                <span class="mini-pill">权重 {{ fmt(dim.weight) }}</span>
                <span class="mini-pill">得分 {{ dim.score_display }}</span>
                <span class="mini-pill">档位 {{ dim.level_display }}</span>
                {% if dim.discipline_applied %}
                <span class="mini-pill warning">⚠️ 证据约束</span>
                {% endif %}
              </div>
            </div>
            <div class="bar-track">
              <div class="bar-fill level-{{ dim.level or 0 }}" style="width: {{ dim.width_pct }}%;"></div>
            </div>
            <div class="dimension-foot">
              <span>{{ dim.guide }}</span>
              <span>置信度 {{ dim.confidence_label }}</span>
              {% if dim.support_summary %}
              <span>{{ dim.support_summary }}</span>
              {% endif %}
              {% if dim.compatibility_note %}
              <span class="warning">{{ dim.compatibility_note }}</span>
              {% endif %}
            </div>
          </div>
          {% endfor %}
        </div>

        <section class="surface" style="box-shadow:none;border:1px solid #e8eef6;margin-top:18px;">
          <button class="section-head" type="button" data-collapse-target="breakdown-{{ loop.index }}" aria-expanded="false">
            <div>
              <div class="section-title">维度拆解</div>
              <div class="section-subtitle">子维度、模型档位与客观校准对比，以及每维置信度。</div>
            </div>
            <span class="toggle-icon">+</span>
          </button>
          <div class="collapse" id="breakdown-{{ loop.index }}" data-open="false">
            <div class="collapse-inner">
              <div class="detail-grid">
                {% for dim in candidate.dimension_rows %}
                <div class="detail-card">
                  <h4>{{ dim.label }}</h4>
                  {% if dim.sub_dimensions %}
                  <div class="sub-list">
                    {% for sub in dim.sub_dimensions %}
                    <span class="sub-pill">{{ sub.label }} {{ sub.weight_pct }}%</span>
                    {% endfor %}
                  </div>
                  {% else %}
                  <div class="muted">当前报告未提供子维度拆解。</div>
                  {% endif %}
                  <div class="stack" style="margin-top:12px;">
                    <div class="stack-item">
                      <strong>校准对比</strong>
                      <div class="stack-meta">模型档位 {{ dim.model_level_display }} ｜ 客观档位 {{ dim.objective_level_display }} ｜ 最终档位 {{ dim.level_display }}</div>
                    </div>
                    <div class="stack-item">
                      <strong>置信度</strong>
                      <div class="stack-meta">{{ dim.confidence_label }}</div>
                    </div>
                    {% if dim.support_count %}
                    <div class="stack-item">
                      <strong>维度支持证据</strong>
                      <div class="stack-meta">{{ dim.support_summary }} ｜ 支持分 {{ fmt(dim.support_score) }}</div>
                    </div>
                    {% endif %}
                    {% if dim.discipline_reason %}
                    <div class="stack-item">
                      <strong>证据约束说明</strong>
                      <div class="stack-meta">{{ dim.discipline_reason }}</div>
                    </div>
                    {% endif %}
                  </div>
                </div>
                {% endfor %}
              </div>
            </div>
          </div>
        </section>

        <div class="penalty-grid">
          <div class="panel-card">
            <h4>排除项与惩罚</h4>
            {% if candidate.exclusion_penalties %}
            <div class="stack">
              {% for item in candidate.exclusion_penalties %}
              <div class="stack-item">
                <div class="penalty-tag">{{ item.name }} · -{{ fmt(item.penalty) }}</div>
                <div class="stack-meta" style="margin-top:8px;">{{ item.severity }} ｜ {{ item.desc }}</div>
              </div>
              {% endfor %}
            </div>
            {% else %}
            <div class="empty-state">当前未触发排除惩罚。</div>
            {% endif %}
          </div>
          <div class="panel-card">
            <h4>硬门槛</h4>
            {% if candidate.gate_details %}
            <div class="stack">
              {% for gate in candidate.gate_details %}
              <div class="stack-item">
                <strong>{{ gate.name }}</strong>
                <div class="stack-meta">封顶 {{ fmt(gate.cap) }} ｜ {{ gate.reason }}</div>
              </div>
              {% endfor %}
            </div>
            {% else %}
            <div class="empty-state">当前未触发硬门槛封顶。</div>
            {% endif %}
          </div>
          <div class="panel-card">
            <h4>交易动量</h4>
            <div class="stack">
              <div class="stack-item">
                <strong>动量加分</strong>
                <div class="stack-meta">{{ fmt(candidate.deal_momentum_bonus) }}</div>
              </div>
              <div class="stack-item">
                <strong>动量信号</strong>
                <div class="stack-meta">{{ candidate.deal_momentum_text }}</div>
              </div>
            </div>
          </div>
          <div class="panel-card">
            <h4>风险概览</h4>
            <div class="stack">
              <div class="stack-item"><strong>高风险</strong><div class="stack-meta">{{ candidate.risk_summary.high }}</div></div>
              <div class="stack-item"><strong>中风险</strong><div class="stack-meta">{{ candidate.risk_summary.medium }}</div></div>
              <div class="stack-item"><strong>低风险</strong><div class="stack-meta">{{ candidate.risk_summary.low }}</div></div>
              <div class="stack-item"><strong>总计</strong><div class="stack-meta">{{ candidate.risk_summary.total }}</div></div>
            </div>
          </div>
          <div class="panel-card">
            <h4>来源结构</h4>
            <div class="stack">
              <div class="stack-item"><strong>官方/数据库锚点</strong><div class="stack-meta">{{ candidate.source_profile.official_anchor_count }}</div></div>
              <div class="stack-item"><strong>权威证据</strong><div class="stack-meta">{{ candidate.source_profile.authoritative_evidence_count }}</div></div>
              <div class="stack-item"><strong>多渠道检索命中</strong><div class="stack-meta">{{ candidate.source_profile.multi_provider_retrieval_count }}（不等于事实互证）</div></div>
              <div class="stack-item"><strong>独立来源确认</strong><div class="stack-meta">{{ candidate.source_profile.independently_corroborated_count }}</div></div>
              <div class="stack-item"><strong>社交依赖</strong><div class="stack-meta">{{ fmt(candidate.source_profile.social_dependency_ratio) }}%</div></div>
            </div>
          </div>
        </div>

        <section class="surface" style="box-shadow:none;border:1px solid #e8eef6;margin-top:18px;">
          <button class="section-head" type="button" data-collapse-target="evidence-{{ loop.index }}" aria-expanded="false">
            <div>
              <div class="section-title">证据表</div>
              <div class="section-subtitle">按重要度降序，保留 claim_type、source_tier、平台、溯源状态与原文链接。</div>
            </div>
            <span class="toggle-icon">+</span>
          </button>
          <div class="collapse" id="evidence-{{ loop.index }}" data-open="false">
            <div class="collapse-inner">
              {% if candidate.evidence %}
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Claim Type</th>
                      <th>来源层级</th>
                      <th>重要度</th>
                      <th>溯源/复核</th>
                      <th>引用</th>
                      <th>来源链接</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for item in candidate.evidence %}
                    <tr>
                      <td>
                        <div class="badge-row">
                          <span class="claim-badge">{{ item.claim_type_label }}</span>
                          <span class="platform-badge">{{ item.platform_label }}</span>
                        </div>
                      </td>
                      <td><span class="tier-badge">{{ item.source_tier_label }}</span></td>
                      <td>{{ item.importance }}</td>
                      <td>
                        <div>{{ item.traceability_label }}</div>
                        {% if item.provider_text %}
                        <div class="muted">{{ item.provider_text }}</div>
                        {% endif %}
                      </td>
                      <td>{{ item.quote }}</td>
                      <td>
                        {% if item.source_url %}
                        <a class="source-link" href="{{ item.source_url }}" target="_blank" rel="noopener noreferrer">{{ item.source_url }}</a>
                        {% else %}
                        <span class="muted">无来源链接</span>
                        {% endif %}
                      </td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
              {% else %}
              <div class="empty-state">当前候选尚未生成可展示证据。</div>
              {% endif %}
            </div>
          </div>
        </section>

        <div class="diagnostic-grid">
          {% for diag in candidate.diagnostic_cards %}
          <div class="diagnostic-card">
            <h4>{{ diag.label }}</h4>
            <div class="diag-value">{{ fmt(diag.value) }}</div>
          </div>
          {% endfor %}
        </div>
      </article>
      {% endfor %}
    {% else %}
      <div class="candidate">
        <div class="empty-state">还没有可展示的候选公司。先生成发现链接，再把高价值链接贴入 `urls.txt` 或直接运行自动搜索与评分。</div>
      </div>
    {% endif %}
  </section>
</div>
<script>
function syncPanel(panel) {
  const open = panel.dataset.open === 'true';
  panel.style.maxHeight = open ? panel.scrollHeight + 'px' : '0px';
  panel.style.opacity = open ? '1' : '0';
}
document.querySelectorAll('.collapse').forEach(syncPanel);
document.querySelectorAll('[data-collapse-target]').forEach((button) => {
  button.addEventListener('click', () => {
    const panel = document.getElementById(button.dataset.collapseTarget);
    const icon = button.querySelector('.toggle-icon');
    const open = panel.dataset.open === 'true';
    panel.dataset.open = open ? 'false' : 'true';
    button.setAttribute('aria-expanded', open ? 'false' : 'true');
    if (icon) icon.textContent = open ? '+' : '−';
    syncPanel(panel);
  });
});
window.addEventListener('resize', () => {
  document.querySelectorAll('.collapse[data-open="true"]').forEach(syncPanel);
});
if (document.body.dataset.pipelineRunning === 'true') {
  const started = Date.now();
  const timer = window.setInterval(async () => {
    try {
      const response = await fetch('/api/pipeline-status', { cache: 'no-store' });
      const state = await response.json();
      if (!state.running) {
        window.clearInterval(timer);
        window.location.reload();
      } else if (Date.now() - started > 15 * 60 * 1000) {
        window.clearInterval(timer);
        const banner = document.getElementById('pipeline-status');
        if (banner) banner.textContent = '评估仍在后台运行。可以稍后刷新本页查看结果。';
      }
    } catch (_) {
      // Local service may be busy for a moment; the next poll will retry.
    }
  }, 1800);
}
</script>
</body>
</html>
"""


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return dtparser.parse(str(value))
    except Exception:
        return None


def safe_float(value: Any, default: float | None = 0.0) -> float | None:
    if value in (None, "", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    if value in (None, "", "--"):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fmt(value: Any) -> str:
    number = safe_float(value, None)
    if number is None:
        return "--"
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.1f}"


def build_discovery_links(query: str) -> list[dict[str, Any]]:
    templates = load_json(SOURCE_TEMPLATE_PATH) or []
    query = (query or "").strip()
    if not query:
        return []
    encoded = quote_plus(query)
    links: list[dict[str, Any]] = []
    for item in templates:
        if not isinstance(item, dict):
            continue
        template = str(item.get("template", ""))
        links.append(
            {
                "name": item.get("name", "未命名链接"),
                "group": item.get("group", "其他"),
                "url": template.replace("{query}", encoded),
            }
        )
    return links


def normalize_manual_urls(raw_text: str) -> tuple[list[str], list[str]]:
    accepted: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw_line in str(raw_text or "").replace("，", "\n").splitlines():
        url = raw_line.strip()
        if not url or url.startswith("#"):
            continue
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or len(url) > 2048:
            rejected.append(url[:160])
            continue
        if url in seen:
            continue
        seen.add(url)
        accepted.append(url)
        if len(accepted) >= MAX_MANUAL_URLS:
            break
    return accepted, rejected


def save_manual_urls(raw_text: str) -> tuple[list[str], list[str]]:
    accepted, rejected = normalize_manual_urls(raw_text)
    URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = [
        "# 页面中保存的待核验原文链接；一行一个",
        "# 搜索摘要不作为证据，抓取和引文匹配成功后才进入评分",
        *accepted,
    ]
    temporary_path = URLS_PATH.with_suffix(URLS_PATH.suffix + ".tmp")
    temporary_path.write_text("\n".join(content) + "\n", encoding="utf-8")
    temporary_path.replace(URLS_PATH)
    return accepted, rejected


def load_manual_urls_text() -> str:
    if not URLS_PATH.exists():
        return ""
    accepted, _ = normalize_manual_urls(URLS_PATH.read_text(encoding="utf-8-sig"))
    return "\n".join(accepted)


def load_saved_links() -> list[dict[str, Any]]:
    data = load_json(DISCOVERY_LINKS_PATH)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("links"), list):
        return [item for item in data["links"] if isinstance(item, dict)]
    return []


def discover_skills_root() -> Path | None:
    candidates: list[Path] = []
    env_root = os.getenv("PE_SKILLS_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_skill_path(skill_key: str, skills_root: Path | None) -> Path | None:
    relative = SKILL_PATH_HINTS.get(skill_key)
    if not relative or not skills_root:
        return None
    candidate = skills_root / relative
    return candidate if candidate.exists() else None


def load_collector_playbook() -> list[dict[str, Any]]:
    raw = load_json(COLLECTOR_PLAYBOOK_PATH)
    sections = raw.get("sections") if isinstance(raw, dict) else []
    if not isinstance(sections, list):
        return []
    skills_root = discover_skills_root()
    normalized_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        if section.get("title") == "登录态采集":
            continue
        items_out: list[dict[str, Any]] = []
        for item in section.get("items", []):
            if not isinstance(item, dict):
                continue
            row = dict(item)
            if row.get("kind") == "skill":
                skill_path = resolve_skill_path(str(row.get("skill_key", "")), skills_root)
                row["status_text"] = "已发现" if skill_path else "未发现"
                row["path_text"] = str(skill_path or ((skills_root / row.get("path")) if skills_root and row.get("path") else row.get("path", "")))
            elif row.get("kind") == "session":
                platform_key = str(row.get("platform_key", ""))
                state_path = SESSIONS_DIR / f"{platform_key}.json"
                row["status_text"] = "已保存登录态" if state_path.exists() else "未保存登录态"
                row["action_url"] = url_for("login_platform", platform=platform_key)
            else:
                row["status_text"] = "规则"
                row["path_text"] = ""
            items_out.append(row)
        normalized_sections.append(
            {
                "title": section.get("title", "未命名模块"),
                "subtitle": section.get("subtitle", ""),
                "items": items_out,
            }
        )
    return normalized_sections


def build_session_statuses() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform, meta in SESSION_PLATFORM_META.items():
        state_path = SESSIONS_DIR / f"{platform}.json"
        rows.append(
            {
                "name": meta["label"],
                "hint": meta["hint"],
                "status_text": "已保存登录态" if state_path.exists() else "未保存登录态",
                "button_label": f"登录并保存 {meta['label']}",
                "action_url": url_for("login_platform", platform=platform),
            }
        )
    return rows


def group_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in links:
        buckets[str(item.get("group", "其他"))].append(item)
    ordered_groups: list[str] = []
    for group in GROUP_ORDER:
        if group in buckets:
            ordered_groups.append(group)
    for group in sorted(buckets):
        if group not in ordered_groups:
            ordered_groups.append(group)
    return [{"title": group, "links": buckets[group]} for group in ordered_groups]


def platform_label(platform: str, source_url: str) -> str:
    platform = (platform or "").strip()
    if platform and platform not in {"general", "unknown"}:
        mapping = {
            "weixin": "微信公众号",
            "xiaohongshu": "小红书",
            "zsxq": "知识星球",
        }
        return mapping.get(platform, platform)
    domain = urlparse(source_url or "").netloc.lower()
    if "mp.weixin.qq.com" in domain:
        return "微信公众号"
    if "xiaohongshu.com" in domain:
        return "小红书"
    if "zsxq.com" in domain:
        return "知识星球"
    if "gov.cn" in domain:
        return "政府/监管"
    return domain or "通用网页"


def traceability_label(value: str) -> str:
    mapping = {
        "verified": "引文已匹配原文",
        "matched": "有来源，待引文核验",
        "declared": "仅声明",
        "unverified": "未复核",
        "missing": "缺失",
    }
    return mapping.get((value or "").strip(), value or "未标注")


def claim_type_label(value: str) -> str:
    return CLAIM_TYPE_LABELS.get(value or "", value or "未分类")


def source_tier_label(value: str) -> str:
    return SOURCE_TIER_LABELS.get(value or "", value or "未标注")


def confidence_label(value: str) -> str:
    return CONFIDENCE_LABELS.get(value or "", value or "未标注")


def bucket_for_score(score: float) -> str:
    if score >= 78:
        return "证据较完整"
    if score >= 68:
        return "优先核验"
    if score >= 58:
        return "持续观察"
    if score >= 45:
        return "待补证"
    return "暂缓核验"


def badge_class_for_score(score: float) -> str:
    if score >= 78:
        return "badge-green"
    if score >= 68:
        return "badge-blue"
    if score >= 58:
        return "badge-orange"
    return "badge-gray"


def level_from_score(score: float | None, weight: float | None) -> int:
    if score is None or not weight:
        return 0
    return max(1, min(5, round((score / weight) * 5)))


def derive_diagnostics(evidence: list[dict[str, Any]]) -> dict[str, float]:
    total = len(evidence)
    if not total:
        return {
            "evidence_strength": 0.0,
            "freshness_score": 0.0,
            "traceability_score": 0.0,
            "source_authority_score": 0.0,
            "cross_validation_score": 0.0,
            "data_quality": 0.0,
            "execution_readiness": 0.0,
        }

    domains = {urlparse(str(item.get("source_url", ""))).netloc.lower() for item in evidence if item.get("source_url")}
    traceable = sum(
        1
        for item in evidence
        if item.get("source_url") and item.get("quote") and item.get("quote_verified") is True
    )
    authority_scores = [SOURCE_TIER_SCORES.get(str(item.get("source_tier", "")), 35) for item in evidence]

    freshness_values = []
    now = datetime.now(timezone.utc)
    for item in evidence:
        dt = parse_dt(item.get("event_date")) or parse_dt(item.get("published_at"))
        if not dt:
            freshness_values.append(0.0)
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 86400.0)
        freshness_values.append(max(12.0, 100.0 - min(age_days, 365.0) * 0.22))

    cross_hits = sum(
        1
        for item in evidence
        if item.get("independently_corroborated")
        and safe_int(item.get("independent_source_count"), 0) >= 2
    )
    cross_validation = min(100.0, cross_hits * 35.0)

    evidence_strength = min(100.0, total * 9.0 + len(domains) * 8.0)
    traceability = min(100.0, traceable / max(total, 1) * 100.0)
    authority = sum(authority_scores) / len(authority_scores)
    freshness = sum(freshness_values) / len(freshness_values)
    data_quality = round((traceability * 0.35 + authority * 0.30 + cross_validation * 0.35), 1)
    execution_readiness = round((evidence_strength * 0.45 + freshness * 0.20 + cross_validation * 0.35), 1)
    return {
        "evidence_strength": round(evidence_strength, 1),
        "freshness_score": round(freshness, 1),
        "traceability_score": round(traceability, 1),
        "source_authority_score": round(authority, 1),
        "cross_validation_score": round(cross_validation, 1),
        "data_quality": round(data_quality, 1),
        "execution_readiness": round(execution_readiness, 1),
    }


def normalize_evidence_rows(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        providers = item.get("providers") if isinstance(item.get("providers"), list) else []
        provider = str(item.get("provider", "")).strip()
        if not providers and provider:
            providers = [provider]
        provider_count = safe_int(item.get("provider_count"), len(providers))
        provider_text = ""
        if provider_count >= 2:
            provider_text = f"{provider_count} 个检索渠道命中（非独立互证）"
        elif providers:
            provider_text = providers[0]
        normalized.append(
            {
                "claim_type_label": claim_type_label(str(item.get("claim_type", ""))),
                "platform_label": platform_label(str(item.get("platform", "")), str(item.get("source_url", ""))),
                "source_tier_label": source_tier_label(str(item.get("source_tier", ""))),
                "importance": safe_int(item.get("importance"), 0),
                "quote": str(item.get("quote", "")).strip() or "无引用摘要",
                "source_url": str(item.get("source_url", "")).strip(),
                "traceability_label": traceability_label(str(item.get("traceability", ""))),
                "provider_text": provider_text,
            }
        )
    normalized.sort(key=lambda row: row["importance"], reverse=True)
    return normalized


def normalize_penalties(raw_candidate: dict[str, Any], computed: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    penalties = raw_candidate.get("exclusion_penalties") or computed.get("penalty_details") or raw_candidate.get("penalty_details") or []
    normalized: list[dict[str, Any]] = []
    if isinstance(penalties, list) and penalties:
        for item in penalties:
            if isinstance(item, dict):
                normalized.append(
                    {
                        "name": item.get("name") or item.get("tag") or "未命名惩罚",
                        "penalty": safe_float(item.get("penalty"), 0.0),
                        "severity": item.get("severity") or "未标注",
                        "desc": item.get("desc") or item.get("reason") or "",
                    }
                )
    if normalized:
        return normalized

    tags = raw_candidate.get("exclusion_tags") or []
    penalty_lookup = config.get("exclusion_penalty") or {}
    for tag in tags:
        info = penalty_lookup.get(tag, {})
        normalized.append(
            {
                "name": tag,
                "penalty": safe_float(info.get("penalty"), 0.0),
                "severity": info.get("severity") or "未标注",
                "desc": info.get("desc") or "",
            }
        )
    return normalized


def normalize_gate_details(raw_candidate: dict[str, Any], computed: dict[str, Any]) -> list[dict[str, Any]]:
    gates = raw_candidate.get("gate_details") or computed.get("gate_details") or []
    normalized = []
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        normalized.append(
            {
                "name": gate.get("name") or "未命名门槛",
                "cap": safe_float(gate.get("cap"), 0.0),
                "reason": gate.get("reason") or "",
            }
        )
    return normalized


def normalize_risk_summary(raw_candidate: dict[str, Any], computed: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, int]:
    risk = raw_candidate.get("risk_summary") or computed.get("risk_summary")
    if isinstance(risk, dict):
        return {
            "high": safe_int(risk.get("high"), 0),
            "medium": safe_int(risk.get("medium"), 0),
            "low": safe_int(risk.get("low"), 0),
            "total": safe_int(risk.get("total"), 0),
        }
    risk_total = sum(1 for item in evidence if str(item.get("claim_type")) == "risk_signal")
    return {"high": 0, "medium": risk_total, "low": 0, "total": risk_total}


def build_dimension_rows(raw_candidate: dict[str, Any], computed: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions = config.get("dimensions") or []
    dimension_scores = raw_candidate.get("dimension_scores") or computed.get("dimension_scores") or {}
    dimension_details = raw_candidate.get("dimension_details") or computed.get("dimension_details") or {}
    score_levels = raw_candidate.get("score_levels") or {}
    confidence_levels = raw_candidate.get("confidence_levels") or {}
    adjusted_weights = raw_candidate.get("adjusted_weights") or computed.get("adjusted_weights") or {}

    rows = []
    for dim in dimensions:
        key = dim.get("key")
        detail = dimension_details.get(key) if isinstance(dimension_details, dict) else {}
        if not isinstance(detail, dict):
            detail = {}
        score = safe_float(dimension_scores.get(key), None) if isinstance(dimension_scores, dict) else None
        if score is None:
            score = safe_float(detail.get("adjusted"), None)
        weight = safe_float(adjusted_weights.get(key), None) if isinstance(adjusted_weights, dict) else None
        if weight is None:
            weight = safe_float(detail.get("weight"), safe_float(dim.get("weight"), 0.0))
        level = safe_int(detail.get("raw_level"), 0)
        if not level and isinstance(score_levels, dict):
            level = safe_int(score_levels.get(key), 0)
        if not level:
            level = level_from_score(score, weight)
        confidence = detail.get("confidence") or (confidence_levels.get(key) if isinstance(confidence_levels, dict) else None)
        support = detail.get("evidence_support") if isinstance(detail.get("evidence_support"), dict) else {}
        support_summary = ""
        if support:
            support_summary = (
                f"支持证据 {safe_int(support.get('count'), 0)} 条"
                f" / 权威 {safe_int(support.get('authoritative_count'), 0)}"
                f" / 近180天 {safe_int(support.get('recent_180'), 0)}"
                f" / 已验证 {safe_int(support.get('verified_count'), 0)}"
            )
        width_pct = 0.0
        if score is not None and weight:
            width_pct = max(0.0, min(100.0, score / weight * 100.0))
        elif level:
            width_pct = level / 5.0 * 100.0
        rows.append(
            {
                "key": key,
                "label": dim.get("label", key),
                "guide": dim.get("guide", ""),
                "weight": weight or 0.0,
                "score": score,
                "score_display": fmt(score),
                "level": level,
                "level_display": str(level) if level else "--",
                "width_pct": f"{width_pct:.1f}",
                "confidence_label": confidence_label(str(confidence or "medium")),
                "model_level_display": str(safe_int(detail.get("model_level"), 0)) if detail.get("model_level") is not None else "--",
                "objective_level_display": str(safe_int(detail.get("objective_level"), 0)) if detail.get("objective_level") is not None else "--",
                "discipline_applied": bool(detail.get("discipline_applied")),
                "discipline_reason": str(detail.get("discipline_reason", "")).strip(),
                "support_summary": support_summary,
                "support_score": safe_float(support.get("support_score"), 0.0),
                "support_count": safe_int(support.get("count"), 0),
                "sub_dimensions": dim.get("sub_dimensions") or [],
                "compatibility_note": "旧版报告未提供该维度明细，当前展示为兼容占位。" if score is None and not detail else "",
            }
        )
    return rows


def normalize_candidate(raw_candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    computed = raw_candidate.get("_computed") if isinstance(raw_candidate.get("_computed"), dict) else {}
    evidence = raw_candidate.get("evidence") if isinstance(raw_candidate.get("evidence"), list) else []
    diagnostics = raw_candidate.get("diagnostics") or computed.get("diagnostics") or derive_diagnostics(evidence)
    if not isinstance(diagnostics, dict):
        diagnostics = derive_diagnostics(evidence)

    total_score = safe_float(raw_candidate.get("total_score"), None)
    if total_score is None:
        total_score = safe_float(computed.get("total_score"), None)
    if total_score is None:
        total_score = safe_float(raw_candidate.get("opportunity_score"), 0.0)

    opportunity_score = safe_float(raw_candidate.get("opportunity_score"), None)
    if opportunity_score is None:
        opportunity_score = safe_float(total_score, 0.0)
    confidence_score = safe_float(raw_candidate.get("confidence_score"), None)
    if confidence_score is None:
        confidence_score = safe_float(diagnostics.get("confidence_index"), 0.0)
    priority_score = safe_float(raw_candidate.get("priority_score"), None)
    if priority_score is None:
        priority_score = safe_float(total_score, 0.0)

    dimension_rows = build_dimension_rows(raw_candidate, computed, config)
    # Public UI always derives a neutral evidence-status label. Historical reports
    # may carry recommendation-oriented bucket names that must not be rendered.
    score_bucket = bucket_for_score(total_score or 0.0)
    deal_momentum = raw_candidate.get("deal_momentum") or computed.get("deal_momentum") or {}
    if not isinstance(deal_momentum, dict):
        deal_momentum = {}
    signals = deal_momentum.get("signals") if isinstance(deal_momentum.get("signals"), list) else []
    risk_summary = normalize_risk_summary(raw_candidate, computed, evidence)
    evidence_rows = normalize_evidence_rows(evidence)
    diagnostic_cards = [
        {"label": "证据强度", "value": diagnostics.get("evidence_strength", 0)},
        {"label": "时效分", "value": diagnostics.get("freshness_score", 0)},
        {"label": "可溯源分", "value": diagnostics.get("traceability_score", 0)},
        {"label": "来源权威", "value": diagnostics.get("source_authority_score", 0)},
        {"label": "交叉验证", "value": diagnostics.get("cross_validation_score", 0)},
        {"label": "证据链", "value": diagnostics.get("evidence_chain_score", 0)},
        {"label": "数据质量", "value": diagnostics.get("data_quality", 0)},
        {"label": "执行就绪", "value": diagnostics.get("execution_readiness", 0)},
    ]
    signal_stats = diagnostics.get("signal_stats") if isinstance(diagnostics.get("signal_stats"), dict) else {}

    summary = raw_candidate.get("summary") or raw_candidate.get("evidence_summary") or "暂无公开证据摘要。"
    return {
        "name": raw_candidate.get("entity") or raw_candidate.get("name") or raw_candidate.get("company_name") or "未命名候选",
        "summary": summary,
        "total_score": total_score or 0.0,
        "score_bucket": score_bucket,
        "badge_class": badge_class_for_score(total_score or 0.0),
        "opportunity_score": opportunity_score or 0.0,
        "confidence_score": confidence_score or 0.0,
        "priority_score": priority_score or 0.0,
        "dimension_rows": dimension_rows,
        "exclusion_penalties": normalize_penalties(raw_candidate, computed, config),
        "gate_details": normalize_gate_details(raw_candidate, computed),
        "deal_momentum_bonus": safe_float(deal_momentum.get("bonus"), 0.0) or 0.0,
        "deal_momentum_text": " / ".join(str(item) for item in signals) if signals else "暂无明显交易动量",
        "risk_summary": risk_summary,
        "source_profile": {
            "official_anchor_count": safe_int(signal_stats.get("official_anchor_count"), 0),
            "authoritative_evidence_count": safe_int(signal_stats.get("authoritative_evidence_count"), 0),
            "cross_provider_evidence_count": safe_int(signal_stats.get("cross_provider_evidence_count"), 0),
            "multi_provider_retrieval_count": safe_int(signal_stats.get("multi_provider_retrieval_count"), 0),
            "independently_corroborated_count": safe_int(signal_stats.get("independently_corroborated_count"), 0),
            "corroborated_claim_count": safe_int(signal_stats.get("corroborated_claim_count"), 0),
            "social_dependency_ratio": (safe_float(diagnostics.get("social_dependency_ratio"), 0.0) or 0.0) * 100,
        },
        "evidence": evidence_rows,
        "diagnostic_cards": diagnostic_cards,
    }


def is_demo_report(raw: Any) -> bool:
    """Conservatively flag bundled/example output without hiding user reports."""
    base = raw.get("report") if isinstance(raw, dict) and isinstance(raw.get("report"), dict) else raw
    if not isinstance(base, dict):
        return False
    meta = base.get("meta") if isinstance(base.get("meta"), dict) else {}
    if bool(base.get("is_demo")) or bool(meta.get("is_demo")):
        return True
    candidates = base.get("candidates") or base.get("candidate_companies") or []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            parsed = urlparse(str(item.get("source_url") or ""))
            host = parsed.netloc.lower()
            path = parsed.path.rstrip("/").lower()
            if host.endswith(".invalid") or host in {"example.com", "www.example.com"}:
                return True
            if path.endswith("/123") or path in {"/p/123", "/article/123"}:
                return True
    return False


def normalize_report(raw: Any, thesis_hint: str = "") -> dict[str, Any]:
    config = load_json(SCORING_CONFIG_PATH) or {}
    demo_report = is_demo_report(raw)
    base = raw.get("report") if isinstance(raw, dict) and isinstance(raw.get("report"), dict) else raw
    if not isinstance(base, dict):
        base = {}

    candidates_raw = base.get("candidates")
    if not isinstance(candidates_raw, list):
        candidates_raw = base.get("candidate_companies")
    if not isinstance(candidates_raw, list):
        candidates_raw = []
    if demo_report:
        candidates_raw = []

    candidates = [normalize_candidate(item, config) for item in candidates_raw if isinstance(item, dict)]
    candidates.sort(key=lambda item: item["priority_score"], reverse=True)

    total_evidence = safe_int(base.get("total_evidence"), 0)
    if demo_report:
        total_evidence = 0
    if not total_evidence:
        total_evidence = sum(len(candidate["evidence"]) for candidate in candidates)
    engines = []
    search_strategy = base.get("search_strategy")
    if isinstance(search_strategy, dict) and isinstance(search_strategy.get("providers_used"), list):
        engines = [str(item) for item in search_strategy["providers_used"] if str(item).strip()]
    elif isinstance(raw, dict):
        pipeline = raw.get("pipeline")
        if isinstance(pipeline, dict) and isinstance(pipeline.get("search_engines_used"), list):
            engines = [str(item) for item in pipeline["search_engines_used"] if str(item).strip()]

    domains = set()
    for candidate in candidates:
        for evidence in candidate["evidence"]:
            if evidence.get("source_url"):
                domains.add(urlparse(evidence["source_url"]).netloc.lower())

    avg_confidence = 0.0
    if candidates:
        avg_confidence = sum(candidate["confidence_score"] for candidate in candidates) / len(candidates)

    generated_at = base.get("generated_at")
    if not generated_at and isinstance(base.get("meta"), dict):
        generated_at = base["meta"].get("generated_at")

    thesis = (thesis_hint or "") if demo_report else (base.get("thesis") or thesis_hint or "")
    if not thesis and isinstance(base.get("meta"), dict):
        thesis = base["meta"].get("headline") or ""

    compatibility_note = "历史演示已隔离" if demo_report else ("兼容旧格式报告" if any(candidate["dimension_rows"] and candidate["dimension_rows"][0]["compatibility_note"] for candidate in candidates) else "九维结构报告")
    top_bucket = candidates[0]["score_bucket"] if candidates else ("等待真实证据" if demo_report else "等待评分")
    scoring_version = base.get("scoring_version") or config.get("version") or "DealScope 证据评分"

    return {
        "generated_at": "尚无真实报告" if demo_report else (generated_at or "暂无"),
        "title_line": thesis or "尚未生成报告",
        "subtitle_line": f"当前研究主题：{thesis}" if thesis else "当前还没有证据评估结果。",
        "scoring_version": scoring_version,
        "compatibility_note": compatibility_note,
        "is_demo": demo_report,
        "candidates": candidates,
        "stats": {
            "total_candidates": len(candidates),
            "total_evidence": total_evidence,
            "source_domains": len(domains),
            "domain_preview": " / ".join(sorted(list(domains))[:3]) if domains else "暂无域名覆盖",
            "search_engine_count": len(engines),
            "search_engine_text": " / ".join(engines) if engines else "手工发现 / 本地抓取",
            "avg_confidence": round(avg_confidence, 1),
            "top_bucket": top_bucket,
        },
    }


def open_path_in_os(path: Path) -> bool:
    if hasattr(os, "startfile"):
        os.startfile(str(path))
        return True

    commands: list[list[str]] = []
    if shutil.which("wslview"):
        commands.append(["wslview", str(path)])
    if shutil.which("explorer.exe"):
        try:
            windows_path = subprocess.run(
                ["wslpath", "-w", str(path)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip() or str(path)
        except Exception:
            windows_path = str(path)
        commands.append(["explorer.exe", windows_path])
    if sys.platform == "darwin":
        commands.append(["open", str(path)])
    if shutil.which("xdg-open"):
        commands.append(["xdg-open", str(path)])

    for command in commands:
        try:
            subprocess.Popen(command)
            return True
        except OSError:
            continue
    return False


def build_redirect(home_query: str, company: str, intensity: str, message: str) -> str:
    return url_for("home", q=home_query, company=company, intensity=intensity, msg=message)


@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    company = request.args.get("company", "").strip()
    intensity = request.args.get("intensity", "standard").strip() or "standard"
    message = request.args.get("msg", "").strip()

    report = normalize_report(load_json(REPORT_PATH), thesis_hint=q)
    latest_attempt = load_json(LATEST_ATTEMPT_PATH)
    links = build_discovery_links(q) if q else load_saved_links()
    grouped_links = group_links(links)
    collector_playbook = load_collector_playbook()
    session_statuses = build_session_statuses()
    return render_template_string(
        TEMPLATE,
        q=q,
        company=company,
        intensity=intensity,
        intensity_options=INTENSITY_OPTIONS,
        grouped_links=grouped_links,
        collector_playbook=collector_playbook,
        session_statuses=session_statuses,
        report=report,
        manual_urls_text=load_manual_urls_text(),
        pipeline_runtime=dict(_pipeline_state),
        latest_attempt=latest_attempt if isinstance(latest_attempt, dict) else {},
        banner_message=message,
        fmt=fmt,
    )


@app.post("/generate")
def generate_links():
    q = request.form.get("q", "").strip()
    company = request.form.get("company", "").strip()
    intensity = request.form.get("intensity", "standard").strip() or "standard"
    if q:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        DISCOVERY_LINKS_PATH.write_text(
            json.dumps(build_discovery_links(q), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        message = "发现链接已更新。"
    else:
        message = "请先输入研究主题。"
    return redirect(build_redirect(q, company, intensity, message))


@app.post("/save-urls")
def save_manual_urls_route():
    q = request.form.get("q", "").strip()
    company = request.form.get("company", "").strip()
    intensity = request.form.get("intensity", "standard").strip() or "standard"
    accepted, rejected = save_manual_urls(request.form.get("urls", ""))
    message = f"已保存 {len(accepted)} 条待核验原文链接。"
    if rejected:
        message += f" 另有 {len(rejected)} 条不是有效的 HTTP(S) 链接，未保存。"
    return redirect(build_redirect(q, company, intensity, message))


@app.post("/login/<platform>")
def login_platform(platform: str):
    if platform not in SESSION_PLATFORM_META:
        return redirect(url_for("home", msg="不支持的登录平台。"))

    command = [
        sys.executable,
        str(ROOT / "collectors" / "session_login.py"),
        "--platform",
        platform,
    ]
    try:
        subprocess.Popen(command, cwd=str(ROOT))
        message = f"已启动 {SESSION_PLATFORM_META[platform]['label']} 登录窗口，完成登录后会自动保存 session。"
    except OSError as exc:
        message = f"启动登录窗口失败：{str(exc)[:120]}"
    return redirect(url_for("home", msg=message))


def _parse_pipeline_outcome(stdout: str, stderr: str) -> tuple[dict[str, Any], list[str]]:
    output_lines = [line.strip() for line in f"{stdout}\n{stderr}".splitlines() if line.strip()]
    for line in reversed(output_lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed, output_lines
    return {}, output_lines


def _run_pipeline_in_background(command: list[str]) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=15 * 60,
        )
        outcome, output_lines = _parse_pipeline_outcome(result.stdout or "", result.stderr or "")
        if result.returncode == 0 and outcome.get("ok", True):
            status = "success"
            message = str(outcome.get("message") or "采集与评分已完成，报告已刷新。")
        else:
            status = "error"
            detail = str(outcome.get("message") or (output_lines[-1] if output_lines else "未知错误"))
            message = f"本轮未生成新报告：{detail}"
    except subprocess.TimeoutExpired:
        status = "error"
        message = "本轮评估超过 15 分钟，已停止等待；上一次成功报告没有被覆盖。"
    except Exception as exc:
        status = "error"
        message = f"评估任务异常：{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        _pipeline_state.update(
            running=False,
            status=status,
            message=message,
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        _pipeline_lock.release()


def _start_pipeline(command: list[str], thesis: str) -> bool:
    if not _pipeline_lock.acquire(blocking=False):
        return False
    _pipeline_state.update(
        running=True,
        status="running",
        message="正在读取原文并进行证据评估。",
        thesis=thesis,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        finished_at="",
    )
    try:
        thread = threading.Thread(
            target=_run_pipeline_in_background,
            args=(command,),
            name="pe-evidence-pipeline",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        _pipeline_state.update(running=False, status="error", message="后台评估任务启动失败。")
        _pipeline_lock.release()
        raise


@app.post("/run")
def run_pipeline_route():
    q = request.form.get("q", "").strip()
    company = request.form.get("company", "").strip()
    intensity = request.form.get("intensity", "standard").strip() or "standard"
    if not q:
        return redirect(build_redirect(q, company, intensity, "请先输入研究主题。"))

    if "urls" in request.form:
        accepted, rejected = save_manual_urls(request.form.get("urls", ""))
        if rejected:
            return redirect(
                build_redirect(
                    q,
                    company,
                    intensity,
                    f"有 {len(rejected)} 条链接格式无效，已停止运行；请检查后重试。",
                )
            )

    command = [sys.executable, str(ROOT / "run_pipeline.py"), "--thesis", q, "--intensity", intensity]
    if company:
        command.extend(["--company", company])
    started = _start_pipeline(command, q)
    message = "评估已在后台启动，完成后页面会自动刷新。" if started else "已有一个评估任务正在运行，请稍候。"
    return redirect(build_redirect(q, company, intensity, message))


@app.get("/api/pipeline-status")
def pipeline_status():
    return dict(_pipeline_state)


@app.post("/open-input")
def open_input():
    ok = open_path_in_os(URLS_PATH)
    return redirect(url_for("home", msg="已尝试打开 urls.txt。" if ok else "当前环境无法直接打开 urls.txt。"))


@app.post("/open-project")
def open_project():
    ok = open_path_in_os(ROOT)
    return redirect(url_for("home", msg="已尝试打开项目目录。" if ok else "当前环境无法直接打开项目目录。"))


@app.route("/health")
def health():
    return {
        "ok": True,
        "service": "DealScopeWorkbench",
        "port": 8787,
        "pipeline": dict(_pipeline_state),
    }


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8787, debug=False)
