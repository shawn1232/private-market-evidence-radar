# Synthetic fixtures

本目录只包含纯合成数据模板：

- `synthetic_weekly_report.json`：周度雷达缓存模板；
- `synthetic_deep_report.json`：深度评估缓存模板。

所有主体名称均带“（虚构）”，所有 URL 均使用 `.invalid` 保留域名，所有顶层对象均包含 `synthetic: true` 与英文演示声明。

模板中的 `__AS_OF__`、`__WINDOW_START__`、`__EVENT_DATE_1__` 等占位符由 `scripts/load_demo.py` 替换。请勿用真实公司、公众号、项目材料或网页正文修改这些公开 fixture。
